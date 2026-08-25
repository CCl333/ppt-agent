from __future__ import annotations

from app.services.bbox_measure import measure_browser_bboxes, measure_node_bboxes
from app.services.layout_validator import check_layout
from app.services.page_quality import evaluate_page_quality
from app.services.page_scene import diff_page_scenes, extract_page_scene, propose_layout_plan


OVERFLOW_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <text x="80" y="198" class="t-card-title" data-node-id="block-1-title" data-text-role="card-title"
        data-layout-box="block-1-title" font-size="40px">预约制深度解析与实施路径</text>
</svg>
"""

DRAFT_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <text x="80" y="140" class="t-page-title" data-node-id="page-title" data-text-role="page-title" font-size="32px">行程预算</text>
  <text x="80" y="220" class="t-card-title" data-node-id="block-1-title" data-text-role="card-title" font-size="18px">预约制</text>
</svg>
"""

DESIGN_MISSING_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <text x="80" y="140" class="t-page-title" data-node-id="page-title" data-text-role="page-title" font-size="32px">行程预算</text>
</svg>
"""


def test_propose_layout_plan_assigns_stable_node_ids():
    scene = propose_layout_plan(
        {
            "title": "行程预算",
            "blocks": [
                {"label": "预约制", "note": "提前约"},
                {"label": "避峰", "note": "错峰走"},
                {"label": "路线", "note": "中轴线"},
            ],
        }
    )
    ids = [node["node_id"] for node in scene["nodes"]]
    assert ids[0] == "page-title"
    assert "block-1-title" in ids
    assert scene["reading_order"][0] == "page-title"


def test_named_layout_box_resolves_against_scene_and_reports_bbox():
    scene = {
        "schema_version": "page-scene.v1",
        "canvas": {"width": 1280, "height": 720},
        "nodes": [
            {
                "node_id": "block-1-title",
                "kind": "text",
                "role": "card-title",
                "box": {"x": 80, "y": 160, "w": 260, "h": 54},
            }
        ],
        "reading_order": ["block-1-title"],
    }
    checks, _metrics, texts = check_layout(OVERFLOW_SVG, layout_plan=scene)
    overflow = next(item for item in checks if item["code"] == "text_out_of_bounds")
    assert overflow["status"] == "fail"
    violation = overflow["violations"][0]
    assert violation["node_id"] == "block-1-title"
    assert violation["expected_box"]["w"] > 0
    assert violation["actual_bbox"]["w"] > violation["expected_box"]["w"]


def test_design_missing_core_node_fails_identity():
    draft_scene = extract_page_scene(DRAFT_SVG)
    report = evaluate_page_quality(DESIGN_MISSING_SVG, stage="design", layout_plan=draft_scene)
    assert report["hard_fail"] is True
    codes = {
        violation.get("code")
        for check in report["checks"]
        for violation in check.get("violations") or []
    }
    assert "SCENE_NODE_MISSING" in codes
    missing = next(
        violation
        for check in report["checks"]
        for violation in check.get("violations") or []
        if violation.get("code") == "SCENE_NODE_MISSING"
    )
    assert missing["node_id"] == "block-1-title"
    assert missing.get("expected_box")


def test_layout_box_drift_is_warning_not_hard_fail():
    base = extract_page_scene(DRAFT_SVG)
    shifted = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <text x="200" y="300" class="t-page-title" data-node-id="page-title" data-text-role="page-title" font-size="32px">行程预算</text>
      <text x="200" y="380" class="t-card-title" data-node-id="block-1-title" data-text-role="card-title" font-size="18px">预约制</text>
    </svg>
    """
    report = evaluate_page_quality(shifted, stage="design", layout_plan=base)
    assert report["hard_fail"] is False
    warning = next(item for item in report.get("warnings") or [] if item.get("code") == "LAYOUT_BOX_DRIFT")
    assert warning["drift_class"] == "skeleton"


def test_decorative_visual_drift_is_not_reported():
    from app.services.page_scene import layout_drift_class

    node = {
        "node_id": "visual-atmosphere",
        "kind": "visual-slot",
        "role": "hero-visual",
        "visual_slot_id": "visual-atmosphere",
        "box": {"x": 40, "y": 80, "w": 400, "h": 240},
    }
    visual_plan = {
        "slots": [{"slot_id": "visual-atmosphere", "kind": "photo", "priority": "optional", "status": "planned"}]
    }
    assert layout_drift_class(node, visual_slots=visual_plan["slots"]) == "decorative"
    base = {
        "schema_version": "page-scene.v1",
        "nodes": [node],
        "reading_order": ["visual-atmosphere"],
    }
    shifted = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <image data-node-id="visual-atmosphere" data-image-slot-id="visual-atmosphere" x="200" y="200" width="500" height="300"/>
    </svg>
    """
    report = evaluate_page_quality(
        shifted, stage="design", layout_plan=base, visual_plan=visual_plan, run_export_preflight=False
    )
    assert not any(item.get("code") == "LAYOUT_BOX_DRIFT" for item in report.get("warnings") or [])
    for check in report["checks"]:
        assert not any(item.get("code") == "LAYOUT_BOX_DRIFT" for item in check.get("violations") or [])


def test_duplicate_node_id_fails():
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <text x="80" y="140" class="t-page-title" data-node-id="page-title" data-text-role="page-title" font-size="32px">甲</text>
      <text x="80" y="220" class="t-page-title" data-node-id="page-title" data-text-role="page-title" font-size="32px">乙</text>
    </svg>
    """
    report = evaluate_page_quality(svg, stage="draft")
    codes = {
        violation.get("code")
        for check in report["checks"]
        for violation in check.get("violations") or []
    }
    assert "NODE_ID_DUPLICATE" in codes


def test_estimate_bbox_measure_points_to_node_and_box():
    payload = measure_node_bboxes(DRAFT_SVG, mode="estimate")
    assert payload["status"] == "ok"
    page_title = next(item for item in payload["nodes"] if item["node_id"] == "page-title")
    assert page_title["actual_bbox"]["w"] > 0
    assert page_title["expected_box"]


def test_browser_bbox_does_not_fake_pass_without_playwright():
    payload = measure_browser_bboxes(DRAFT_SVG)
    assert payload["status"] in {"ok", "skipped"}
    if payload["status"] == "skipped":
        assert payload["reason"] in {"playwright_unavailable", "browser_measure_failed"}
        assert payload["nodes"] == []


def test_draft_extra_kpi_is_warning_not_hard_fail():
    proposed = propose_layout_plan(
        {"title": "核心景点怎么选", "blocks": [{"label": "三潭印月", "note": "必看亮点"}]}
    )
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <style>.t-page-title { font-size: 28px; } .t-card-title { font-size: 18px; } .t-body { font-size: 14px; } .t-kpi { font-size: 28px; }</style>
      <text class="t-page-title" data-node-id="page-title" data-text-role="page-title" x="80" y="140">核心景点怎么选</text>
      <text class="t-card-title" data-node-id="block-1-title" data-text-role="card-title" x="80" y="220">三潭印月</text>
      <text class="t-body" data-node-id="block-1-body" data-text-role="body" x="80" y="260">必看亮点</text>
      <text class="t-kpi" data-node-id="kpi-ticket" data-text-role="kpi" x="900" y="220">80元</text>
    </svg>
    """
    report = evaluate_page_quality(svg, stage="draft", layout_plan=proposed)
    codes = {
        violation.get("code")
        for check in report["checks"]
        if check.get("status") == "fail"
        for violation in check.get("violations") or []
    }
    assert "SCENE_NODE_ADDED" not in codes
    assert report["hard_fail"] is False
    assert any(item.get("code") == "SCENE_NODE_ADDED" for item in report.get("warnings") or [])


def test_draft_cover_display_role_is_compatible_with_page_title_svg():
    proposed = propose_layout_plan({"title": "杭州西湖半日游攻略"}, page_role="cover")
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <style>.t-page-title { font-size: 32px; }</style>
      <g transform="translate(64, 72)">
        <text class="t-page-title" data-node-id="page-title" data-text-role="page-title"
              data-layout-box="64,72,1152,52" x="0" y="32">杭州西湖半日游攻略</text>
      </g>
    </svg>
    """
    report = evaluate_page_quality(svg, stage="draft", layout_plan=proposed)
    codes = {
        violation.get("code")
        for check in report["checks"]
        for violation in check.get("violations") or []
    }
    assert "SCENE_ROLE_CHANGED" not in codes
    assert "FONT_BELOW_MINIMUM" not in codes
    assert "TEXT_OUTSIDE_CANVAS" not in codes
    assert report["hard_fail"] is False


def test_scene_diff_detects_role_change():
    left = extract_page_scene(DRAFT_SVG)
    right_svg = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <text x="80" y="140" class="t-page-title" data-node-id="page-title" data-text-role="page-title" font-size="32px">行程预算</text>
      <text x="80" y="220" class="t-kpi" data-node-id="block-1-title" data-text-role="kpi" font-size="24px">预约制</text>
    </svg>
    """
    diff = diff_page_scenes(left, extract_page_scene(right_svg))
    assert diff["role_changed"]
    report = evaluate_page_quality(right_svg, stage="design", layout_plan=left)
    codes = {
        violation.get("code")
        for check in report["checks"]
        for violation in check.get("violations") or []
    }
    assert "SCENE_ROLE_CHANGED" in codes


def test_draft_quality_uses_proposed_scene_not_self_extract():
    proposed = propose_layout_plan(
        {"title": "行程预算", "blocks": [{"label": "预约制", "note": "提前约"}]}
    )
    report = evaluate_page_quality(DESIGN_MISSING_SVG, stage="draft", layout_plan=proposed)
    assert report["hard_fail"] is True
    codes = {
        violation.get("code")
        for check in report["checks"]
        for violation in check.get("violations") or []
    }
    assert "SCENE_NODE_MISSING" in codes
    assert "block-1-title" in {
        violation.get("node_id")
        for check in report["checks"]
        for violation in check.get("violations") or []
    }


def test_extract_page_scene_keeps_text_ref_and_groups_from_base():
    proposed = propose_layout_plan({"title": "行程预算", "blocks": [{"label": "预约制", "note": "提前约"}]})
    extracted = extract_page_scene(DRAFT_SVG, content_plan={"title": "行程预算"}, base_scene=proposed)
    title = next(node for node in extracted["nodes"] if node["node_id"] == "page-title")
    assert title.get("text_ref") == "content_plan.title"
    assert any(node["node_id"] == "block-1" and node["kind"] == "group" for node in extracted["nodes"])
    card_title = next(node for node in extracted["nodes"] if node["node_id"] == "block-1-title")
    assert card_title.get("text_ref") == "content_plan.blocks.0.label"
