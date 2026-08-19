from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from fastapi import HTTPException

from app.models.base import now_utc
from app.models.entities import AgentTask
from app.services.tasks import (
    STATUS_CANCELED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_SUCCESS,
    cancel_tasks,
    enqueue_page_action,
    reclaim_expired_tasks,
    run_once,
)
from tests.helpers import make_content_page, make_project


def test_duplicate_page_task_is_rejected(service, db_session):
    project = make_project(db_session, stage="search")
    page = make_content_page(db_session, project, page_code="page-03", title="A", sort_order=1)
    first = service.queue_page_action(project.id, page.id, "page_draft_generate")
    assert first["status"] == "queued"
    try:
        service.queue_page_action(project.id, page.id, "page_draft_generate")
        raise AssertionError("expected 409")
    except HTTPException as exc:
        assert exc.status_code == 409


def test_failed_page_task_retries_then_marks_failed(db_session, monkeypatch):
    project = make_project(db_session, stage="draft")
    page = make_content_page(db_session, project, page_code="page-03", title="A", sort_order=1)
    attempts = {"n": 0}

    def boom(*_args, **_kwargs):
        attempts["n"] += 1
        raise RuntimeError("bad key")

    monkeypatch.setattr("app.services.orchestrator.PptAgentService.run_page_action_flow", boom)
    enqueue_page_action(
        db_session,
        project_id=project.id,
        page_id=page.id,
        action_type="page_design_generate",
        agent_run_id="run-1",
        priority=100,
    )
    db_session.commit()

    for _ in range(3):
        task = db_session.scalars(select(AgentTask)).one()
        task.next_run_at = now_utc()
        db_session.commit()
        assert run_once() is True

    db_session.expire_all()
    task = db_session.scalars(select(AgentTask)).one()
    assert attempts["n"] == 3
    assert task.retry_num == 3
    assert task.status == STATUS_FAILED


def test_cancel_pending_task_stops_before_run(db_session, monkeypatch):
    project = make_project(db_session, stage="draft")
    page = make_content_page(db_session, project, page_code="page-03", title="A", sort_order=1)
    ran = {"n": 0}

    def should_not_run(*_args, **_kwargs):
        ran["n"] += 1

    monkeypatch.setattr("app.services.orchestrator.PptAgentService.run_page_action_flow", should_not_run)
    enqueue_page_action(
        db_session,
        project_id=project.id,
        page_id=page.id,
        action_type="page_draft_generate",
        agent_run_id="run-2",
    )
    db_session.commit()
    canceled = cancel_tasks(db_session, project_id=project.id, page_id=page.id)
    db_session.commit()
    assert canceled == 1
    assert run_once() is False
    db_session.expire_all()
    task = db_session.scalars(select(AgentTask)).one()
    assert task.status == STATUS_CANCELED
    assert ran["n"] == 0


def test_expired_lease_is_reclaimed_as_pending(db_session):
    project = make_project(db_session, stage="design")
    page = make_content_page(db_session, project, page_code="page-03", title="A", sort_order=1)
    task = enqueue_page_action(
        db_session,
        project_id=project.id,
        page_id=page.id,
        action_type="page_design_generate",
        agent_run_id="run-3",
    )
    task.status = STATUS_PROCESSING
    task.lease_expires_at = now_utc() - timedelta(seconds=10)
    page.design_status = "running"
    db_session.commit()

    recovered = reclaim_expired_tasks(db_session)
    db_session.commit()
    db_session.expire_all()
    task = db_session.get(AgentTask, task.task_id)
    assert recovered == 1
    assert task.status == STATUS_PENDING
    assert task.retry_num == 1


def test_reclaim_does_not_rerun_successful_tasks(db_session):
    project = make_project(db_session, stage="design")
    page = make_content_page(db_session, project, page_code="page-03", title="A", sort_order=1)
    task = enqueue_page_action(
        db_session,
        project_id=project.id,
        page_id=page.id,
        action_type="page_design_generate",
        agent_run_id="run-4",
    )
    task.status = STATUS_SUCCESS
    task.task_stage = "done"
    db_session.commit()
    recovered = reclaim_expired_tasks(db_session)
    db_session.commit()
    db_session.expire_all()
    assert recovered == 0
    assert db_session.get(AgentTask, task.task_id).status == STATUS_SUCCESS
