from __future__ import annotations

import pytest

from app.services.content_plan import (
    assert_svg_matches_plan,
    normalize_content_plan,
)
from app.services.prompt_contracts import get_prompt_text


COVER_PLAN = {
    "page_code": "cover",
    "title": "2026年北京五日深度游全攻略",
    "subtitle": "智慧预约、时空避峰与文化沉淀的完美旅行方案",
    "badge": "2026年度权威更新版",
    "blocks": [
        {"role": "value_point", "label": "预约制深度解析", "note": "全景点预约全流程覆盖"},
        {"role": "value_point", "label": "大数据避峰策略", "note": "科学规划最优游览路径"},
        {"role": "value_point", "label": "1500元性价比模型", "note": "精细化预算与品质平衡"},
    ],
    "footer": {"presenter": "[您的姓名/团队名称]", "date": "202X年X月X日"},
}

CONTENT_BLOCKS = [
    {"label": "预约现状", "note": "先看放票窗口"},
    {"label": "抢票 SOP", "note": "三步完成预约"},
    {"label": "行程清单", "note": "证件与交通"},
]


def _content_payload(**overrides):
    payload = {
        "title": "出发前要做的事",
        "subtitle": "把预约和行程锁死",
        "blocks": [dict(item) for item in CONTENT_BLOCKS],
        "footer": {"presenter": "张三", "date": "2026-08-01"},
    }
    payload.update(overrides)
    return payload


def test_normalize_rejects_empty_content_blocks():
    with pytest.raises(RuntimeError, match="blocks"):
        normalize_content_plan(
            {"title": "实践路径", "blocks": []},
            page_code="page-03",
            title="实践路径",
            page_role="content",
        )


def test_normalize_cover_requires_copy():
    with pytest.raises(RuntimeError, match="subtitle"):
        normalize_content_plan(
            {"title": "封面"},
            page_code="cover",
            title="封面",
            page_role="cover",
        )


def test_normalize_rejects_long_label():
    with pytest.raises(RuntimeError, match="label 超过 22 字"):
        normalize_content_plan(
            _content_payload(
                blocks=[
                    {"label": "这是一个明显超过二十二个汉字限制的卡片标题内容", "note": "短解释"},
                    CONTENT_BLOCKS[1],
                    CONTENT_BLOCKS[2],
                ]
            ),
            page_code="page-04",
            title="出发前要做的事",
            page_role="content",
        )


def test_normalize_rejects_long_note():
    with pytest.raises(RuntimeError, match="note 超过 22 字"):
        normalize_content_plan(
            _content_payload(
                blocks=[
                    {"label": "预约现状", "note": "这是一句明显超过二十二个汉字限制的解释文案内容"},
                    CONTENT_BLOCKS[1],
                    CONTENT_BLOCKS[2],
                ]
            ),
            page_code="page-04",
            title="出发前要做的事",
            page_role="content",
        )


def test_normalize_rejects_too_few_content_blocks():
    with pytest.raises(RuntimeError, match="3～5"):
        normalize_content_plan(
            _content_payload(blocks=CONTENT_BLOCKS[:2]),
            page_code="page-04",
            title="出发前要做的事",
            page_role="content",
        )


def test_normalize_rejects_too_many_content_blocks():
    extra = {"label": "额外卡片", "note": "超出上限"}
    with pytest.raises(RuntimeError, match="3～5"):
        normalize_content_plan(
            _content_payload(blocks=CONTENT_BLOCKS + [extra, extra, extra]),
            page_code="page-04",
            title="出发前要做的事",
            page_role="content",
        )


def test_normalize_drops_footer_on_content_pages():
    plan = normalize_content_plan(
        _content_payload(),
        page_code="page-04",
        title="出发前要做的事",
        page_role="content",
    )
    assert plan["footer"] == {}


def test_cover_plan_fixture_still_normalizes():
    plan = normalize_content_plan(COVER_PLAN, page_code="cover", title=COVER_PLAN["title"], page_role="cover")
    assert plan["title"] == COVER_PLAN["title"]
    assert plan["footer"]["presenter"] == COVER_PLAN["footer"]["presenter"]
    assert len(plan["blocks"]) == 3


def test_normalize_allows_catalog_without_slots():
    plan = normalize_content_plan(
        _content_payload(),
        page_code="page-04",
        title="出发前要做的事",
        page_role="content",
        page_images=[{"image_id": "IMG-1", "caption": "故宫"}],
    )
    assert plan["image_slots"] == []


def test_svg_matches_plan_and_ignores_page_number():
    plan = normalize_content_plan(COVER_PLAN, page_code="cover", title=COVER_PLAN["title"], page_role="cover")
    svg = f"""
    <svg viewBox="0 0 1280 720">
      <text>{plan['title']}</text>
      <text>{plan['subtitle']}</text>
      <text>{plan['badge']}</text>
      <text>{plan['blocks'][0]['label']}</text>
      <text>{plan['blocks'][1]['label']}</text>
      <text>{plan['blocks'][2]['label']}</text>
      <text data-chrome="page_number">3 / 8</text>
    </svg>
    """
    assert_svg_matches_plan(svg, plan)


def test_invented_timeline_allowed_when_skeleton_present():
    plan = normalize_content_plan(COVER_PLAN, page_code="cover", title=COVER_PLAN["title"], page_role="cover")
    svg = f"""
    <svg viewBox="0 0 1280 720">
      <text>{plan['title']}</text>
      <text>{plan['subtitle']}</text>
      <text>{plan['badge']}</text>
      <text>{plan['blocks'][0]['label']}</text>
      <text>{plan['blocks'][1]['label']}</text>
      <text>{plan['blocks'][2]['label']}</text>
      <text>2024 Copilot</text>
      <text>2025 Workflow</text>
      <text>2026 Agent</text>
    </svg>
    """
    assert_svg_matches_plan(svg, plan)


def test_skeleton_allows_table_and_metrics():
    plan = normalize_content_plan(
        {
            "title": "出发前要做的事",
            "blocks": [
                {"label": "预约现状", "note": "可忽略的长解释"},
                {"label": "放票窗口", "note": ""},
                {"label": "抢票 SOP", "note": ""},
                {"label": "行程清单", "note": ""},
            ],
        },
        page_code="page-04",
        title="出发前要做的事",
        page_role="content",
    )
    svg = """
    <svg viewBox="0 0 1280 720">
      <text>出发前要做的事</text>
      <text>预约现状</text>
      <text>100%</text>
      <text>景点名称</text>
      <text>1</text>
      <text>2</text>
      <text>3</text>
      <text>放票窗口</text>
      <text>抢票 SOP</text>
      <text>行程清单</text>
      <text>景点现场示意图</text>
    </svg>
    """
    assert_svg_matches_plan(svg, plan)


def test_split_label_across_text_nodes():
    plan = normalize_content_plan(
        _content_payload(),
        page_code="page-04",
        title="出发前要做的事",
        page_role="content",
    )
    svg = """
    <svg viewBox="0 0 1280 720">
      <text>出发前要做的事</text>
      <text>把预约和行程锁死</text>
      <text>预约</text>
      <text>现状</text>
      <text>抢票 SOP</text>
      <text>行程清单</text>
    </svg>
    """
    assert_svg_matches_plan(svg, plan)


def test_missing_label_still_fails():
    plan = normalize_content_plan(
        _content_payload(),
        page_code="page-04",
        title="出发前要做的事",
        page_role="content",
    )
    svg = """
    <svg viewBox="0 0 1280 720">
      <text>出发前要做的事</text>
      <text>把预约和行程锁死</text>
      <text>预约现状</text>
      <text>抢票 SOP</text>
    </svg>
    """
    with pytest.raises(RuntimeError, match="缺失"):
        assert_svg_matches_plan(svg, plan)


def test_note_not_required_in_svg():
    plan = normalize_content_plan(
        _content_payload(),
        page_code="page-04",
        title="出发前要做的事",
        page_role="content",
    )
    svg = """
    <svg viewBox="0 0 1280 720">
      <text>出发前要做的事</text>
      <text>把预约和行程锁死</text>
      <text>预约现状</text>
      <text>抢票 SOP</text>
      <text>行程清单</text>
    </svg>
    """
    assert_svg_matches_plan(svg, plan)


def test_draft_prompt_allows_fill_from_summary():
    draft = get_prompt_text("draft.page_generate.system")
    assert "填" in draft or "summary" in draft
    assert "禁止新增句子" not in draft
    design = get_prompt_text("design.svg_generate.system")
    assert "逐字一致" not in design
