from __future__ import annotations

from typing import Any

from app.services.page_scene import extract_page_scene


def measure_node_bboxes(svg_markup: str, *, mode: str = "estimate", layout_plan: dict[str, Any] | None = None) -> dict[str, Any]:
    if mode == "browser":
        return measure_browser_bboxes(svg_markup, layout_plan=layout_plan)
    scene = extract_page_scene(svg_markup, base_scene=layout_plan)
    return {
        "status": "ok",
        "mode": "estimate",
        "nodes": [
            {
                "node_id": node["node_id"],
                "expected_box": node.get("box"),
                "actual_bbox": node.get("actual_bbox") or node.get("box"),
                "role": node.get("role"),
            }
            for node in scene.get("nodes") or []
        ],
    }


def measure_browser_bboxes(svg_markup: str, *, layout_plan: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "status": "skipped",
            "mode": "browser",
            "reason": "playwright_unavailable",
            "nodes": [],
        }
    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<style>html,body{margin:0;background:#fff}</style>"
        + (svg_markup or "")
    )
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 720})
                page.set_content(html, wait_until="load")
                page.evaluate("async () => { if (document.fonts) await document.fonts.ready; }")
                measured = page.evaluate(
                    """() => Array.from(document.querySelectorAll('[data-node-id]')).map((el) => {
                        const box = el.getBBox ? el.getBBox() : el.getBoundingClientRect();
                        const length = el.getComputedTextLength ? el.getComputedTextLength() : null;
                        return {
                            node_id: el.getAttribute('data-node-id'),
                            actual_bbox: {x: box.x, y: box.y, w: box.width, h: box.height},
                            text_length: length,
                        };
                    })"""
                )
            finally:
                browser.close()
    except Exception as exc:
        return {
            "status": "skipped",
            "mode": "browser",
            "reason": "browser_measure_failed",
            "detail": str(exc),
            "nodes": [],
        }
    return {"status": "ok", "mode": "browser", "nodes": list(measured or [])}
