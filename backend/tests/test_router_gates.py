from __future__ import annotations

from tests.helpers import make_content_page, make_project


def test_message_flow_skips_gated_action_when_should_execute_false(service, db_session, monkeypatch):
    project = make_project(db_session, stage="search")
    page = make_content_page(db_session, project, page_code="page-03", title="A", sort_order=1)
    message = service._add_message(
        project_id=project.id,
        role="user",
        stage="search",
        scope_type="page",
        target_page_id=page.id,
        content_md="帮我搜索",
        structured_payload_json={"ui_surface": "search"},
    )
    db_session.commit()
    ran = {"n": 0}

    def fake_route(**_kwargs):
        return {
            "scope_type": "page",
            "target_stage": "search",
            "target_page_id": page.id,
            "intent_type": "page_search_run",
            "action_type": "page_search_run",
            "should_execute": False,
            "needs_clarification": False,
            "requires_confirmation": False,
            "missing_data": [],
            "data_updates": {},
            "execution_plan": [],
            "next_recommendations": [],
            "reason": "建议先确认再搜索",
        }

    monkeypatch.setattr(service.generator, "route_workspace_intent", fake_route)

    def should_not_search(**_kwargs):
        ran["n"] += 1

    monkeypatch.setattr(service, "_run_page_search", should_not_search)
    service.run_message_flow(message.id)
    assert ran["n"] == 0


def test_outline_generate_action_has_executor(service, db_session, monkeypatch):
    project = make_project(db_session, stage="init")
    message = service._add_message(
        project_id=project.id,
        role="user",
        stage="init",
        scope_type="project",
        target_page_id=None,
        content_md="生成大纲",
        structured_payload_json={"ui_surface": "init"},
    )
    db_session.commit()
    called = {"n": 0}

    def fake_route(**_kwargs):
        return {
            "scope_type": "project",
            "target_stage": "outline",
            "target_page_id": None,
            "intent_type": "outline_generate",
            "action_type": "outline_generate",
            "should_execute": True,
            "needs_clarification": False,
            "requires_confirmation": False,
            "missing_data": [],
            "data_updates": {},
            "execution_plan": [],
            "next_recommendations": [],
            "reason": "固定项已齐",
        }

    def fake_outline(_project_id):
        called["n"] += 1

    monkeypatch.setattr(service.generator, "route_workspace_intent", fake_route)
    monkeypatch.setattr(service, "run_outline_flow", fake_outline)
    service.run_message_flow(message.id)
    assert called["n"] == 1
