from __future__ import annotations

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Delete

from app.core.config import get_settings
from app.models.entities import Project, ProjectPage, RequirementForm
from tests.helpers import make_content_page, make_project


def test_delete_project_removes_row_and_files(client, db_session):
    project = make_project(db_session, stage="search")
    page = make_content_page(db_session, project, page_code="page-03", title="内容页", sort_order=1)
    project_id = project.id
    page_id = page.id
    form_id = project.requirement_form.id
    settings = get_settings()
    background = settings.background_path / f"{project_id}.png"
    export_file = settings.export_path / f"{project_id}.pptx"
    background.write_bytes(b"png")
    export_file.write_bytes(b"pptx")
    project.background_asset_path = str(background)
    db_session.commit()

    response = client.delete(f"/api/v1/projects/{project_id}")
    assert response.status_code == 200
    assert response.json() == {"status": "deleted", "project_id": project_id}
    assert client.get(f"/api/v1/projects/{project_id}").status_code == 404
    listed = client.get("/api/v1/projects").json()["items"]
    assert all(item["project_id"] != project_id for item in listed)

    db_session.expire_all()
    assert db_session.get(Project, project_id) is None
    assert db_session.get(ProjectPage, page_id) is None
    assert db_session.get(RequirementForm, form_id) is None
    assert background.exists() is False
    assert export_file.exists() is False


def test_delete_missing_project_returns_404(client):
    response = client.delete("/api/v1/projects/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


def test_delete_project_succeeds_when_cancel_is_locked(client, db_session, monkeypatch):
    project = make_project(db_session, stage="search")
    project_id = project.id

    def locked_cancel(*_args, **_kwargs):
        raise OperationalError("UPDATE", {}, Exception("database is locked"))

    monkeypatch.setattr("app.services.orchestrator.request_task_cancel", locked_cancel)
    response = client.delete(f"/api/v1/projects/{project_id}")
    assert response.status_code == 200
    assert response.json() == {"status": "deleted", "project_id": project_id}
    db_session.expire_all()
    assert db_session.get(Project, project_id) is None


def test_delete_project_retries_when_row_delete_is_locked(client, db_session, monkeypatch):
    project = make_project(db_session, stage="search")
    project_id = project.id
    original_execute = Session.execute
    attempts = {"count": 0}

    def execute_with_one_lock(self, statement, *args, **kwargs):
        if isinstance(statement, Delete) and getattr(statement.table, "name", None) == "projects":
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise OperationalError(statement, {}, Exception("database is locked"))
        return original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", execute_with_one_lock)
    monkeypatch.setattr("app.services.orchestrator.time.sleep", lambda *_args, **_kwargs: None)
    response = client.delete(f"/api/v1/projects/{project_id}")
    assert response.status_code == 200
    assert attempts["count"] >= 2
    db_session.expire_all()
    assert db_session.get(Project, project_id) is None


def test_delete_project_returns_conflict_when_still_locked(client, db_session, monkeypatch):
    project = make_project(db_session, stage="search")
    original_execute = Session.execute

    def always_locked(self, statement, *args, **kwargs):
        if isinstance(statement, Delete) and getattr(statement.table, "name", None) == "projects":
            raise OperationalError(statement, {}, Exception("database is locked"))
        return original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", always_locked)
    monkeypatch.setattr("app.services.orchestrator.time.sleep", lambda *_args, **_kwargs: None)
    response = client.delete(f"/api/v1/projects/{project.id}")
    assert response.status_code == 409
    assert "暂时无法删除" in response.json()["detail"]
