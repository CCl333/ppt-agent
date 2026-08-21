from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.models.entities import OutlineVersion
from tests.helpers import make_content_page, make_project


def test_outline_uses_search_digest_not_chunks(service, db_session, monkeypatch):
    project = make_project(db_session, stage="init")
    captured: dict = {}

    def fake_outline(**kwargs):
        captured.update(kwargs)
        return {
            "ppt_outline": {
                "cover": {"title": "封面", "content": ["开场"]},
                "table_of_contents": {"title": "目录", "content": []},
                "parts": [],
                "end_page": {"title": "结束", "content": []},
            }
        }

    monkeypatch.setattr(service.generator, "generate_outline", fake_outline)

    def should_not_retrieve(**_kwargs):
        raise AssertionError("outline must not retrieve init_corpus chunks")

    monkeypatch.setattr(service.research, "retrieve_for_collection", should_not_retrieve)
    service.run_outline_flow(project.id)
    assert captured["context_digest"]
    assert captured["context_digest"][0]["excerpt_md"]
    db_session.refresh(project)
    assert project.current_stage == "outline"


def test_question_refine_without_corpus_uses_search_results(service, db_session, monkeypatch):
    project = make_project(db_session, stage="init", document_count=0)
    form = project.requirement_form
    form.init_search_results_json = [
        {"title": "摘要", "url": "https://example.com/a", "snippet": "背景调研摘要"}
    ]
    form.init_corpus_digest_json = {"document_count": 0}
    db_session.commit()
    captured: dict = {}

    def fake_refine(**kwargs):
        captured.update(kwargs)
        return {
            "ai_questions": [{"question_code": "q1", "label": "补充问题"}],
            "page_count_options": [],
        }

    monkeypatch.setattr(service.generator, "refine_init_questions_with_retrieval", fake_refine)
    monkeypatch.setattr(
        service.generator,
        "route_workspace_intent",
        lambda **_kwargs: {
            "scope_type": "project",
            "target_stage": "init",
            "target_page_id": None,
            "intent_type": "init_add_question",
            "action_type": "init_add_question",
            "should_execute": True,
            "needs_clarification": False,
            "requires_confirmation": False,
            "missing_data": [],
            "data_updates": {"question_patch": {"mode": "upsert", "question": {"question_code": "q2", "label": "新问题"}}},
            "execution_plan": [],
            "next_recommendations": [],
            "reason": "修订问题",
        },
    )
    message = service._add_message(
        project_id=project.id,
        role="user",
        stage="init",
        scope_type="project",
        target_page_id=None,
        content_md="增加一个受众问题",
        structured_payload_json={"ui_surface": "init"},
    )
    db_session.commit()
    service.run_message_flow(message.id)
    assert captured["context_digest"][0]["excerpt_md"] == "背景调研摘要"


def test_retry_outline_enqueues_when_generation_failed(service, db_session):
    from sqlalchemy import select

    from app.models.entities import AgentTask
    from app.services.tasks import STATUS_PENDING

    project = make_project(db_session, stage="outline")
    result = service.retry_outline(project.id)
    assert result["current_stage"] == "outline"
    tasks = list(db_session.scalars(select(AgentTask).where(AgentTask.project_id == project.id, AgentTask.task_type == "outline")))
    assert len(tasks) == 1
    assert tasks[0].status == STATUS_PENDING


def test_retry_outline_rejects_when_outline_exists(service, db_session):
    project = make_project(db_session, stage="outline")
    db_session.add(OutlineVersion(project_id=project.id, version_no=1, status="ready", outline_json={"ppt_outline": {}}))
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        service.retry_outline(project.id)
    assert exc.value.status_code == 409


def test_retry_outline_rejects_outside_outline(service, db_session):
    project = make_project(db_session, stage="init")
    with pytest.raises(HTTPException) as exc:
        service.retry_outline(project.id)
    assert exc.value.status_code == 409


def test_outline_confirm_advances_stage(service, db_session):
    project = make_project(db_session, stage="outline")
    db_session.add(
        OutlineVersion(project_id=project.id, version_no=1, status="ready", outline_json={"ppt_outline": {}})
    )
    db_session.commit()
    result = service.confirm_outline(project.id)
    db_session.refresh(project)
    assert project.current_stage == "search"
    assert result["current_stage"] == "search"


def test_page_search_blocked_before_outline_confirm(service, db_session):
    project = make_project(db_session, stage="outline")
    page = make_content_page(db_session, project, page_code="page-03", title="页", sort_order=1)
    with pytest.raises(HTTPException) as exc:
        service.queue_page_action(project.id, page.id, "page_search_run")
    assert exc.value.status_code == 409
    assert "确认大纲" in str(exc.value.detail)


def test_router_rejects_outline_confirm_outside_outline_surface(service, db_session, monkeypatch):
    project = make_project(db_session, stage="search")
    message = service._add_message(
        project_id=project.id,
        role="user",
        stage="search",
        scope_type="project",
        target_page_id=None,
        content_md="确认大纲",
        structured_payload_json={"ui_surface": "search"},
    )
    db_session.commit()
    monkeypatch.setattr(
        service.generator,
        "route_workspace_intent",
        lambda **_kwargs: {
            "scope_type": "project",
            "target_stage": "search",
            "target_page_id": None,
            "intent_type": "outline_confirm_to_search",
            "action_type": "outline_confirm_to_search",
            "should_execute": True,
            "needs_clarification": False,
            "requires_confirmation": False,
            "missing_data": [],
            "data_updates": {},
            "execution_plan": [],
            "next_recommendations": [],
            "reason": "确认大纲",
        },
    )
    called = {"n": 0}

    def should_not_advance(_project):
        called["n"] += 1

    monkeypatch.setattr(service, "_advance_outline_to_search", should_not_advance)
    service.run_message_flow(message.id)
    assert called["n"] == 0


def test_router_outline_confirm_advances_stage(service, db_session, monkeypatch):
    project = make_project(db_session, stage="outline")
    db_session.add(
        OutlineVersion(project_id=project.id, version_no=1, status="ready", outline_json={"ppt_outline": {}})
    )
    message = service._add_message(
        project_id=project.id,
        role="user",
        stage="outline",
        scope_type="project",
        target_page_id=None,
        content_md="确认大纲",
        structured_payload_json={"ui_surface": "outline"},
    )
    db_session.commit()
    monkeypatch.setattr(
        service.generator,
        "route_workspace_intent",
        lambda **_kwargs: {
            "scope_type": "project",
            "target_stage": "outline",
            "target_page_id": None,
            "intent_type": "outline_confirm_to_search",
            "action_type": "outline_confirm_to_search",
            "should_execute": True,
            "needs_clarification": False,
            "requires_confirmation": False,
            "missing_data": [],
            "data_updates": {},
            "execution_plan": [],
            "next_recommendations": [],
            "reason": "确认大纲",
        },
    )
    service.run_message_flow(message.id)
    db_session.refresh(project)
    assert project.current_stage == "search"


def test_retry_requirement_source_researches_snippets_not_corpus(service, db_session, monkeypatch):
    from app.models.entities import SourceCollection, SourceDocument

    project = make_project(db_session, stage="init")
    form = project.requirement_form
    form.init_search_results_json = [
        {
            "id": "src-old",
            "query_text": "q1",
            "query_purpose": "定义",
            "search_rank": 1,
            "title": "旧结果",
            "url": "https://example.com/old",
            "bocha_summary": "old",
            "snippet": "old",
        },
        {
            "id": "src-keep",
            "query_text": "q2",
            "query_purpose": "案例",
            "search_rank": 1,
            "title": "保留",
            "url": "https://example.com/keep",
            "bocha_summary": "keep",
            "snippet": "keep",
        },
    ]
    db_session.commit()
    reader_calls: list[str] = []
    monkeypatch.setattr(
        service.research.mcp,
        "read_url_markdown",
        lambda url: reader_calls.append(url),
    )
    monkeypatch.setattr(
        service.research,
        "search_query_summaries",
        lambda query_plan, limit_per_query=3, on_query_completed=None, **_kwargs: [
            {
                "id": "src-digest",
                "query_text": "q1",
                "query_purpose": "定义",
                "search_rank": 1,
                "title": "整理稿",
                "url": "llm-search://digest/q1",
                "bocha_summary": "新整理稿",
                "snippet": "新整理稿",
                "content_excerpt_md": "新整理稿",
                "source_kind": "llm_answer",
            }
        ],
    )
    service.retry_requirement_source(project.id, "src-old")
    db_session.refresh(form)
    titles = [item["title"] for item in form.init_search_results_json]
    assert reader_calls == []
    assert "整理稿" in titles
    assert "保留" in titles
    assert "旧结果" not in titles
    assert db_session.query(SourceCollection).filter(SourceCollection.collection_type == "init_knowledge").count() == 0
    assert db_session.query(SourceDocument).count() == 0
