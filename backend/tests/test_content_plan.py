from __future__ import annotations

import pytest

from app.services.content_plan import (
    assert_svg_matches_plan,
    normalize_content_plan,
)


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


def test_svg_matches_plan_and_ignores_page_number():
    plan = normalize_content_plan(COVER_PLAN, page_code="cover", title=COVER_PLAN["title"], page_role="cover")
    svg = f"""
    <svg viewBox="0 0 1280 720">
      <text>{plan['title']}</text>
      <text>{plan['subtitle']}</text>
      <text>{plan['badge']}</text>
      <text>{plan['blocks'][0]['label']}</text>
      <text>{plan['blocks'][0]['note']}</text>
      <text>{plan['blocks'][1]['label']}</text>
      <text>{plan['blocks'][1]['note']}</text>
      <text>{plan['blocks'][2]['label']}</text>
      <text>{plan['blocks'][2]['note']}</text>
      <text>{plan['footer']['presenter']}</text>
      <text>{plan['footer']['date']}</text>
      <text data-chrome="page_number">3 / 8</text>
    </svg>
    """
    assert_svg_matches_plan(svg, plan)


def test_invented_timeline_fails_plan_check():
    plan = normalize_content_plan(COVER_PLAN, page_code="cover", title=COVER_PLAN["title"], page_role="cover")
    svg = f"""
    <svg viewBox="0 0 1280 720">
      <text>{plan['title']}</text>
      <text>2024 Copilot</text>
      <text>2025 Workflow</text>
      <text>2026 Agent</text>
    </svg>
    """
    with pytest.raises(RuntimeError, match="不一致"):
        assert_svg_matches_plan(svg, plan)


def test_appended_text_counts_as_extra():
    plan = normalize_content_plan(COVER_PLAN, page_code="cover", title=COVER_PLAN["title"], page_role="cover")
    svg = f"""
    <svg viewBox="0 0 1280 720">
      <text>{plan['title']}</text>
      <text>{plan['subtitle']}</text>
      <text>{plan['badge']}</text>
      <text>{plan['blocks'][0]['label']}</text>
      <text>{plan['blocks'][0]['note']}</text>
      <text>{plan['blocks'][1]['label']}</text>
      <text>{plan['blocks'][1]['note']}</text>
      <text>{plan['blocks'][2]['label']}</text>
      <text>{plan['blocks'][2]['note']}</text>
      <text>{plan['footer']['presenter']}</text>
      <text>{plan['footer']['date']}</text>
      <text>预约制深度解析（含2024 Copilot）</text>
    </svg>
    """
    with pytest.raises(RuntimeError, match="多出"):
        assert_svg_matches_plan(svg, plan)
