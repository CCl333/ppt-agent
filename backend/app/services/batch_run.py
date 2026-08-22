from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.base import now_utc
from app.models.entities import AgentTask, BatchRun, DesignVersion, DraftVersion, ProjectPage
from app.services.events import append_event
from app.services.quality_report import hash_svg
from app.services.runtime import build_fingerprint

STATUS_PENDING = 1
STATUS_PROCESSING = 2
STATUS_SUCCESS = 3
STATUS_FAILED = 4
STATUS_CANCELED = 5


def create_batch_run(
    session: Session,
    *,
    project_id: str,
    action_type: str,
    agent_run_id: str,
    pages: list[ProjectPage],
    eligible_ids: set[str],
    parent_batch_run_id: str | None = None,
    retry_reason: str | None = None,
) -> BatchRun:
    snapshot_pages = [capture_page_snapshot(session, page, action_type, eligible=page.id in eligible_ids) for page in pages]
    batch = BatchRun(
        project_id=project_id,
        action_type=action_type,
        status="queued",
        expected_count=len(eligible_ids),
        queued_count=0,
        skipped_count=max(0, len(pages) - len(eligible_ids)),
        input_snapshot_json={"pages": snapshot_pages, "action_type": action_type},
        build_fingerprint_json=build_fingerprint(),
        parent_batch_run_id=parent_batch_run_id,
        retry_reason=retry_reason,
        agent_run_id=agent_run_id,
    )
    session.add(batch)
    session.flush()
    return batch


def capture_page_snapshot(
    session: Session,
    page: ProjectPage,
    action_type: str,
    *,
    eligible: bool,
) -> dict[str, Any]:
    draft = session.get(DraftVersion, page.current_draft_version_id) if page.current_draft_version_id else None
    design = session.get(DesignVersion, page.current_design_version_id) if page.current_design_version_id else None
    digest = page.page_corpus_digest_json or {}
    return {
        "page_id": page.id,
        "page_code": page.page_code,
        "sort_order": page.sort_order,
        "page_role": page.page_role,
        "eligible": eligible,
        "search_status": page.search_status,
        "summary_status": page.summary_status,
        "draft_status": page.draft_status,
        "design_status": page.design_status,
        "current_brief_version_id": page.current_brief_version_id,
        "current_draft_version_id": page.current_draft_version_id,
        "current_design_version_id": page.current_design_version_id,
        "digest_document_count": digest.get("document_count"),
        "digest_content_chars": digest.get("content_chars"),
        "summary_hash": hash_svg(page.page_summary_md or ""),
        "draft_svg_hash": draft.svg_hash if draft else None,
        "design_svg_hash": design.svg_hash if design else None,
        "action_type": action_type,
    }


def page_snapshot_matches(session: Session, page: ProjectPage, snapshot: dict[str, Any], action_type: str) -> bool:
    current = capture_page_snapshot(session, page, action_type, eligible=True)
    keys = _compare_keys_for_action(action_type)
    return all(current.get(key) == snapshot.get(key) for key in keys)


def refresh_batch_run(session: Session, batch_run_id: str) -> BatchRun | None:
    batch = session.get(BatchRun, batch_run_id)
    if batch is None:
        return None
    previous_status = batch.status
    previous_counts = (batch.success_count, batch.failed_count, batch.canceled_count, batch.running_count)
    tasks = list(session.scalars(select(AgentTask).where(AgentTask.batch_run_id == batch_run_id)))
    success = sum(1 for item in tasks if item.status == STATUS_SUCCESS)
    failed = sum(1 for item in tasks if item.status == STATUS_FAILED)
    canceled = sum(1 for item in tasks if item.status == STATUS_CANCELED)
    running = sum(1 for item in tasks if item.status == STATUS_PROCESSING)
    pending = sum(1 for item in tasks if item.status == STATUS_PENDING)
    batch.success_count = success
    batch.failed_count = failed
    batch.canceled_count = canceled
    batch.running_count = running
    failure_summary: dict[str, int] = {}
    for task in tasks:
        if task.status != STATUS_FAILED:
            continue
        context = task.task_context or {}
        code = str(context.get("last_error_code") or context.get("last_error") or "FAILED")[:120]
        failure_summary[code] = int(failure_summary.get(code) or 0) + 1
    batch.failure_summary_json = failure_summary
    now = now_utc()
    terminal = pending == 0 and running == 0
    if batch.queued_count == 0:
        batch.status = "success"
        batch.finished_at = batch.finished_at or now
    elif not terminal:
        if running > 0 or success + failed + canceled > 0:
            batch.status = "running"
            if batch.started_at is None:
                batch.started_at = now
        else:
            batch.status = "queued"
        batch.finished_at = None
    else:
        if canceled == len(tasks) and canceled > 0:
            batch.status = "canceled"
        elif failed == 0 and canceled == 0:
            batch.status = "success"
        elif success == 0:
            batch.status = "failed"
        else:
            batch.status = "partial_success"
        batch.finished_at = now
        batch.running_count = 0
    changed = (
        previous_status != batch.status
        or previous_counts != (batch.success_count, batch.failed_count, batch.canceled_count, batch.running_count)
    )
    session.flush()
    if not changed:
        return batch
    append_event(
        session,
        project_id=batch.project_id,
        event_type="batch.updated",
        stage=batch.action_type,
        scope_type="project",
        payload={
            "batch_run_id": batch.id,
            "status": batch.status,
            "success_count": batch.success_count,
            "failed_count": batch.failed_count,
            "canceled_count": batch.canceled_count,
            "running_count": batch.running_count,
        },
        agent_run_id=batch.agent_run_id,
    )
    return batch


def serialize_batch_run(session: Session, batch: BatchRun) -> dict[str, Any]:
    tasks = list(session.scalars(select(AgentTask).where(AgentTask.batch_run_id == batch.id)))
    return {
        "batch_run_id": batch.id,
        "agent_run_id": batch.agent_run_id,
        "action_type": batch.action_type,
        "status": batch.status,
        "expected_count": batch.expected_count,
        "queued_count": batch.queued_count,
        "skipped_count": batch.skipped_count,
        "running_count": batch.running_count,
        "success_count": batch.success_count,
        "failed_count": batch.failed_count,
        "canceled_count": batch.canceled_count,
        "failure_summary": batch.failure_summary_json or {},
        "input_snapshot": batch.input_snapshot_json or {},
        "retry_reason": batch.retry_reason,
        "parent_batch_run_id": batch.parent_batch_run_id,
        "build_fingerprint": batch.build_fingerprint_json or {},
        "task_ids": [item.task_id for item in tasks],
        "tasks": [
            {
                "task_id": item.task_id,
                "page_id": item.page_id,
                "status": item.status,
                "task_type": item.task_type,
            }
            for item in tasks
        ],
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "started_at": batch.started_at.isoformat() if batch.started_at else None,
        "finished_at": batch.finished_at.isoformat() if batch.finished_at else None,
    }


def retry_failed_batch(session: Session, *, project_id: str, batch_run_id: str, agent_run_id: str) -> BatchRun:
    from app.services.tasks import BATCH_TO_PAGE_ACTION, _batch_page_eligible, enqueue_page_action

    batch = session.get(BatchRun, batch_run_id)
    if batch is None or batch.project_id != project_id:
        raise HTTPException(status_code=404, detail="批次不存在")
    if batch.status not in {"failed", "partial_success"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="当前批次没有可重试的失败页")
    failed_tasks = list(
        session.scalars(
            select(AgentTask).where(AgentTask.batch_run_id == batch.id, AgentTask.status == STATUS_FAILED)
        )
    )
    if not failed_tasks:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="当前批次没有可重试的失败页")
    snapshots = {item.get("page_id"): item for item in (batch.input_snapshot_json or {}).get("pages") or [] if item.get("page_id")}
    page_action = BATCH_TO_PAGE_ACTION[batch.action_type]
    retry_pages: list[ProjectPage] = []
    upstream_changed = False
    for task in failed_tasks:
        if not task.page_id:
            continue
        page = session.get(ProjectPage, task.page_id)
        if page is None or page.project_id != project_id:
            continue
        snapshot = snapshots.get(page.id) or {}
        if snapshot and not page_snapshot_matches(session, page, snapshot, batch.action_type):
            upstream_changed = True
        if not _batch_page_eligible(page, batch.action_type):
            continue
        retry_pages.append(page)
    if not retry_pages:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="失败页已不可重试或输入已变为可跳过状态")
    all_pages = list(
        session.scalars(select(ProjectPage).where(ProjectPage.project_id == project_id).order_by(ProjectPage.sort_order.asc()))
    )
    eligible_ids = {page.id for page in retry_pages}
    new_batch = create_batch_run(
        session,
        project_id=project_id,
        action_type=batch.action_type,
        agent_run_id=agent_run_id,
        pages=all_pages,
        eligible_ids=eligible_ids,
        parent_batch_run_id=batch.id,
        retry_reason="upstream_changed" if upstream_changed else "retry_failed",
    )
    created: list[AgentTask] = []
    for page in retry_pages:
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
                    batch_run_id=new_batch.id,
                )
            )
        except HTTPException as exc:
            if exc.status_code != status.HTTP_409_CONFLICT:
                raise
    new_batch.queued_count = len(created)
    new_batch.skipped_count = max(0, len(all_pages) - len(created))
    new_batch.expected_count = len(eligible_ids)
    session.flush()
    refresh_batch_run(session, new_batch.id)
    append_event(
        session,
        project_id=project_id,
        event_type="task.batch_queued",
        stage=batch.action_type,
        scope_type="project",
        payload={
            "action_type": batch.action_type,
            "queued": len(created),
            "agent_run_id": agent_run_id,
            "batch_run_id": new_batch.id,
            "parent_batch_run_id": batch.id,
            "retry_reason": new_batch.retry_reason,
        },
        agent_run_id=agent_run_id,
    )
    return new_batch


def _compare_keys_for_action(action_type: str) -> tuple[str, ...]:
    if action_type == "project_batch_search":
        return ("digest_document_count", "search_status")
    if action_type == "project_batch_summary":
        return ("digest_document_count", "digest_content_chars", "current_brief_version_id")
    if action_type == "project_batch_draft":
        return ("current_brief_version_id", "summary_hash")
    return ("current_draft_version_id", "draft_svg_hash")
