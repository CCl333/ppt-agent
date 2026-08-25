from __future__ import annotations

import json
import re
from typing import Any
from xml.etree import ElementTree as ET

from app.services.quality_report import make_check
from app.services.svg import CANVAS_HEIGHT, CANVAS_WIDTH, _ensure_svg_xmlns, _local_tag, _parse_svg, _serialize_svg
from app.services.svg_contract import estimated_text_width, flatten_group_translates, parse_length
from app.services.style_tokens import derive_role_scale, min_font_px_for_token, resolve_text_token

_FONT_CLASS_ALIASES = {
    "t-badge": "t-label",
    "t-tag": "t-label",
    "t-chip": "t-label",
    "t-num": "t-kpi",
}

SAFE_AREA = {"x": 48.0, "y": 56.0, "w": 1184.0, "h": 624.0}
CHROME_SKIP = {"background", "texture", "page_number"}
AUX_TOKENS = {
    "t-caption",
    "t-label",
    "t-badge",
    "t-num",
    "t-tag",
    "t-kpi-unit",
    "t-card-subtitle",
    "t-page-badge",
    "t-toc-num",
}
HARD_FONT_TOKENS = {
    "t-display",
    "t-page-title",
    "t-title",
    "t-card-title",
    "t-kpi",
    "t-toc-item",
}
ASCENT_RATIO = 0.88
MIN_FONT_PX = {name: float(spec["min_px"]) for name, spec in derive_role_scale(None).items()}
DEFAULT_FONT_PX = 16.0
LINE_HEIGHT = 1.2
TOLERANCE_PX = 2.0
CANVAS_TOLERANCE_PX = 4.0
SAFE_TOLERANCE_PX = 16.0
BOX_TOLERANCE_PX = 8.0
OVERLAP_AREA_PX2 = 16.0
_CSS_RULE_RE = re.compile(r"([^{}]+)\{([^}]*)\}")
_CSS_CLASS_RE = re.compile(r"\.([A-Za-z0-9_-]+)")
_FONT_SIZE_RE = re.compile(r"font-size\s*:\s*([\d.]+)px", re.I)
_TEXT_ANCHOR_RE = re.compile(r"text-anchor\s*:\s*(start|middle|end)", re.I)


def check_layout(
    svg_markup: str,
    layout_plan: dict[str, Any] | None = None,
    typography: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    root = _parse_svg(_ensure_svg_xmlns(svg_markup))
    flatten_group_translates(root)
    class_fonts = collect_css_font_sizes(root)
    texts = collect_text_items(root, layout_plan=layout_plan, class_fonts=class_fonts)
    chrome_boxes = _chrome_boxes(root, class_fonts=class_fonts)
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
        if not _inside(bbox, {"x": 0.0, "y": 0.0, "w": float(CANVAS_WIDTH), "h": float(CANVAS_HEIGHT)}, CANVAS_TOLERANCE_PX):
            canvas_violations.append(_violation("TEXT_OUTSIDE_CANVAS", item, "文字超出画布", expected={"x": 0, "y": 0, "w": CANVAS_WIDTH, "h": CANVAS_HEIGHT}))
        if item["core"] and not item["chrome"] and not _inside(bbox, SAFE_AREA, SAFE_TOLERANCE_PX):
            safe_violations.append(_violation("TEXT_OUTSIDE_SAFE_AREA", item, "文字超出 safe area", expected=SAFE_AREA))
        if item["layout_box"] and _usable_box(item["layout_box"]) and not _inside(bbox, item["layout_box"], BOX_TOLERANCE_PX):
            box_violations.append(_violation("TEXT_OUT_OF_BOUNDS", item, "文字超出文本盒", expected=item["layout_box"]))
        min_px = min_font_px_for_token(item["token"], typography)
        if item["token"] in HARD_FONT_TOKENS and item["font_px"] + 0.01 < min_px:
            font_violations.append(_violation("FONT_BELOW_MINIMUM", item, f"字号 {item['font_px']}px 低于 {min_px}px"))
        if item["core"] and not item["chrome"]:
            for chrome in chrome_boxes:
                if _overlap_area(bbox, chrome["box"]) > OVERLAP_AREA_PX2:
                    chrome_violations.append(_violation("TEXT_COVERS_CHROME", item, f"文字覆盖 {chrome['kind']}", expected=chrome["box"]))

    core_items = [item for item in texts if item["core"] and not item["chrome"]]
    for index, left in enumerate(core_items):
        for right in core_items[index + 1 :]:
            area = _overlap_area(left["bbox"], right["bbox"])
            if area > OVERLAP_AREA_PX2:
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


def collect_css_font_sizes(root: ET.Element) -> dict[str, float]:
    fonts: dict[str, float] = {}
    for elem in root.iter():
        if _local_tag(elem) != "style":
            continue
        css = "".join(elem.itertext() or "")
        for match in _CSS_RULE_RE.finditer(css):
            size_match = _FONT_SIZE_RE.search(match.group(2) or "")
            if not size_match:
                continue
            try:
                size = float(size_match.group(1))
            except ValueError:
                continue
            if size <= 0:
                continue
            for selector in (match.group(1) or "").split(","):
                for class_name in _CSS_CLASS_RE.findall(selector):
                    fonts[class_name] = size
    return fonts


def collect_font_size_maps(svg_markup: str) -> tuple[dict[str, float], dict[str, float]]:
    root = _parse_svg(_ensure_svg_xmlns(svg_markup))
    class_fonts = collect_css_font_sizes(root)
    by_node: dict[str, float] = {}
    for elem in root.iter():
        if _local_tag(elem) != "text":
            continue
        node_id = (elem.get("data-node-id") or "").strip()
        if not node_id:
            continue
        by_node[node_id] = _font_px(elem, class_fonts=class_fonts)
    return by_node, dict(class_fonts)


def apply_draft_font_sizes(design_svg: str, draft_svg: str) -> str:
    node_sizes, class_sizes = collect_font_size_maps(draft_svg)
    class_sizes = dict(class_sizes)
    for source, target in _FONT_CLASS_ALIASES.items():
        if source in class_sizes and target not in class_sizes:
            class_sizes[target] = class_sizes[source]
    text_sizes = _collect_draft_text_font_sizes(draft_svg)
    role_sizes = {name: float(spec["size_px"]) for name, spec in derive_role_scale(None).items()}
    root = _parse_svg(_ensure_svg_xmlns(design_svg))
    for elem in root.iter():
        if _local_tag(elem) not in {"text", "tspan"}:
            continue
        if (elem.get("data-chrome") or "").strip():
            continue
        node_id = (elem.get("data-node-id") or "").strip()
        size = node_sizes.get(node_id) if node_id else None
        token = resolve_text_token((elem.get("class") or "").split(), elem.get("data-text-role")) or _text_token(elem)
        if size is None and token:
            size = class_sizes.get(token)
        if size is None:
            text = "".join(elem.itertext()).strip()
            size = text_sizes.get(text)
        if size is None and token:
            size = role_sizes.get(token)
        if size is None or size <= 0:
            continue
        elem.set("font-size", f"{size:g}px")
    return _serialize_svg(root)


def _collect_draft_text_font_sizes(svg_markup: str) -> dict[str, float]:
    root = _parse_svg(_ensure_svg_xmlns(svg_markup))
    class_fonts = collect_css_font_sizes(root)
    sizes: dict[str, float] = {}
    for elem in root.iter():
        if _local_tag(elem) != "text":
            continue
        text = "".join(elem.itertext()).strip()
        if not text:
            continue
        size = _font_px(elem, class_fonts=class_fonts)
        if size > 0:
            sizes[text] = size
    return sizes


def collect_text_items(
    root: ET.Element,
    layout_plan: dict[str, Any] | None = None,
    class_fonts: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    fonts = class_fonts if class_fonts is not None else collect_css_font_sizes(root)

    def walk(elem: ET.Element, inherited_chrome: str, inherited_anchor: str) -> None:
        chrome = (elem.get("data-chrome") or "").strip() or inherited_chrome
        anchor = _text_anchor(elem, inherited_anchor)
        if _local_tag(elem) == "text":
            class_names = (elem.get("class") or "").split()
            class_token = _text_token(elem)
            role = (elem.get("data-text-role") or "").strip() or None
            token = resolve_text_token(class_names, role) or class_token
            font_px = _font_px(elem, class_fonts=fonts)
            node_id = (elem.get("data-node-id") or "").strip() or None
            layout_box = parse_layout_box(elem.get("data-layout-box"), layout_plan=layout_plan)
            lines = _text_lines(elem, class_fonts=fonts, anchor=anchor)
            if lines:
                first = lines[0]
                last = lines[-1]
                first_px = float(first.get("font_px") or font_px)
                last_px = float(last.get("font_px") or font_px)
                top = first["y"] - first_px * ASCENT_RATIO
                bottom = last["y"] + last_px * (LINE_HEIGHT - ASCENT_RATIO)
                lefts: list[float] = []
                rights: list[float] = []
                for line in lines:
                    line_w = estimated_text_width(line["text"], float(line.get("font_px") or font_px))
                    line_x = _anchored_x(line["x"], line_w, str(line.get("anchor") or anchor))
                    lefts.append(line_x)
                    rights.append(line_x + line_w)
                bbox = {
                    "x": min(lefts),
                    "y": top,
                    "w": max(rights) - min(lefts),
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
                        "core": bool(token) and token not in AUX_TOKENS and chrome not in CHROME_SKIP and chrome != "page_title",
                        "font_px": font_px,
                        "bbox": bbox,
                        "layout_box": layout_box,
                        "role": (elem.get("data-text-role") or "").strip() or None,
                    }
                )
        for child in list(elem):
            walk(child, chrome, anchor)

    walk(root, "", "start")
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


def _text_lines(
    elem: ET.Element,
    class_fonts: dict[str, float] | None = None,
    anchor: str = "start",
) -> list[dict[str, Any]]:
    font_px = _font_px(elem, class_fonts=class_fonts)
    origin_x = parse_length(elem.get("x"), 0.0)
    origin_y = parse_length(elem.get("y"), 0.0)
    tspans = [child for child in list(elem) if _local_tag(child) == "tspan"]
    if not tspans:
        content = "".join(elem.itertext()).strip()
        if not content:
            return []
        return [{"x": origin_x, "y": origin_y, "text": content, "font_px": font_px, "anchor": anchor}]
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
        lines.append(
            {
                "x": x,
                "y": current_y,
                "text": content,
                "font_px": _font_px(tspan, default=font_px, class_fonts=class_fonts),
                "anchor": _text_anchor(tspan, anchor),
            }
        )
    return lines


def _font_px(
    elem: ET.Element,
    default: float = DEFAULT_FONT_PX,
    class_fonts: dict[str, float] | None = None,
) -> float:
    value = parse_length(elem.get("font-size"), 0.0)
    if value > 0:
        return value
    style_match = _FONT_SIZE_RE.search(elem.get("style") or "")
    if style_match:
        try:
            inline = float(style_match.group(1))
        except ValueError:
            inline = 0.0
        if inline > 0:
            return inline
    names = (elem.get("class") or "").split()
    fonts = class_fonts or {}
    for name in names:
        if name.startswith("t-") and name in fonts:
            return fonts[name]
    for name in names:
        if name in fonts:
            return fonts[name]
    return default


def _text_token(elem: ET.Element) -> str | None:
    for name in (elem.get("class") or "").split():
        if name.startswith("t-"):
            return name
    return None


def _text_anchor(elem: ET.Element, default: str = "start") -> str:
    raw = (elem.get("text-anchor") or "").strip().lower()
    if raw in {"start", "middle", "end"}:
        return raw
    style_match = _TEXT_ANCHOR_RE.search(elem.get("style") or "")
    if style_match:
        return style_match.group(1).lower()
    return default or "start"


def _anchored_x(x: float, width: float, anchor: str) -> float:
    if anchor == "end":
        return x - width
    if anchor == "middle":
        return x - width / 2.0
    return x


def _usable_box(box: dict[str, float] | None) -> bool:
    if not box:
        return False
    return box.get("w", 0) > 1 and box.get("h", 0) > 1


def _chrome_boxes(root: ET.Element, class_fonts: dict[str, float] | None = None) -> list[dict[str, Any]]:
    boxes: list[dict[str, Any]] = []
    fonts = class_fonts if class_fonts is not None else collect_css_font_sizes(root)
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
            font_px = _font_px(elem, class_fonts=fonts)
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


def _inside(inner: dict[str, float], outer: dict[str, float], tolerance: float = TOLERANCE_PX) -> bool:
    return (
        inner["x"] >= outer["x"] - tolerance
        and inner["y"] >= outer["y"] - tolerance
        and inner["x"] + inner["w"] <= outer["x"] + outer["w"] + tolerance
        and inner["y"] + inner["h"] <= outer["y"] + outer["h"] + tolerance
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
