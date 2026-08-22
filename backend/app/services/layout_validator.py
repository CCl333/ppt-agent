from __future__ import annotations

import json
from typing import Any
from xml.etree import ElementTree as ET

from app.services.quality_report import make_check
from app.services.svg import CANVAS_HEIGHT, CANVAS_WIDTH, _ensure_svg_xmlns, _local_tag, _parse_svg
from app.services.svg_contract import estimated_text_width, flatten_group_translates, parse_length
from app.services.style_tokens import derive_role_scale, min_font_px_for_token, resolve_text_token

SAFE_AREA = {"x": 48.0, "y": 56.0, "w": 1184.0, "h": 624.0}
CHROME_SKIP = {"background", "texture", "page_number"}
AUX_TOKENS = {"t-caption", "t-label"}
ASCENT_RATIO = 0.88
MIN_FONT_PX = {name: float(spec["min_px"]) for name, spec in derive_role_scale(None).items()}
DEFAULT_FONT_PX = 16.0
LINE_HEIGHT = 1.2
TOLERANCE_PX = 2.0


def check_layout(
    svg_markup: str,
    layout_plan: dict[str, Any] | None = None,
    typography: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    root = _parse_svg(_ensure_svg_xmlns(svg_markup))
    flatten_group_translates(root)
    texts = collect_text_items(root, layout_plan=layout_plan)
    chrome_boxes = _chrome_boxes(root)
    canvas_violations: list[dict[str, Any]] = []
    safe_violations: list[dict[str, Any]] = []
    box_violations: list[dict[str, Any]] = []
    font_violations: list[dict[str, Any]] = []
    chrome_violations: list[dict[str, Any]] = []
    overlap_violations: list[dict[str, Any]] = []
    fonts: list[float] = []

    for item in texts:
        fonts.append(item["font_px"])
        bbox = item["bbox"]
        if not _inside(bbox, {"x": 0.0, "y": 0.0, "w": float(CANVAS_WIDTH), "h": float(CANVAS_HEIGHT)}):
            canvas_violations.append(_violation("TEXT_OUTSIDE_CANVAS", item, "文字超出画布", expected={"x": 0, "y": 0, "w": CANVAS_WIDTH, "h": CANVAS_HEIGHT}))
        if item["core"] and not item["chrome"] and not _inside(bbox, SAFE_AREA):
            safe_violations.append(_violation("TEXT_OUTSIDE_SAFE_AREA", item, "文字超出 safe area", expected=SAFE_AREA))
        if item["layout_box"] and not _inside(bbox, item["layout_box"]):
            box_violations.append(_violation("TEXT_OUT_OF_BOUNDS", item, "文字超出文本盒", expected=item["layout_box"]))
        min_px = min_font_px_for_token(item["token"], typography)
        if item["font_px"] + 0.01 < min_px:
            font_violations.append(_violation("FONT_BELOW_MINIMUM", item, f"字号 {item['font_px']}px 低于 {min_px}px"))
        if item["core"] and not item["chrome"]:
            for chrome in chrome_boxes:
                if _overlap_area(bbox, chrome["box"]) > 1:
                    chrome_violations.append(_violation("TEXT_COVERS_CHROME", item, f"文字覆盖 {chrome['kind']}", expected=chrome["box"]))

    core_items = [item for item in texts if item["core"] and not item["chrome"]]
    for index, left in enumerate(core_items):
        for right in core_items[index + 1 :]:
            area = _overlap_area(left["bbox"], right["bbox"])
            if area > 1:
                overlap_violations.append(
                    {
                        "code": "TEXT_OVERLAP",
                        "node_id": left["node_id"],
                        "other_node_id": right["node_id"],
                        "detail": f"{left['node_id'] or left['text'][:12]} 与 {right['node_id'] or right['text'][:12]} 重叠",
                        "expected_box": left["bbox"],
                        "actual_bbox": right["bbox"],
                    }
                )

    checks = [
        make_check("text_outside_canvas", "layout", status="fail" if canvas_violations else "pass", violations=canvas_violations),
        make_check("text_outside_safe_area", "layout", status="fail" if safe_violations else "pass", violations=safe_violations),
        make_check("text_out_of_bounds", "layout", status="fail" if box_violations else "pass", violations=box_violations),
        make_check("text_overlap", "layout", status="fail" if overlap_violations else "pass", violations=overlap_violations),
        make_check("font_below_minimum", "layout", status="fail" if font_violations else "pass", violations=font_violations),
        make_check("text_covers_chrome", "layout", status="fail" if chrome_violations else "pass", violations=chrome_violations),
    ]
    metrics = {
        "text_count": len(texts),
        "min_font_px": min(fonts) if fonts else None,
        "max_font_px": max(fonts) if fonts else None,
        "core_overlap_count": len(overlap_violations),
        "overflow_area_px2": sum(_overflow_area(item["bbox"], item["layout_box"]) for item in texts if item["layout_box"]),
    }
    return checks, metrics, texts


def collect_text_items(root: ET.Element, layout_plan: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    def walk(elem: ET.Element, inherited_chrome: str) -> None:
        chrome = (elem.get("data-chrome") or "").strip() or inherited_chrome
        if _local_tag(elem) == "text":
            class_names = (elem.get("class") or "").split()
            class_token = _text_token(elem)
            role = (elem.get("data-text-role") or "").strip() or None
            token = resolve_text_token(class_names, role) or class_token
            font_px = _font_px(elem)
            node_id = (elem.get("data-node-id") or "").strip() or None
            layout_box = parse_layout_box(elem.get("data-layout-box"), layout_plan=layout_plan)
            lines = _text_lines(elem)
            if lines:
                width = max(
                    estimated_text_width(line["text"], float(line.get("font_px") or font_px))
                    for line in lines
                )
                first = lines[0]
                last = lines[-1]
                first_px = float(first.get("font_px") or font_px)
                last_px = float(last.get("font_px") or font_px)
                top = first["y"] - first_px * ASCENT_RATIO
                bottom = last["y"] + last_px * (LINE_HEIGHT - ASCENT_RATIO)
                bbox = {
                    "x": min(line["x"] for line in lines),
                    "y": top,
                    "w": width,
                    "h": max(bottom - top, max(first_px, last_px, font_px)),
                }
                text = "".join(line["text"] for line in lines)
                items.append(
                    {
                        "node_id": node_id,
                        "text": text,
                        "token": token,
                        "class_token": class_token,
                        "chrome": chrome,
                        "core": token not in AUX_TOKENS and chrome not in CHROME_SKIP and chrome != "page_title",
                        "font_px": font_px,
                        "bbox": bbox,
                        "layout_box": layout_box,
                        "role": (elem.get("data-text-role") or "").strip() or None,
                    }
                )
        for child in list(elem):
            walk(child, chrome)

    walk(root, "")
    return items


def parse_layout_box(raw: str | None, layout_plan: dict[str, Any] | None = None) -> dict[str, float] | None:
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        if text.startswith("{"):
            data = json.loads(text)
            return {"x": float(data["x"]), "y": float(data["y"]), "w": float(data["w"]), "h": float(data["h"])}
        parts = [part for part in text.replace(" ", "").split(",") if part]
        if len(parts) == 4:
            return {"x": float(parts[0]), "y": float(parts[1]), "w": float(parts[2]), "h": float(parts[3])}
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    return box_from_scene(layout_plan, text)


def box_from_scene(scene: dict[str, Any] | None, ref: str | None) -> dict[str, float] | None:
    if not isinstance(scene, dict) or not ref:
        return None
    key = ref.strip()
    aliases = {key, key.removesuffix("-box")}
    for node in scene.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("node_id") or "").strip()
        box_id = str(node.get("box_id") or "").strip()
        if node_id not in aliases and box_id not in aliases and f"{node_id}-box" not in aliases:
            continue
        box = node.get("box")
        if not isinstance(box, dict):
            continue
        try:
            return {"x": float(box["x"]), "y": float(box["y"]), "w": float(box["w"]), "h": float(box["h"])}
        except (KeyError, TypeError, ValueError):
            return None
    return None


def _text_lines(elem: ET.Element) -> list[dict[str, Any]]:
    font_px = _font_px(elem)
    origin_x = parse_length(elem.get("x"), 0.0)
    origin_y = parse_length(elem.get("y"), 0.0)
    tspans = [child for child in list(elem) if _local_tag(child) == "tspan"]
    if not tspans:
        content = "".join(elem.itertext()).strip()
        if not content:
            return []
        return [{"x": origin_x, "y": origin_y, "text": content, "font_px": font_px}]
    lines: list[dict[str, Any]] = []
    current_y = origin_y
    for tspan in tspans:
        content = "".join(tspan.itertext()).strip()
        if not content:
            continue
        x = parse_length(tspan.get("x"), origin_x)
        if tspan.get("y") not in {None, ""}:
            current_y = parse_length(tspan.get("y"), current_y)
        current_y += parse_length(tspan.get("dy"), 0.0)
        lines.append({"x": x, "y": current_y, "text": content, "font_px": _font_px(tspan, font_px)})
    return lines


def _font_px(elem: ET.Element, default: float = DEFAULT_FONT_PX) -> float:
    value = parse_length(elem.get("font-size"), 0.0)
    return value if value > 0 else default


def _text_token(elem: ET.Element) -> str | None:
    for name in (elem.get("class") or "").split():
        if name.startswith("t-"):
            return name
    return None


def _chrome_boxes(root: ET.Element) -> list[dict[str, Any]]:
    boxes: list[dict[str, Any]] = []
    for elem in root.iter():
        kind = (elem.get("data-chrome") or "").strip()
        if kind not in {"title_bar", "page_number"}:
            continue
        tag = _local_tag(elem)
        if tag == "rect":
            boxes.append(
                {
                    "kind": kind,
                    "box": {
                        "x": parse_length(elem.get("x")),
                        "y": parse_length(elem.get("y")),
                        "w": parse_length(elem.get("width")),
                        "h": parse_length(elem.get("height")),
                    },
                }
            )
        elif tag == "text":
            font_px = _font_px(elem)
            x = parse_length(elem.get("x"))
            y = parse_length(elem.get("y"))
            text = "".join(elem.itertext())
            boxes.append(
                {
                    "kind": kind,
                    "box": {
                        "x": x,
                        "y": y - font_px * ASCENT_RATIO,
                        "w": estimated_text_width(text, font_px),
                        "h": font_px * LINE_HEIGHT,
                    },
                }
            )
    return boxes


def _inside(inner: dict[str, float], outer: dict[str, float]) -> bool:
    return (
        inner["x"] >= outer["x"] - TOLERANCE_PX
        and inner["y"] >= outer["y"] - TOLERANCE_PX
        and inner["x"] + inner["w"] <= outer["x"] + outer["w"] + TOLERANCE_PX
        and inner["y"] + inner["h"] <= outer["y"] + outer["h"] + TOLERANCE_PX
    )


def _overlap_area(left: dict[str, float], right: dict[str, float]) -> float:
    x1 = max(left["x"], right["x"])
    y1 = max(left["y"], right["y"])
    x2 = min(left["x"] + left["w"], right["x"] + right["w"])
    y2 = min(left["y"] + left["h"], right["y"] + right["h"])
    if x2 <= x1 or y2 <= y1:
        return 0.0
    return (x2 - x1) * (y2 - y1)


def _overflow_area(inner: dict[str, float], outer: dict[str, float]) -> float:
    overflow = 0.0
    overflow += max(0.0, outer["x"] - inner["x"]) * inner["h"]
    overflow += max(0.0, inner["x"] + inner["w"] - (outer["x"] + outer["w"])) * inner["h"]
    overflow += max(0.0, outer["y"] - inner["y"]) * inner["w"]
    overflow += max(0.0, inner["y"] + inner["h"] - (outer["y"] + outer["h"])) * inner["w"]
    return overflow


def _violation(code: str, item: dict[str, Any], detail: str, expected: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "code": code,
        "node_id": item.get("node_id"),
        "detail": detail,
        "expected_box": expected,
        "actual_bbox": item.get("bbox"),
    }
