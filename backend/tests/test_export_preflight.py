from __future__ import annotations

from app.services.export_preflight import preflight_shapes_slide
from app.services.page_quality import evaluate_page_quality


def test_preflight_accepts_native_text_slide():
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <rect x="80" y="80" width="400" height="200" fill="#003366"/>
      <text x="100" y="140" font-size="36" fill="#ffffff">导出测试</text>
    </svg>
    """
    result = preflight_shapes_slide(svg)
    assert result["status"] == "pass"
    assert result["has_native_text"] is True


def test_preflight_rejects_unknown_element():
    result = preflight_shapes_slide('<svg viewBox="0 0 1280 720"><foo/></svg>')
    assert result["status"] == "fail"
    assert result["violations"][0]["code"] == "EXPORT_SHAPES_PREFLIGHT"


def test_preflight_rejects_nested_tspan():
    svg = """
    <svg viewBox="0 0 1280 720">
      <text x="80" y="160" font-size="16" fill="#111111">外层<tspan x="140" y="160">中层<tspan x="220" y="200">内层</tspan></tspan></text>
    </svg>
    """
    result = preflight_shapes_slide(svg)
    assert result["status"] == "fail"
    assert "tspan" in result["detail"]


def test_design_quality_requires_export_preflight():
    svg = '<svg viewBox="0 0 1280 720"><foo/></svg>'
    report = evaluate_page_quality(svg, stage="design")
    assert report["hard_fail"] is True
    export_check = next(item for item in report["checks"] if item["code"] == "export_shapes_preflight")
    assert export_check["status"] == "fail"


def test_preflight_rejects_unknown_element_without_image_mode():
    result = preflight_shapes_slide('<svg viewBox="0 0 1280 720"><foo/></svg>')
    assert result["status"] == "fail"
    assert result["violations"][0]["code"] == "EXPORT_SHAPES_PREFLIGHT"
    assert "image" not in result["detail"].lower()
