from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.models.entities import DesignVersion, DraftVersion, Project, ProjectPage
from app.services.prompt_contracts import PROMPT_TEXTS
from tests.helpers import make_content_page, make_project


def test_confirm_requirements_rejects_design_stage_and_keeps_pages(client, db_session):
    project = make_project(db_session, stage="design")
    page = make_content_page(db_session, project, page_code="page-03", title="保留页", sort_order=1)
    draft_id = page.current_draft_version_id
    design_id = page.current_design_version_id

    response = client.post(f"/api/v1/projects/{project.id}/requirements/confirm", json={})

    assert response.status_code == 409
    assert "后续阶段" in response.json()["detail"]
    db_session.expire_all()
    assert db_session.get(Project, project.id).current_stage == "design"
    assert db_session.get(ProjectPage, page.id) is not None
    assert db_session.get(DraftVersion, draft_id) is not None
    assert db_session.get(DesignVersion, design_id) is not None


def test_confirm_requirements_rejects_search_stage(service, db_session):
    project = make_project(db_session, stage="search")
    with pytest.raises(HTTPException) as exc:
        service.confirm_requirements(project.id)
    assert exc.value.status_code == 409
    db_session.expire_all()
    assert db_session.get(Project, project.id).current_stage == "search"


def test_router_prompt_limits_confirm_to_init_surface():
    prompt = PROMPT_TEXTS["workspace.intent_router.system"]
    assert "init_confirm_to_outline" in prompt
    assert "仅当 `ui_surface` 为 `init`" in prompt
    assert "outline_confirm_to_search" in prompt
    assert "仅当 `ui_surface` 为 `outline`" in prompt
