from __future__ import annotations

import random
import threading
import time
from datetime import timedelta
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import session_scope
from app.models.base import new_id, now_utc
from app.models.entities import AgentTask, ProjectPage
from app.services.events import append_event
from app.services.search_quality import MIN_DIGEST_CHARS

STATUS_PENDING = 1
STATUS_PROCESSING = 2
STATUS_SUCCESS = 3
STATUS_FAILED = 4
STATUS_CANCELED = 5

PAGE_ACTION_TO_TASK_TYPE = {
    "page_generate_search_queries": "page_search_queries",
    "page_search_run": "page_search",
    "page_search_refresh": "page_search",
    "page_summary_generate": "page_summary",
    "page_draft_generate": "page_draft",
    "page_design_generate": "page_design",
}

BATCH_TO_PAGE_ACTION = {
    "project_batch_search": "page_search_run",
    "project_batch_summary": "page_summary_generate",
    "project_batch_draft": "page_draft_generate",
    "project_batch_design": "page_design_generate",
}

_ACTIVE_STATUSES = (STATUS_PENDING, STATUS_PROCESSING)
_wake_event = threading.Event()
_scheduler_lock = threading.Lock()
_scheduler_started = False
_stop_event = threading.Event()
_workers: list[threading.Thread] = []


class TaskCanceled(Exception):
    """Worker stopped at a stage boundary because cancel_requested was set."""


def wake_scheduler() -> None:
    if get_settings().run_jobs_inline:
        while run_once():
            pass
        return
    _wake_event.set()


def enqueue_project_task(
    session: Session,
    *,
    project_id: str,
    task_type: str,
    task_context: dict[str, Any] | None = None,
    priority: int = 50,
) -> AgentTask:
    if task_type != "message":
        existing = _find_active_task(session, project_id=project_id, page_id=None, task_type=task_type)
        if existing:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="同类任务已在队列中")
    return _insert_task(
        session,
        project_id=project_id,
        page_id=None,
        task_type=task_type,
        task_context=task_context or {},
        priority=priority,
    )


def enqueue_page_action(
    session: Session,
    *,
    project_id: str,
    page_id: str,
    action_type: str,
    agent_run_id: str,
    replace_existing: bool = True,
    priority: int = 100,
    batch_run_id: str | None = None,
) -> AgentTask:
    task_type = PAGE_ACTION_TO_TASK_TYPE.get(action_type)
    if not task_type:
        raise HTTPException(status_code=400, detail=f"不支持的页面动作: {action_type}")
    existing = _find_active_task(session, project_id=project_id, page_id=page_id, task_type=task_type)
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该页同类任务已在队列中")
    return _insert_task(
        session,
        project_id=project_id,
        page_id=page_id,
        task_type=task_type,
        task_context={
            "action_type": action_type,
            "agent_run_id": agent_run_id,
            "replace_existing": replace_existing,
            "batch_run_id": batch_run_id,
        },
        priority=priority,
        batch_run_id=batch_run_id,
    )


def enqueue_batch_action(
    session: Session,
    *,
    project_id: str,
    action_type: str,
    agent_run_id: str,
    page_ids: list[str] | None = None,
    parent_batch_run_id: str | None = None,
    retry_reason: str | None = None,
) -> list[AgentTask]:
    from app.services.batch_run import create_batch_run, refresh_batch_run

    page_action = BATCH_TO_PAGE_ACTION.get(action_type)
    if not page_action:
        raise HTTPException(status_code=400, detail=f"不支持的批量动作: {action_type}")
    pages = list(
        session.scalars(select(ProjectPage).where(ProjectPage.project_id == project_id).order_by(ProjectPage.sort_order.asc()))
    )
    if page_ids is not None:
        allowed = set(page_ids)
        candidate_pages = [page for page in pages if page.id in allowed]
    else:
        candidate_pages = pages
    eligible_ids = {page.id for page in candidate_pages if _batch_page_eligible(page, action_type)}
    batch = create_batch_run(
        session,
        project_id=project_id,
        action_type=action_type,
        agent_run_id=agent_run_id,
        pages=pages,
        eligible_ids=eligible_ids,
        parent_batch_run_id=parent_batch_run_id,
        retry_reason=retry_reason,
    )
    created: list[AgentTask] = []
    for page in pages:
        if page.id not in eligible_ids:
            continue
        try:
            created.append(
                enqueue_page_action(
                    session,
                    project_id=project_id,
                    page_id=page.id,
                    action_type=page_action,
                    agent_run_id=agent_run_id,
                    replace_existing=True,
                    priority=0,
                    batch_run_id=batch.id,
                )
            )
        except HTTPException as exc:
            if exc.status_code != status.HTTP_409_CONFLICT:
                raise
    batch.queued_count = len(created)
    batch.skipped_count = max(0, len(pages) - len(created))
    session.flush()
    refresh_batch_run(session, batch.id)
    append_event(
        session,
        project_id=project_id,
        event_type="task.batch_queued",
        stage=action_type,
        scope_type="project",
        payload={
            "action_type": action_type,
            "queued": len(created),
            "skipped": batch.skipped_count,
            "expected": batch.expected_count,
            "agent_run_id": agent_run_id,
            "batch_run_id": batch.id,
        },
        agent_run_id=agent_run_id,
    )
    return created


def enqueue_export_job(
    session: Session,
    *,
    project_id: str,
    export_id: str,
    agent_run_id: str | None = None,
) -> AgentTask:
    return _insert_task(
        session,
        project_id=project_id,
        page_id=None,
        task_type="export",
        task_context={"export_id": export_id, "agent_run_id": agent_run_id or ""},
        priority=80,
        max_retry_num=1,
    )


def enqueue_quality_eval_job(
    session: Session,
    *,
    project_id: str,
    eval_id: str,
    agent_run_id: str | None = None,
) -> AgentTask:
    return _insert_task(
        session,
        project_id=project_id,
        page_id=None,
        task_type="quality_eval",
        task_context={"eval_id": eval_id, "agent_run_id": agent_run_id or ""},
        priority=60,
        max_retry_num=1,
    )


def cancel_tasks(session: Session, *, project_id: str, page_id: str | None = None) -> int:
    now = now_utc()
    filters = [AgentTask.project_id == project_id, AgentTask.status.in_(_ACTIVE_STATUSES)]
    if page_id:
        filters.append(AgentTask.page_id == page_id)
    pending_count = session.execute(
        update(AgentTask)
        .where(*filters, AgentTask.status == STATUS_PENDING)
        .values(status=STATUS_CANCELED, cancel_requested=1, updated_at=now, task_stage="canceled")
    ).rowcount or 0
    processing_count = session.execute(
        update(AgentTask).where(*filters, AgentTask.status == STATUS_PROCESSING).values(cancel_requested=1, updated_at=now)
    ).rowcount or 0
    append_event(
        session,
        project_id=project_id,
        event_type="task.canceled",
        stage="search",
        scope_type="page" if page_id else "project",
        target_page_id=page_id,
        payload={"pending": pending_count, "processing": processing_count},
    )
    _refresh_batches_for_project(session, project_id)
    return pending_count + processing_count


def reclaim_expired_tasks(session: Session | None = None) -> int:
    now = now_utc()
    recovered = 0
    if session is None:
        with session_scope() as owned:
            return reclaim_expired_tasks(owned)
    expired = list(
        session.scalars(
            select(AgentTask).where(
                AgentTask.status == STATUS_PROCESSING,
                or_(AgentTask.lease_expires_at.is_(None), AgentTask.lease_expires_at < now),
            )
        )
    )
    for task in expired:
        if task.cancel_requested:
            _finish_task(session, task, STATUS_CANCELED, "lease expired after cancel")
        elif task.retry_num + 1 >= task.max_retry_num:
            task.retry_num += 1
            _finish_task(session, task, STATUS_FAILED, "lease expired, retries exhausted")
            _mark_related_page_failed(session, task)
        else:
            task.retry_num += 1
            task.status = STATUS_PENDING
            task.next_run_at = now
            task.lease_expires_at = None
            task.updated_at = now
            _append_log(task, "reclaimed")
            recovered += 1
    session.flush()
    _clear_orphaned_running_pages(session)
    batch_ids = {task.batch_run_id for task in expired if task.batch_run_id}
    if batch_ids:
        from app.services.batch_run import refresh_batch_run

        for batch_id in batch_ids:
            refresh_batch_run(session, str(batch_id))
    return recovered


def run_once() -> bool:
    task_id = _claim_next_task_id()
    if not task_id:
        return False
    _execute_claimed_task(task_id)
    return True


def start_scheduler() -> None:
    global _scheduler_started
    settings = get_settings()
    reclaim_expired_tasks()
    if not settings.task_worker_enabled or settings.run_jobs_inline:
        return
    with _scheduler_lock:
        if _scheduler_started:
            return
        _stop_event.clear()
        _scheduler_started = True
        for index in range(max(1, settings.task_worker_count)):
            thread = threading.Thread(target=_worker_loop, name=f"ppt-task-worker-{index}", daemon=True)
            _workers.append(thread)
            thread.start()


def stop_scheduler() -> None:
    global _scheduler_started
    _stop_event.set()
    _wake_event.set()
    with _scheduler_lock:
        _scheduler_started = False
        _workers.clear()


def _insert_task(
    session: Session,
    *,
    project_id: str,
    page_id: str | None,
    task_type: str,
    task_context: dict[str, Any],
    priority: int,
    batch_run_id: str | None = None,
    max_retry_num: int = 3,
) -> AgentTask:
    now = now_utc()
    task = AgentTask(
        task_id=new_id(),
        project_id=project_id,
        page_id=page_id,
        batch_run_id=batch_run_id,
        task_type=task_type,
        task_stage="init",
        status=STATUS_PENDING,
        priority=priority,
        next_run_at=now,
        retry_num=0,
        max_retry_num=max_retry_num,
        task_context=task_context,
        schedule_log=[{"at": now.isoformat(), "event": "queued"}],
        cancel_requested=0,
    )
    session.add(task)
    session.flush()
    append_event(
        session,
        project_id=project_id,
        event_type="task.queued",
        stage=task_type,
        scope_type="page" if page_id else "project",
        target_page_id=page_id,
        payload={"task_id": task.task_id, "task_type": task_type, "priority": priority},
        agent_run_id=str(task_context.get("agent_run_id") or ""),
    )
    return task


def _find_active_task(session: Session, *, project_id: str, page_id: str | None, task_type: str) -> AgentTask | None:
    stmt: Select[tuple[AgentTask]] = select(AgentTask).where(
        AgentTask.project_id == project_id,
        AgentTask.task_type == task_type,
        AgentTask.status.in_(_ACTIVE_STATUSES),
    )
    if page_id is None:
        stmt = stmt.where(AgentTask.page_id.is_(None))
    else:
        stmt = stmt.where(AgentTask.page_id == page_id)
    return session.scalars(stmt.limit(1)).first()


def _batch_page_eligible(page: ProjectPage, action_type: str) -> bool:
    digest = page.page_corpus_digest_json or {}
    if action_type == "project_batch_search":
        if page.page_role != "content":
            return False
        return not (page.search_status == "ready" and bool(digest.get("document_count")))
    if action_type == "project_batch_summary":
        if page.page_role != "content" or not digest.get("document_count"):
            return False
        # content_chars 是后加字段：老资料池没有该键时不在这里拦，交给
        # _run_page_summary 惰性补算后再判，避免误伤资料池完好的历史页面。
        if "content_chars" in digest and int(digest.get("content_chars") or 0) < MIN_DIGEST_CHARS:
            return False
        return page.summary_status != "ready"
    if action_type == "project_batch_draft":
        if page.page_role == "content" and not page.page_summary_md:
            return False
        return page.draft_status != "ready"
    if action_type == "project_batch_design":
        if not page.current_draft_version_id:
            return False
        return page.design_status != "ready"
    return False


def _claim_next_task_id() -> str | None:
    now = now_utc()
    lease = now + timedelta(seconds=get_settings().task_lease_seconds)
    with session_scope() as session:
        if session.get_bind().dialect.name == "sqlite":
            session.execute(select(func.now()))
        candidates = list(
            session.scalars(
                select(AgentTask.task_id)
                .where(
                    AgentTask.status == STATUS_PENDING,
                    AgentTask.next_run_at <= now,
                    AgentTask.cancel_requested == 0,
                )
                .order_by(AgentTask.priority.desc(), AgentTask.next_run_at.asc())
                .limit(8)
            )
        )
        for task_id in candidates:
            result = session.execute(
                update(AgentTask)
                .where(AgentTask.task_id == task_id, AgentTask.status == STATUS_PENDING, AgentTask.cancel_requested == 0)
                .values(status=STATUS_PROCESSING, lease_expires_at=lease, updated_at=now)
            )
            if result.rowcount == 1:
                return task_id
    return None


def _execute_claimed_task(task_id: str) -> None:
    from app.services.orchestrator import PptAgentService

    with session_scope() as session:
        task = session.get(AgentTask, task_id)
        if task is None:
            return
        if task.cancel_requested:
            _finish_task(session, task, STATUS_CANCELED, "canceled before run")
            return
        _append_log(task, "started")
        if task.batch_run_id:
            from app.services.batch_run import refresh_batch_run

            refresh_batch_run(session, task.batch_run_id)
        context = dict(task.task_context or {})
        service = PptAgentService(session)
        try:
            if task.task_type == "bootstrap":
                service.run_bootstrap_flow(task.project_id)
            elif task.task_type == "outline":
                service.run_outline_flow(task.project_id)
            elif task.task_type == "message":
                service.run_message_flow(str(context["message_id"]))
            elif task.task_type in set(PAGE_ACTION_TO_TASK_TYPE.values()):
                service.run_page_action_flow(
                    task.project_id,
                    str(task.page_id),
                    str(context["action_type"]),
                    str(context.get("agent_run_id") or new_id()),
                    bool(context.get("replace_existing", True)),
                )
            elif task.task_type == "export":
                service.run_export_job(str(context["export_id"]))
            elif task.task_type == "quality_eval":
                service.run_quality_eval_job(str(context["eval_id"]))
            else:
                raise RuntimeError(f"未知任务类型: {task.task_type}")
            session.refresh(task)
            task.task_stage = "done"
            _finish_task(session, task, STATUS_SUCCESS, "success")
        except TaskCanceled:
            session.rollback()
            with session_scope() as retry_session:
                latest = retry_session.get(AgentTask, task_id)
                if latest:
                    _finish_task(retry_session, latest, STATUS_CANCELED, "canceled")
        except Exception as exc:
            session.rollback()
            _handle_failure(task_id, str(exc))


def _handle_failure(task_id: str, error: str) -> None:
    with session_scope() as session:
        task = session.get(AgentTask, task_id)
        if task is None:
            return
        task.retry_num += 1
        context = dict(task.task_context or {})
        context["last_error"] = error[:300]
        task.task_context = context
        if task.cancel_requested:
            _finish_task(session, task, STATUS_CANCELED, error)
            return
        if task.retry_num >= task.max_retry_num:
            _finish_task(session, task, STATUS_FAILED, error)
            _mark_related_page_failed(session, task)
            return
        delay = min(60.0, (2 ** (task.retry_num - 1)) + random.uniform(0, 0.5))
        task.status = STATUS_PENDING
        task.next_run_at = now_utc() + timedelta(seconds=delay)
        task.lease_expires_at = None
        task.updated_at = now_utc()
        _append_log(task, f"retry in {delay:.1f}s: {error[:180]}")
        append_event(
            session,
            project_id=task.project_id,
            event_type="task.retry",
            stage=task.task_type,
            scope_type="page" if task.page_id else "project",
            target_page_id=task.page_id,
            payload={"task_id": task.task_id, "retry_num": task.retry_num, "error": error[:300]},
        )


def _finish_task(session: Session, task: AgentTask, status_value: int, note: str) -> None:
    task.status = status_value
    task.updated_at = now_utc()
    task.lease_expires_at = None
    if status_value == STATUS_SUCCESS:
        task.task_stage = "done"
    _append_log(task, note)
    event_type = {
        STATUS_SUCCESS: "task.succeeded",
        STATUS_FAILED: "task.failed",
        STATUS_CANCELED: "task.canceled",
    }.get(status_value, "task.updated")
    append_event(
        session,
        project_id=task.project_id,
        event_type=event_type,
        stage=task.task_type,
        scope_type="page" if task.page_id else "project",
        target_page_id=task.page_id,
        payload={"task_id": task.task_id, "status": status_value, "note": note[:300]},
        agent_run_id=str((task.task_context or {}).get("agent_run_id") or ""),
    )
    if task.batch_run_id:
        from app.services.batch_run import refresh_batch_run

        refresh_batch_run(session, task.batch_run_id)
    _sync_export_job_terminal(session, task, status_value, note)
    _sync_quality_eval_terminal(session, task, status_value, note)


def _sync_export_job_terminal(session: Session, task: AgentTask, status_value: int, note: str) -> None:
    if task.task_type != "export" or status_value not in {STATUS_FAILED, STATUS_CANCELED}:
        return
    export_id = str((task.task_context or {}).get("export_id") or "")
    if not export_id:
        return
    from app.models.entities import ExportJob
    from app.services.export_job import fail_export_job

    job = session.get(ExportJob, export_id)
    if job is None or job.status not in {"queued", "running"}:
        return
    if status_value == STATUS_CANCELED:
        fail_export_job(
            session,
            job,
            {
                "error_code": "EXPORT_CANCELED",
                "category": "user_action_required",
                "message": "导出已取消",
                "retryability": "retry_now",
            },
            status="canceled",
        )
        return
    fail_export_job(
        session,
        job,
        {
            "error_code": "EXPORT_WORKER_FAILED",
            "category": "transient_infra",
            "message": note[:300] or "导出 worker 失败",
            "retryability": "retry_now",
        },
    )


def _sync_quality_eval_terminal(session: Session, task: AgentTask, status_value: int, note: str) -> None:
    if task.task_type != "quality_eval" or status_value not in {STATUS_FAILED, STATUS_CANCELED}:
        return
    eval_id = str((task.task_context or {}).get("eval_id") or "")
    if not eval_id:
        return
    from app.models.entities import QualityEvalJob
    from app.services.quality_eval import fail_quality_eval_job

    job = session.get(QualityEvalJob, eval_id)
    if job is None or job.status not in {"queued", "running"}:
        return
    if status_value == STATUS_CANCELED:
        fail_quality_eval_job(
            session,
            job,
            {
                "error_code": "QUALITY_EVAL_CANCELED",
                "category": "user_action_required",
                "message": "主观评估已取消",
                "retryability": "retry_now",
            },
            status_value="canceled",
        )
        return
    fail_quality_eval_job(
        session,
        job,
        {
            "error_code": "QUALITY_EVAL_WORKER_FAILED",
            "category": "transient_infra",
            "message": note[:300] or "主观评估 worker 失败",
            "retryability": "retry_now",
        },
    )


def _mark_related_page_failed(session: Session, task: AgentTask) -> None:
    if not task.page_id:
        return
    page = session.get(ProjectPage, task.page_id)
    if page is None:
        return
    action_type = str((task.task_context or {}).get("action_type") or "")
    if task.task_type in {"page_search", "page_search_queries"} or action_type in {
        "page_search_run",
        "page_search_refresh",
        "page_generate_search_queries",
    }:
        if page.search_status == "running":
            page.search_status = "failed"
    elif task.task_type == "page_summary" or action_type == "page_summary_generate":
        if page.summary_status == "running":
            page.summary_status = "failed"
    elif task.task_type == "page_draft" or action_type == "page_draft_generate":
        if page.draft_status == "running":
            page.draft_status = "failed"
    elif task.task_type == "page_design" or action_type == "page_design_generate":
        if page.design_status == "running":
            page.design_status = "failed"


def _clear_orphaned_running_pages(session: Session) -> None:
    active_page_ids = set(
        session.scalars(select(AgentTask.page_id).where(AgentTask.status == STATUS_PROCESSING, AgentTask.page_id.is_not(None)))
    )
    pages = session.scalars(
        select(ProjectPage).where(
            or_(
                ProjectPage.search_status == "running",
                ProjectPage.summary_status == "running",
                ProjectPage.draft_status == "running",
                ProjectPage.design_status == "running",
            )
        )
    )
    for page in pages:
        if page.id in active_page_ids:
            continue
        if page.search_status == "running":
            page.search_status = "failed"
        if page.summary_status == "running":
            page.summary_status = "failed"
        if page.draft_status == "running":
            page.draft_status = "failed"
        if page.design_status == "running":
            page.design_status = "failed"


def _refresh_batches_for_project(session: Session, project_id: str) -> None:
    from app.services.batch_run import refresh_batch_run

    batch_ids = set(
        session.scalars(select(AgentTask.batch_run_id).where(AgentTask.project_id == project_id, AgentTask.batch_run_id.is_not(None)))
    )
    for batch_id in batch_ids:
        refresh_batch_run(session, str(batch_id))


def _append_log(task: AgentTask, event: str) -> None:
    log = list(task.schedule_log or [])
    log.append({"at": now_utc().isoformat(), "event": event[:240]})
    task.schedule_log = log[-20:]


def _worker_loop() -> None:
    poll_seconds = max(0.2, get_settings().task_poll_interval_ms / 1000)
    while not _stop_event.is_set():
        ran = False
        try:
            ran = run_once()
        except Exception:
            time.sleep(min(2.0, poll_seconds * 2))
        if ran:
            continue
        _wake_event.wait(timeout=poll_seconds)
        _wake_event.clear()
