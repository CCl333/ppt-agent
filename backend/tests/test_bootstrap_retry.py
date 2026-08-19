from __future__ import annotations

from sqlalchemy import select
from fastapi import HTTPException

from app.models.entities import AgentTask
from app.services.tasks import STATUS_PENDING
from tests.helpers import make_project


def test_retry_bootstrap_enqueues_when_init_failed(service, db_session):
    project = make_project(db_session, stage="init")
    project.requirement_form.status = "failed"
    db_session.commit()

    result = service.retry_bootstrap(project.id)
    assert result["project_id"] == project.id
    db_session.refresh(project.requirement_form)
    assert project.requirement_form.status == "running"
    tasks = list(db_session.scalars(select(AgentTask).where(AgentTask.project_id == project.id)))
    assert len(tasks) == 1
    assert tasks[0].task_type == "bootstrap"
    assert tasks[0].status == STATUS_PENDING


def test_retry_bootstrap_rejects_outside_init(service, db_session):
    project = make_project(db_session, stage="search")
    try:
        service.retry_bootstrap(project.id)
        raise AssertionError("expected 409")
    except HTTPException as exc:
        assert exc.status_code == 409
