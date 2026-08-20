from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.models.entities import ProjectPage
from app.services.research import ResearchService
from tests.helpers import add_page_chunk, make_content_page, make_project


def test_title_change_marks_current_page_stale_only(service, db_session):
    project = make_project(db_session, stage="search")
    page_a = make_content_page(db_session, project, page_code="page-03", title="原标题 A", sort_order=1)
    page_b = make_content_page(db_session, project, page_code="page-04", title="原标题 B", sort_order=2)

    service.patch_page_outline(
        project.id,
        page_a.id,
        {"title": "新标题 A", "content_outline": ["新要点"], "section_title": "章节一"},
    )

    db_session.expire_all()
    updated_a = db_session.get(ProjectPage, page_a.id)
    updated_b = db_session.get(ProjectPage, page_b.id)
    assert updated_a.summary_status == "stale"
    assert updated_a.draft_status == "stale"
    assert updated_a.design_status == "stale"
    assert updated_a.current_draft_version_id == page_a.current_draft_version_id
    assert updated_a.current_design_version_id == page_a.current_design_version_id
    assert updated_b.summary_status == "ready"
    assert updated_b.draft_status == "ready"
    assert updated_b.design_status == "ready"


def test_requirement_form_missing_page_count_returns_422(service, db_session):
    project = make_project(db_session, stage="init", page_count_target=None)
    form = project.requirement_form
    form.answers_json = {"style_preset": "minimalism", "q1": "已回答"}
    with pytest.raises(HTTPException) as exc:
        service._validate_requirement_form(project, form)
    assert exc.value.status_code == 422
    assert "页数目标未填写" in str(exc.value.detail)


def test_requirement_form_missing_style_returns_422(service, db_session):
    project = make_project(db_session, stage="init", style_preset=None)
    form = project.requirement_form
    form.answers_json = {"page_count_target": 8, "q1": "已回答"}
    with pytest.raises(HTTPException) as exc:
        service._validate_requirement_form(project, form)
    assert exc.value.status_code == 422
    assert "风格预设未选择" in str(exc.value.detail)


def test_requirement_form_unanswered_question_returns_422(service, db_session):
    project = make_project(db_session, stage="init")
    form = project.requirement_form
    form.answers_json = {"page_count_target": 8, "style_preset": "minimalism"}
    with pytest.raises(HTTPException) as exc:
        service._validate_requirement_form(project, form)
    assert exc.value.status_code == 422
    assert "补充问题未完成" in str(exc.value.detail)


def test_requirement_form_gate_accepts_search_results_without_corpus(service, db_session):
    project = make_project(db_session, stage="init", document_count=0)
    form = project.requirement_form
    form.init_search_results_json = [
        {
            "title": "摘要来源",
            "url": "https://example.com/a",
            "snippet": "可用于大纲的搜索摘要",
        }
    ]
    form.init_corpus_digest_json = {"document_count": 0}
    service._validate_requirement_form(project, form)


def test_requirement_form_missing_search_results_returns_422(service, db_session):
    project = make_project(db_session, stage="init", document_count=0)
    form = project.requirement_form
    form.init_search_results_json = []
    with pytest.raises(HTTPException) as exc:
        service._validate_requirement_form(project, form)
    assert exc.value.status_code == 422
    assert "首轮搜索结果为空" in str(exc.value.detail)


def test_page_b_evidence_excludes_page_a_chunks(db_session):
    project = make_project(db_session, stage="search")
    page_a = make_content_page(db_session, project, page_code="page-03", title="页面 A", sort_order=1)
    page_b = make_content_page(db_session, project, page_code="page-04", title="页面 B", sort_order=2)
    chunk_a = add_page_chunk(
        db_session,
        project,
        page_a,
        uri="https://example.com/a",
        title="A 文档",
        content="AlphaSecretToken evidence exclusive to page A",
    )
    chunk_b = add_page_chunk(
        db_session,
        project,
        page_b,
        uri="https://example.com/b",
        title="B 文档",
        content="BetaSecretToken evidence exclusive to page B",
    )
    research = ResearchService(db_session)
    collection_b = research.get_or_create_page_collection(project, page_b)
    session = research.create_session(
        project_id=project.id,
        page_id=page_b.id,
        scope_type="page",
        session_role="page_summary",
        research_goal="验证跨页隔离",
        query_plan=[{"query_text": "AlphaSecretToken BetaSecretToken", "query_purpose": "test"}],
        context_snapshot={},
    )

    evidence = research.retrieve_for_collection(
        project=project,
        collection=collection_b,
        research_session=session,
        query_plan=[{"query_text": "AlphaSecretToken BetaSecretToken", "query_purpose": "test"}],
        limit=20,
    )

    evidence_text = " ".join(str(item.get("excerpt_md") or "") for item in evidence)
    selected_chunk_ids = {item.get("chunk_id") for item in (session.selected_citations_json or [])}
    assert chunk_a.id not in selected_chunk_ids
    assert "AlphaSecretToken" not in evidence_text
    assert chunk_b.id in selected_chunk_ids
    assert "BetaSecretToken" in evidence_text
