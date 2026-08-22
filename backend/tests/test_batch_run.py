from __future__ import annotations

from sqlalchemy import select

from app.models.entities import AgentTask, BatchRun
from app.services.batch_run import refresh_batch_run
from app.services.tasks import (
    STATUS_CANCELED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SUCCESS,
    enqueue_batch_action,
)
from tests.helpers import make_content_page, make_project


def test_batch_enqueue_creates_parent_and_does_not_treat_queue_as_done(db_session):
    project = make_project(db_session, stage="design")
    make_content_page(db_session, project, page_code="p1", title="A", sort_order=1, with_design=False)
    make_content_page(db_session, project, page_code="p2", title="B", sort_order=2, with_design=False)
    db_session.commit()

    tasks = enqueue_batch_action(
        db_session,
        project_id=project.id,
        action_type="project_batch_design",
        agent_run_id="run-batch-1",
    )
    db_session.commit()
    assert len(tasks) == 2
    batch = db_session.get(BatchRun, tasks[0].batch_run_id)
    assert batch is not None
    assert batch.status == "queued"
    assert batch.expected_count == 2
    assert batch.queued_count == 2
    assert batch.success_count == 0
    assert batch.finished_at is None


def test_batch_aggregates_partial_success(db_session):
    project = make_project(db_session, stage="design")
    make_content_page(db_session, project, page_code="p1", title="A", sort_order=1, with_design=False)
    make_content_page(db_session, project, page_code="p2", title="B", sort_order=2, with_design=False)
    db_session.commit()
    tasks = enqueue_batch_action(
        db_session,
        project_id=project.id,
        action_type="project_batch_design",
        agent_run_id="run-batch-2",
    )
    tasks[0].status = STATUS_SUCCESS
    tasks[1].status = STATUS_FAILED
    tasks[1].task_context = {**(tasks[1].task_context or {}), "last_error_code": "QUALITY_GATE_FAILED"}
    db_session.flush()
    batch = refresh_batch_run(db_session, tasks[0].batch_run_id)
    assert batch.status == "partial_success"
    assert batch.success_count == 1
    assert batch.failed_count == 1
    assert batch.failure_summary_json["QUALITY_GATE_FAILED"] == 1
    assert batch.finished_at is not None


def test_batch_all_failed_and_cancel(db_session):
    project = make_project(db_session, stage="draft")
    make_content_page(db_session, project, page_code="p1", title="A", sort_order=1, with_draft=False, with_design=False)
    make_content_page(db_session, project, page_code="p2", title="B", sort_order=2, with_draft=False, with_design=False)
    db_session.commit()
    tasks = enqueue_batch_action(
        db_session,
        project_id=project.id,
        action_type="project_batch_draft",
        agent_run_id="run-batch-3",
    )
    for task in tasks:
        task.status = STATUS_FAILED
    batch = refresh_batch_run(db_session, tasks[0].batch_run_id)
    assert batch.status == "failed"

    for task in tasks:
        task.status = STATUS_CANCELED
    batch = refresh_batch_run(db_session, tasks[0].batch_run_id)
    assert batch.status == "canceled"


def test_batch_api_returns_batch_run(client, db_session):
    project = make_project(db_session, stage="design")
    page = make_content_page(db_session, project, page_code="p1", title="A", sort_order=1, with_design=False)
    db_session.commit()
    response = client.post(
        f"/api/v1/projects/{project.id}/actions/batch",
        json={"action_type": "project_batch_design"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "queued"
    assert payload["batch_run_id"]
    assert payload["queued_count"] == 1
    assert payload["success_count"] == 0
    fetched = client.get(f"/api/v1/projects/{project.id}/batches/{payload['batch_run_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["task_ids"]
    assert fetched.json()["tasks"][0]["page_id"] == page.id


def test_retry_failed_only_retries_failed_pages(client, db_session):
    project = make_project(db_session, stage="design")
    ready = make_content_page(db_session, project, page_code="p1", title="成功页", sort_order=1, with_design=False)
    failed = make_content_page(db_session, project, page_code="p2", title="失败页", sort_order=2, with_design=False)
    db_session.commit()
    tasks = enqueue_batch_action(
        db_session,
        project_id=project.id,
        action_type="project_batch_design",
        agent_run_id="run-batch-4",
    )
    by_page = {task.page_id: task for task in tasks}
    by_page[ready.id].status = STATUS_SUCCESS
    by_page[failed.id].status = STATUS_FAILED
    refresh_batch_run(db_session, tasks[0].batch_run_id)
    db_session.commit()

    response = client.post(f"/api/v1/projects/{project.id}/batches/{tasks[0].batch_run_id}:retry-failed")
    assert response.status_code == 200, response.text
    payload = response.json()
    db_session.expire_all()
    assert payload["batch_run_id"] != tasks[0].batch_run_id
    assert payload["parent_batch_run_id"] == tasks[0].batch_run_id
    assert payload["retry_reason"] == "retry_failed"
    assert payload["queued_count"] == 1
    assert payload["tasks"][0]["page_id"] == failed.id
    pending = list(
        db_session.scalars(
            select(AgentTask).where(AgentTask.batch_run_id == payload["batch_run_id"], AgentTask.status == STATUS_PENDING)
        )
    )
    assert [item.page_id for item in pending] == [failed.id]


def test_retry_failed_marks_upstream_changed(client, db_session):
    project = make_project(db_session, stage="design")
    page = make_content_page(db_session, project, page_code="p1", title="失败页", sort_order=1, with_design=False)
    db_session.commit()
    tasks = enqueue_batch_action(
        db_session,
        project_id=project.id,
        action_type="project_batch_design",
        agent_run_id="run-batch-5",
    )
    tasks[0].status = STATUS_FAILED
    refresh_batch_run(db_session, tasks[0].batch_run_id)
    page.current_draft_version_id = "changed-draft"
    db_session.commit()

    response = client.post(f"/api/v1/projects/{project.id}/batches/{tasks[0].batch_run_id}:retry-failed")
    assert response.status_code == 200, response.text
    assert response.json()["retry_reason"] == "upstream_changed"
