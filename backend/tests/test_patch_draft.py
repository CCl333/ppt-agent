from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.models.entities import DraftVersion
from tests.helpers import make_content_page, make_project

VALID_SVG = '<svg viewBox="0 0 1280 720"><text x="80" y="120">改后的策划稿</text></svg>'


def test_patch_page_draft_creates_new_version_and_marks_design_stale(service, db_session):
    project = make_project(db_session, stage="draft")
    page = make_content_page(db_session, project, page_code="page-03", title="策划页", sort_order=1)
    previous_id = page.current_draft_version_id
    assert previous_id
    assert page.design_status == "ready"

    result = service.patch_page_draft(project.id, page.id, VALID_SVG)
    db_session.refresh(page)

    assert page.current_draft_version_id != previous_id
    assert page.draft_status == "ready"
    assert page.design_status == "stale"
    assert result["draft"]["draft_version_id"] == page.current_draft_version_id
    assert "改后的策划稿" in (page.current_draft_version_id and result["draft"]["draft_svg_markup"] or "")


def test_patch_page_draft_rejects_invalid_svg(service, db_session):
    project = make_project(db_session, stage="draft")
    page = make_content_page(db_session, project, page_code="page-03", title="策划页", sort_order=1)
    previous_id = page.current_draft_version_id

    with pytest.raises(HTTPException) as exc:
        service.patch_page_draft(project.id, page.id, "这不是 SVG")
    assert exc.value.status_code == 422
    db_session.refresh(page)
    assert page.current_draft_version_id == previous_id
    assert page.design_status == "ready"


def test_patch_page_draft_keeps_previous_version_retrievable(service, db_session):
    project = make_project(db_session, stage="draft")
    page = make_content_page(db_session, project, page_code="page-03", title="策划页", sort_order=1)
    previous_id = page.current_draft_version_id
    previous = db_session.get(DraftVersion, previous_id)
    assert previous is not None
    previous_markup = previous.draft_svg_markup

    service.patch_page_draft(project.id, page.id, VALID_SVG)
    db_session.refresh(page)
    stored_previous = db_session.get(DraftVersion, previous_id)
    assert stored_previous is not None
    assert stored_previous.draft_svg_markup == previous_markup
    assert stored_previous.id != page.current_draft_version_id
    current = db_session.get(DraftVersion, page.current_draft_version_id)
    assert current is not None
    assert current.version_no == previous.version_no + 1


def test_patch_page_draft_rejects_missing_plan_label(service, db_session):
    project = make_project(db_session, stage="draft")
    page = make_content_page(db_session, project, page_code="page-03", title="策划页", sort_order=1)
    previous = db_session.get(DraftVersion, page.current_draft_version_id)
    assert previous is not None
    previous.content_plan_json = {
        "page_code": "page-03",
        "title": "策划页",
        "subtitle": "",
        "badge": "",
        "blocks": [{"role": "point", "label": "要点一", "note": "解释"}],
        "footer": {},
    }
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        service.patch_page_draft(project.id, page.id, VALID_SVG)
    assert exc.value.status_code == 422
    db_session.refresh(page)
    assert page.current_draft_version_id == previous.id


def test_patch_page_draft_allows_extra_text_when_skeleton_present(service, db_session):
    project = make_project(db_session, stage="draft")
    page = make_content_page(db_session, project, page_code="page-03", title="策划页", sort_order=1)
    previous = db_session.get(DraftVersion, page.current_draft_version_id)
    assert previous is not None
    previous.content_plan_json = {
        "page_code": "page-03",
        "title": "策划页",
        "subtitle": "",
        "badge": "",
        "blocks": [{"role": "point", "label": "要点一", "note": "解释"}],
        "footer": {},
    }
    db_session.commit()

    svg = '<svg viewBox="0 0 1280 720"><text x="80" y="120">策划页</text><text x="80" y="180">要点一</text><text x="80" y="240">100%</text></svg>'
    result = service.patch_page_draft(project.id, page.id, svg)
    assert "100%" in result["draft"]["draft_svg_markup"]
