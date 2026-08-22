from __future__ import annotations

from app.services.layout_validator import check_layout
from app.services.page_quality import evaluate_page_quality
from app.services.quality_report import build_quality_report, make_check


def test_quality_report_hard_fail_ignores_subjective_score():
    report = build_quality_report(
        [
            make_check("text_out_of_bounds", "layout", status="fail", violations=[{"code": "TEXT_OUT_OF_BOUNDS"}]),
            make_check("vlm_score", "subjective", status="pass"),
        ],
        subjective_score=0.96,
    )
    assert report["hard_fail"] is True
    assert report["status"] == "fail"
    assert report["subjective_score"] == 0.96


def test_quality_report_warning_does_not_block_ready():
    report = build_quality_report(
        [make_check("measured_warning", "layout", status="pass", violations=[])],
        warnings=[{"code": "LOW_CONTRAST"}],
    )
    assert report["hard_fail"] is False
    assert report["status"] == "pass"
    assert report["warnings"][0]["code"] == "LOW_CONTRAST"


def test_card_title_layout_box_overflow_is_detected():
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <text x="80" y="198" class="t-title" data-node-id="block-1-title"
            data-layout-box="80,160,260,54" font-size="40px">预约制深度解析与实施路径</text>
    </svg>
    """
    checks, _metrics, _texts = check_layout(svg)
    overflow = next(item for item in checks if item["code"] == "text_out_of_bounds")
    assert overflow["status"] == "fail"
    assert overflow["violations"][0]["node_id"] == "block-1-title"


def test_core_text_overlap_is_detected():
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <text x="80" y="200" class="t-title" data-node-id="a" font-size="40px">卡片标题甲侧内容过长会被放大</text>
      <text x="80" y="220" class="t-title" data-node-id="b" font-size="40px">卡片标题乙侧内容同样被放大</text>
    </svg>
    """
    checks, metrics, _texts = check_layout(svg)
    overlap = next(item for item in checks if item["code"] == "text_overlap")
    assert overlap["status"] == "fail"
    assert metrics["core_overlap_count"] >= 1


def test_text_outside_safe_area_is_detected():
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <text x="8" y="24" class="t-body" data-node-id="edge" font-size="16px">贴边正文</text>
    </svg>
    """
    checks, _metrics, _texts = check_layout(svg)
    safe = next(item for item in checks if item["code"] == "text_outside_safe_area")
    assert safe["status"] == "fail"


def test_invalid_layout_box_is_ignored():
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <text x="80" y="140" class="t-body" data-layout-box="{not-json" font-size="16px">正文</text>
    </svg>
    """
    checks, _metrics, texts = check_layout(svg)
    assert texts[0]["layout_box"] is None
    overflow = next(item for item in checks if item["code"] == "text_out_of_bounds")
    assert overflow["status"] == "pass"


def test_chrome_caption_is_not_treated_as_core_safe_area_violation():
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <text x="48" y="36" class="t-caption" data-chrome="page_title" font-size="13px">页标题</text>
      <text x="80" y="140" class="t-body" font-size="16px">正文</text>
    </svg>
    """
    report = evaluate_page_quality(svg, stage="draft")
    assert report["hard_fail"] is False


def test_svg_profile_fails_forbidden_filter():
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <filter id="glow"><feGaussianBlur stdDeviation="2"/></filter>
      <text x="80" y="140" class="t-body" font-size="16px">正文</text>
    </svg>
    """
    report = evaluate_page_quality(svg, stage="draft")
    assert report["hard_fail"] is True
    profile = next(item for item in report["checks"] if item["code"] == "svg_profile")
    assert profile["status"] == "fail"
    assert profile["violations"][0]["code"] == "SVG_CONTRACT_FAILED"


def test_tspan_line_font_size_is_used_for_bbox_width():
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <text x="80" y="140" class="t-body" font-size="16px">
        <tspan x="80" y="140">短</tspan>
        <tspan x="80" dy="48" font-size="40px">预约制深度解析路径</tspan>
      </text>
    </svg>
    """
    _checks, _metrics, texts = check_layout(svg)
    assert texts[0]["bbox"]["w"] >= 360
