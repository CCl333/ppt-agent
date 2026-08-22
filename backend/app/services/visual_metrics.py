from __future__ import annotations

from typing import Any

from app.services.layout_validator import SAFE_AREA, check_layout, collect_text_items
from app.services.svg import CANVAS_HEIGHT, CANVAS_WIDTH, _ensure_svg_xmlns, _parse_svg
from app.services.svg_contract import flatten_group_translates

CANVAS_AREA = float(CANVAS_WIDTH * CANVAS_HEIGHT)
EXPECTED_ASPECT = CANVAS_WIDTH / CANVAS_HEIGHT
WHITESPACE_HIGH = 0.72
WHITESPACE_LOW = 0.08
IMBALANCE_RATIO = 0.22


def measure_visual_metrics(svg_markup: str, *, layout_plan: dict[str, Any] | None = None) -> dict[str, Any]:
    _layout_checks, layout_metrics, texts = check_layout(svg_markup, layout_plan=layout_plan)
    boxes = [item["bbox"] for item in texts if item.get("bbox")]
    occupied = _occupied_area(boxes)
    whitespace = max(0.0, 1.0 - occupied / CANVAS_AREA) if CANVAS_AREA else 1.0
    imbalance = _imbalance(boxes)
    font_sizes = sorted({round(float(item["font_px"]), 1) for item in texts if item.get("font_px")})
    warnings: list[dict[str, Any]] = []
    if whitespace > WHITESPACE_HIGH:
        warnings.append(
            {
                "code": "EXCESSIVE_WHITESPACE",
                "detail": f"空白占比 {whitespace:.2f}，页面可能太空",
                "value": whitespace,
            }
        )
    if whitespace < WHITESPACE_LOW and boxes:
        warnings.append(
            {
                "code": "OVERCROWDED",
                "detail": f"空白占比 {whitespace:.2f}，页面可能过密",
                "value": whitespace,
            }
        )
    if imbalance["offset_ratio"] > IMBALANCE_RATIO:
        warnings.append(
            {
                "code": "VISUAL_IMBALANCE",
                "detail": "视觉重心明显偏离画布中心",
                "value": imbalance["offset_ratio"],
            }
        )
    return {
        "schema_version": "visual-metrics.v1",
        "whitespace_ratio": round(whitespace, 4),
        "occupied_area_px2": round(occupied, 2),
        "collision_count": int(layout_metrics.get("core_overlap_count") or 0),
        "overflow_area_px2": float(layout_metrics.get("overflow_area_px2") or 0.0),
        "text_coverage_ratio": round(1.0 - whitespace, 4),
        "font_step_count": len(font_sizes),
        "font_sizes_px": font_sizes,
        "safe_area": SAFE_AREA,
        "aspect": {"width": CANVAS_WIDTH, "height": CANVAS_HEIGHT, "ratio": EXPECTED_ASPECT},
        "imbalance": imbalance,
        "warnings": warnings,
        "layout_metrics": layout_metrics,
    }


def collect_occupied_boxes(svg_markup: str, *, layout_plan: dict[str, Any] | None = None) -> list[dict[str, float]]:
    root = _parse_svg(_ensure_svg_xmlns(svg_markup))
    flatten_group_translates(root)
    return [item["bbox"] for item in collect_text_items(root, layout_plan=layout_plan) if item.get("bbox")]


def _occupied_area(boxes: list[dict[str, float]]) -> float:
    return sum(max(0.0, box["w"]) * max(0.0, box["h"]) for box in boxes)


def _imbalance(boxes: list[dict[str, float]]) -> dict[str, float]:
    if not boxes:
        return {"cx": CANVAS_WIDTH / 2, "cy": CANVAS_HEIGHT / 2, "offset_ratio": 0.0}
    weights = []
    cx = 0.0
    cy = 0.0
    total = 0.0
    for box in boxes:
        area = max(0.0, box["w"]) * max(0.0, box["h"])
        if area <= 0:
            continue
        cx += (box["x"] + box["w"] / 2) * area
        cy += (box["y"] + box["h"] / 2) * area
        total += area
        weights.append(area)
    if total <= 0:
        return {"cx": CANVAS_WIDTH / 2, "cy": CANVAS_HEIGHT / 2, "offset_ratio": 0.0}
    cx /= total
    cy /= total
    dx = cx - CANVAS_WIDTH / 2
    dy = cy - CANVAS_HEIGHT / 2
    diagonal = (CANVAS_WIDTH**2 + CANVAS_HEIGHT**2) ** 0.5
    return {
        "cx": round(cx, 2),
        "cy": round(cy, 2),
        "offset_ratio": round(((dx**2 + dy**2) ** 0.5) / diagonal, 4) if diagonal else 0.0,
    }
