from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.services.clarification import (
    CLARIFICATION_CODES,
    apply_working_title_to_outline,
    merge_clarification_questions,
    placeholder_project_title,
    project_title_from_answers,
    topic_from_request,
)
from tests.helpers import make_project


def test_placeholder_title_strips_request_voice():
    assert placeholder_project_title("请帮我生成一份约 12 页的咨询风 PPT") == "咨询风"
    assert placeholder_project_title("2026年北京五日深度游全攻略") == "2026年北京五日深度游全攻略"
    assert placeholder_project_title("请生成PPT") == "未命名项目"


def test_topic_from_request_drops_page_count_and_deck_words():
    assert "约" not in topic_from_request("帮我做一份约 8 页的北京旅游PPT")
    assert "PPT" not in topic_from_request("帮我做一份北京旅游PPT")


def test_merge_always_injects_four_clarification_questions():
    merged = merge_clarification_questions(
        [
            {
                "question_code": "audience_focus",
                "label": "这份 PPT 更要打动谁",
                "description": "受众",
                "options": [
                    {"option_code": "A", "label": "管理层"},
                    {"option_code": "B", "label": "业务"},
                    {"option_code": "C", "label": "执行"},
                ],
                "allow_custom": True,
            },
            {
                "question_code": "depth_focus",
                "label": "更想强调哪一类信息",
                "description": "主题深度",
                "options": [
                    {"option_code": "A", "label": "规则"},
                    {"option_code": "B", "label": "路线"},
                    {"option_code": "C", "label": "预算"},
                ],
                "allow_custom": True,
            },
            {
                "question_code": "extra_two",
                "label": "第二补充",
                "description": "补充",
                "options": [
                    {"option_code": "A", "label": "A"},
                    {"option_code": "B", "label": "B"},
                    {"option_code": "C", "label": "C"},
                ],
                "allow_custom": True,
            },
            {
                "question_code": "extra_three",
                "label": "不应保留",
                "description": "超出上限",
                "options": [
                    {"option_code": "A", "label": "A"},
                    {"option_code": "B", "label": "B"},
                    {"option_code": "C", "label": "C"},
                ],
                "allow_custom": True,
            },
        ],
        working_title_options=["请帮我生成标题", "2026年北京五日深度游全攻略", "京郊避峰攻略", "预约制旅行方案"],
        request_text="请帮我生成一份北京旅游PPT",
    )
    codes = [item["question_code"] for item in merged]
    assert codes[:4] == list(CLARIFICATION_CODES)
    assert "audience_focus" not in codes
    assert codes[4:] == ["depth_focus", "extra_two"]
    titles = [option["label"] for option in merged[3]["options"]]
    assert "请帮我生成标题" not in titles
    assert "2026年北京五日深度游全攻略" in titles


def test_merge_is_idempotent_and_keeps_existing_title_options():
    first = merge_clarification_questions(
        [],
        working_title_options=["封面标题甲", "封面标题乙", "封面标题丙"],
        request_text="随便写一份PPT",
    )
    second = merge_clarification_questions(first, request_text="随便写一份PPT", existing=first)
    assert [item["question_code"] for item in second] == list(CLARIFICATION_CODES)
    assert [option["label"] for option in second[3]["options"]] == ["封面标题甲", "封面标题乙", "封面标题丙"]


def test_project_title_from_answers_rejects_request_voice():
    assert project_title_from_answers({"working_title": "2026年北京五日深度游全攻略"}) == "2026年北京五日深度游全攻略"
    assert project_title_from_answers({"working_title": "请帮我生成一份约 12 页"}) is None


def test_apply_working_title_overwrites_cover():
    outline = apply_working_title_to_outline(
        {"ppt_outline": {"cover": {"title": "模型自己编的", "sub_title": "副标题"}}},
        {"working_title": "2026年北京五日深度游全攻略"},
    )
    assert outline["ppt_outline"]["cover"]["title"] == "2026年北京五日深度游全攻略"
    assert outline["ppt_outline"]["cover"]["sub_title"] == "副标题"


def test_patch_working_title_updates_project_title(service, db_session):
    project = make_project(db_session, stage="init")
    service.patch_requirement_answer(project.id, "working_title", "2026年北京五日深度游全攻略")
    db_session.refresh(project)
    assert project.title == "2026年北京五日深度游全攻略"


def test_cannot_delete_clarification_question(service, db_session):
    project = make_project(db_session, stage="init")
    with pytest.raises(HTTPException) as exc:
        service.delete_requirement_question(project.id, "audience")
    assert exc.value.status_code == 422


def test_outline_flow_uses_working_title_as_cover(service, db_session, monkeypatch):
    project = make_project(
        db_session,
        stage="init",
        answers={
            "page_count_target": 8,
            "style_preset": "minimalism",
            "q1": "已回答",
            "working_title": "2026年北京五日深度游全攻略",
        },
    )

    def fake_outline(**_kwargs):
        return {
            "ppt_outline": {
                "cover": {"title": "模型自己编的", "sub_title": "副标题", "content": []},
                "table_of_contents": {"title": "目录", "content": []},
                "parts": [],
                "end_page": {"title": "结束", "content": []},
            }
        }

    monkeypatch.setattr(service.generator, "generate_outline", fake_outline)
    service.run_outline_flow(project.id)
    db_session.refresh(project)
    assert project.title == "2026年北京五日深度游全攻略"
    outline = service._get_current_outline(project.id)
    assert outline.outline_json["ppt_outline"]["cover"]["title"] == "2026年北京五日深度游全攻略"
