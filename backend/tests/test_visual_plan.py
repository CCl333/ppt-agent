from __future__ import annotations

import pytest

from app.services.page_quality import evaluate_page_quality
from app.services.visual_plan import normalize_visual_plan, propose_visual_plan


def test_image_slots_convert_to_photo_slots():
    plan = propose_visual_plan(
        {"title": "中轴线", "image_slots": [{"image_id": "asset-7", "label": "现场", "placement": "hero"}]},
        page_images=[{"image_id": "asset-7", "caption": "故宫"}],
    )
    slot = plan["slots"][0]
    assert slot["kind"] == "photo"
    assert slot["source_mode"] == "search"
    assert slot["asset_id"] == "asset-7"
    assert slot["status"] == "bound"
    assert slot["priority"] == "required"
    assert slot["fallback"] == "reflow_without_visual"
    assert slot["license_status"] == "unknown"


def test_content_image_slot_without_placement_is_optional_support():
    plan = propose_visual_plan(
        {"title": "中轴线", "image_slots": [{"image_id": "asset-7", "label": "现场"}]},
        page_images=[{"image_id": "asset-7"}],
        page_role="content",
    )
    slot = plan["slots"][0]
    assert slot["placement"] == "support"
    assert slot["priority"] == "optional"
    assert slot["fallback"] == "skip"


def test_section_visual_intent_is_optional_atmosphere():
    plan = propose_visual_plan(
        {"title": "走进北京的时间轴", "visual_intent": "北京城市轴线"},
        page_role="section",
    )
    assert plan["slots"][0]["kind"] == "photo"
    assert plan["slots"][0]["priority"] == "optional"
    assert plan["slots"][0]["fallback"] == "skip"
    assert plan["slots"][0]["status"] == "planned"


def test_no_image_catalog_still_produces_complete_plan():
    plan = propose_visual_plan({"title": "行程预算", "blocks": [{"label": "交通", "note": "地铁"}]})
    assert plan["slots"]
    assert plan["slots"][0]["kind"] == "none"
    assert plan["slots"][0]["status"] == "ready"
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <text x="80" y="140" class="t-page-title" data-node-id="page-title" data-text-role="page-title" font-size="32px">行程预算</text>
    </svg>
    """
    report = evaluate_page_quality(svg, stage="draft", visual_plan=plan)
    assert report["hard_fail"] is False


def test_required_chart_without_output_cannot_ready():
    plan = normalize_visual_plan(
        {
            "slots": [
                {
                    "slot_id": "visual-1",
                    "kind": "chart",
                    "source_mode": "render",
                    "priority": "required",
                    "status": "planned",
                }
            ]
        }
    )
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <text x="80" y="140" class="t-body" font-size="16px">图表占位说明</text>
    </svg>
    """
    report = evaluate_page_quality(svg, stage="design", visual_plan=plan)
    assert report["hard_fail"] is True
    codes = {
        violation.get("code")
        for check in report["checks"]
        for violation in check.get("violations") or []
    }
    assert "UNRESOLVED_REQUIRED_SLOT" in codes


def test_fallback_applied_counts_as_ready_slot():
    plan = normalize_visual_plan(
        {
            "slots": [
                {
                    "slot_id": "visual-1",
                    "kind": "photo",
                    "source_mode": "search",
                    "priority": "required",
                    "status": "fallback_applied",
                    "fallback": "reflow_without_visual",
                }
            ]
        }
    )
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <text x="80" y="140" class="t-page-title" data-node-id="page-title" data-text-role="page-title" font-size="32px">无图重排</text>
    </svg>
    """
    report = evaluate_page_quality(svg, stage="design", visual_plan=plan)
    assert report["hard_fail"] is False


def test_unknown_kind_is_rejected():
    with pytest.raises(RuntimeError, match="kind"):
        normalize_visual_plan({"slots": [{"slot_id": "x", "kind": "sticker"}]})


def test_content_support_photo_is_optional():
    plan = propose_visual_plan(
        {"title": "行程预算"},
        page_images=[{"image_id": "asset-1", "caption": "现场", "license_status": "unknown"}],
        page_role="content",
    )
    slot = plan["slots"][0]
    assert slot["kind"] == "photo"
    assert slot["placement"] == "support"
    assert slot["priority"] == "optional"
    assert slot["fallback"] == "skip"
    assert slot["license_status"] == "unknown"


def test_cover_photo_is_required_hero():
    plan = propose_visual_plan(
        {"title": "封面"},
        page_images=[{"image_id": "asset-1", "caption": "主视觉"}],
        page_role="cover",
    )
    slot = plan["slots"][0]
    assert slot["priority"] == "required"
    assert slot["fallback"] == "reflow_without_visual"
    assert slot["placement"] == "hero"


def test_generated_image_defaults_to_optional_skip():
    plan = normalize_visual_plan(
        {
            "page_role": "content",
            "slots": [{"slot_id": "visual-1", "kind": "generated_image", "status": "planned"}],
        }
    )
    assert plan["slots"][0]["priority"] == "optional"
    assert plan["slots"][0]["fallback"] == "skip"


def test_chart_without_priority_is_required():
    plan = normalize_visual_plan(
        {
            "slots": [{"slot_id": "visual-1", "kind": "chart", "source_mode": "render", "status": "planned"}],
        }
    )
    assert plan["slots"][0]["priority"] == "required"
    assert "fallback" not in plan["slots"][0]


def test_unknown_license_does_not_block_ready():
    plan = propose_visual_plan(
        {"title": "中轴线", "image_slots": [{"image_id": "asset-7", "label": "现场", "placement": "hero"}]},
        page_images=[{"image_id": "asset-7", "license_status": "unknown"}],
        page_role="content",
    )
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <text x="80" y="140" class="t-page-title" data-node-id="page-title" data-text-role="page-title" font-size="32px">中轴线</text>
    </svg>
    """
    report = evaluate_page_quality(svg, stage="design", visual_plan=plan)
    codes = {
        violation.get("code")
        for check in report["checks"]
        for violation in check.get("violations") or []
    }
    assert "UNRESOLVED_REQUIRED_SLOT" not in codes
    assert plan["slots"][0]["license_status"] == "unknown"
