from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET

from app.services.style_tokens import TOKEN_CLASS_NAMES, build_token_map, resolve_text_token
from app.services.svg import (
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    SVG_NS,
    XLINK_NS,
    _ensure_svg_xmlns,
    _local_tag,
    _parse_svg,
    _serialize_svg,
)

ALLOWED_TAGS = {
    "svg",
    "g",
    "rect",
    "circle",
    "ellipse",
    "line",
    "polygon",
    "polyline",
    "path",
    "text",
    "tspan",
    "image",
    "title",
    "desc",
    "defs",
    "style",
}
ALWAYS_FORBIDDEN_TAGS = {
    "filter",
    "clippath",
    "mask",
    "pattern",
    "use",
    "textpath",
    "foreignobject",
    "symbol",
}
DRAFT_DEFS_TAGS = {
    "lineargradient",
    "radialgradient",
    "stop",
    "marker",
}
ALLOWED_PATH_COMMANDS = set("MLHVZmlhvz")
_COLOR_ATTRS = ("fill", "stroke", "stop-color", "color", "flood-color")
_PATH_CMD_RE = re.compile(r"[A-Za-z]")
_TRANSLATE_RE = re.compile(
    r"translate\(\s*([-+]?\d*\.?\d+(?:e[-+]?\d+)?)\s*(?:[,\s]\s*([-+]?\d*\.?\d+(?:e[-+]?\d+)?))?\s*\)",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"[-+]?\d*\.?\d+(?:e[-+]?\d+)?")


@dataclass(frozen=True)
class SvgViolation:
    xpath: str
    rule: str
    detail: str


class SvgContractError(RuntimeError):
    def __init__(self, message: str, violations: list[SvgViolation]):
        super().__init__(message)
        self.violations = violations

    def as_payload(self) -> dict[str, Any]:
        return {
            "error": str(self),
            "violations": [violation.__dict__ for violation in self.violations],
        }


def validate_svg_contract(
    svg_markup: str,
    *,
    stage: str,
    style_pack: dict[str, Any] | None = None,
) -> None:
    root = _parse_svg(_ensure_svg_xmlns(svg_markup))
    violations: list[SvgViolation] = []
    _walk_validate(root, "/svg", stage, violations, text_token=False, in_defs=False)
    if violations:
        first = violations[0]
        raise SvgContractError(
            f"SVG 契约校验失败（{stage}）: [{first.xpath}] {first.rule}: {first.detail}",
            violations,
        )
    if stage == "design" and style_pack is None:
        raise SvgContractError(
            "设计稿缺少 style_pack，无法校验令牌",
            [SvgViolation("/svg", "style_pack", "style_pack 不能为空")],
        )


def expand_token_classes(
    svg_markup: str,
    style_pack: dict[str, Any],
    *,
    preserve_font_size: bool = False,
) -> str:
    root = _parse_svg(_ensure_svg_xmlns(svg_markup))
    token_map = build_token_map(style_pack.get("palette"), style_pack.get("typography"))
    for elem in root.iter():
        raw_class = (elem.get("class") or "").strip()
        if not raw_class:
            continue
        names = [name for name in raw_class.split() if name]
        resolved = resolve_text_token(names, elem.get("data-text-role"))
        for name in names:
            lookup = resolved if name.startswith("t-") and resolved else name
            attrs = token_map.get(lookup)
            if not attrs:
                continue
            tag = _local_tag(elem)
            applied = dict(attrs)
            if tag == "line" and "fill" in applied and "stroke" not in applied:
                applied["stroke"] = applied.pop("fill")
                if "fill-opacity" in applied and "stroke-opacity" not in applied:
                    applied["stroke-opacity"] = applied.pop("fill-opacity")
                applied.setdefault("fill", "none")
            for key, value in applied.items():
                if preserve_font_size and key == "font-size":
                    continue
                elem.set(key, value)
    return _serialize_svg(root)


_WEAK_FILL_TOKENS = {"c-white", "c-bg", "c-surface"}
_KEEP_FILL_TOKENS = {"c-accent", "c-accent-8", "c-accent-16", "c-surface-alt"}
_TINTED_TOKENS = {"c-accent-8", "c-accent-16"}
_PANEL_CLASS_HINTS = {"card-bg", "inner-panel", "panel", "card", "chip", "badge-bg", "kpi-bg"}
_CSS_RULE_RE = re.compile(r"\.([A-Za-z0-9_-]+)\s*\{([^}]+)\}")


def restore_draft_panel_shapes(design_svg: str, draft_svg: str) -> str:
    """Keep draft card/panel fills after design enhancement.

    Design output often drops tinted rects or remaps them to c-white because
    literal colors are forbidden. Re-apply a c-* fill token by geometry, or
    insert the missing rect behind content.
    """
    draft_root = _parse_svg(_ensure_svg_xmlns(draft_svg))
    design_root = _parse_svg(_ensure_svg_xmlns(design_svg))
    flatten_group_translates(draft_root)
    flatten_group_translates(design_root)
    css_fills, css_strokes = _css_paint_maps(draft_root)
    draft_panels = [
        (elem, box, token)
        for elem, box in _iter_abs_rects(draft_root)
        if _is_panel_rect(elem, box)
        for token in [_panel_fill_token(elem, css_fills=css_fills, css_strokes=css_strokes)]
        if token
    ]
    if not draft_panels:
        return design_svg
    design_rects = [
        (elem, box)
        for elem, box in _iter_abs_rects(design_root)
        if _is_panel_rect(elem, box) and not (elem.get("data-chrome") or "").strip()
    ]
    used: set[int] = set()
    for draft_elem, draft_box, token in sorted(draft_panels, key=lambda item: item[1]["w"] * item[1]["h"], reverse=True):
        match_idx = _best_rect_match(draft_box, design_rects, used)
        if match_idx is None:
            design_root.insert(0, _clone_panel_rect(draft_elem, draft_box, token))
            continue
        used.add(match_idx)
        match_elem, _match_box = design_rects[match_idx]
        _snap_panel_geometry(match_elem, draft_box, draft_elem)
        if _needs_panel_paint_fix(match_elem, token):
            _apply_panel_class(match_elem, token)
    return _serialize_svg(design_root)


def _iter_abs_rects(root: ET.Element) -> list[tuple[ET.Element, dict[str, float]]]:
    found: list[tuple[ET.Element, dict[str, float]]] = []

    def walk(elem: ET.Element, dx: float, dy: float) -> None:
        extra = _parse_translate(elem.get("transform"))
        ndx = dx + (extra[0] if extra else 0.0)
        ndy = dy + (extra[1] if extra else 0.0)
        tag = _local_tag(elem)
        if tag == "rect":
            found.append(
                (
                    elem,
                    {
                        "x": parse_length(elem.get("x")) + ndx,
                        "y": parse_length(elem.get("y")) + ndy,
                        "w": parse_length(elem.get("width")),
                        "h": parse_length(elem.get("height")),
                    },
                )
            )
        if tag == "g":
            for child in list(elem):
                walk(child, ndx, ndy)
            return
        for child in list(elem):
            walk(child, ndx if tag == "svg" else 0.0, ndy if tag == "svg" else 0.0)

    walk(root, 0.0, 0.0)
    return found


def _is_panel_rect(elem: ET.Element, box: dict[str, float]) -> bool:
    if (elem.get("data-chrome") or "").strip():
        return False
    width = box["w"]
    height = box["h"]
    if width < 28 or height < 18:
        return False
    if width >= CANVAS_WIDTH - 8 and height >= CANVAS_HEIGHT - 8:
        return False
    if height <= 12 and width >= CANVAS_WIDTH - 16:
        return False
    classes = (elem.get("class") or "").lower()
    if "slot" in classes or (elem.get("data-image-slot") or "").strip() or (elem.get("data-slot") or "").strip():
        return False
    if (elem.get("stroke-dasharray") or "").strip():
        return False
    return True


def _css_paint_maps(root: ET.Element) -> tuple[dict[str, str], dict[str, str]]:
    fills: dict[str, str] = {}
    strokes: dict[str, str] = {}
    for elem in root.iter():
        if _local_tag(elem) != "style":
            continue
        for match in _CSS_RULE_RE.finditer("".join(elem.itertext())):
            body = match.group(2)
            name = match.group(1)
            fill_match = re.search(r"fill\s*:\s*([^;]+)", body, flags=re.I)
            stroke_match = re.search(r"stroke\s*:\s*([^;]+)", body, flags=re.I)
            if fill_match:
                fills[name] = fill_match.group(1).strip()
            if stroke_match:
                strokes[name] = stroke_match.group(1).strip()
    return fills, strokes


def _panel_fill_token(
    elem: ET.Element,
    *,
    css_fills: dict[str, str] | None = None,
    css_strokes: dict[str, str] | None = None,
) -> str | None:
    css_fills = css_fills or {}
    css_strokes = css_strokes or {}
    classes = [name for name in (elem.get("class") or "").split() if name]
    color_classes = [name for name in classes if name.startswith("c-")]
    for name in color_classes:
        if name in _KEEP_FILL_TOKENS or name in _TINTED_TOKENS or name == "c-surface":
            return "c-surface-alt" if name == "c-surface" else name
        if name in {"c-white", "c-bg"}:
            return None
    fill = (elem.get("fill") or "").strip()
    if not fill or fill.lower() in {"none", "transparent"}:
        for name in classes:
            if name in css_fills:
                fill = css_fills[name]
                break
    if not fill or fill.lower() in {"none", "transparent"}:
        if any(name in _PANEL_CLASS_HINTS for name in classes):
            return "c-surface-alt"
        return None
    opacity = parse_length(elem.get("fill-opacity"), 1.0) * parse_length(elem.get("opacity"), 1.0)
    if opacity <= 0.02:
        return None
    rgb = _parse_color_rgb(fill)
    if rgb is None:
        return "c-surface-alt" if any(name in _PANEL_CLASS_HINTS for name in classes) else "c-surface"
    token = _token_for_draft_fill(rgb, opacity)
    stroke = (elem.get("stroke") or "").strip()
    if not stroke:
        for name in classes:
            if name in css_strokes:
                stroke = css_strokes[name]
                break
    has_card_hint = any(name in _PANEL_CLASS_HINTS for name in classes)
    if token == "c-surface" and (has_card_hint or (stroke and stroke.lower() not in {"none", "transparent"})):
        return "c-surface-alt"
    return token


def _token_for_draft_fill(rgb: tuple[int, int, int], opacity: float) -> str:
    red, green, blue = rgb
    chroma = (max(red, green, blue) - min(red, green, blue)) / 255.0
    lum = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255.0
    if opacity < 0.2:
        return "c-accent-8"
    if opacity < 0.4:
        return "c-accent-16"
    if lum < 0.35:
        return "c-accent"
    tinted = chroma > 0.02 or abs(red - green) + abs(green - blue) + abs(blue - red) > 12
    if lum > 0.7 and tinted:
        return "c-accent-8"
    if lum > 0.92:
        return "c-surface"
    return "c-surface-alt"


def _parse_color_rgb(raw: str) -> tuple[int, int, int] | None:
    value = raw.strip().lower()
    if value in {"white"}:
        return (255, 255, 255)
    if value in {"black"}:
        return (0, 0, 0)
    if value.startswith("#"):
        hex_value = value[1:]
        if len(hex_value) == 3:
            hex_value = "".join(ch * 2 for ch in hex_value)
        if len(hex_value) == 6:
            try:
                return int(hex_value[0:2], 16), int(hex_value[2:4], 16), int(hex_value[4:6], 16)
            except ValueError:
                return None
        return None
    match = re.match(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", value)
    if match:
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    return None


def _needs_panel_paint_fix(elem: ET.Element, mapped: str) -> bool:
    names = {name for name in (elem.get("class") or "").split() if name}
    color = {name for name in names if name.startswith("c-")}
    fill = (elem.get("fill") or "").strip().lower()
    if not color and fill in {"", "none", "transparent"}:
        return True
    if not color:
        return True
    if mapped in _TINTED_TOKENS and color & _WEAK_FILL_TOKENS:
        return True
    if mapped == "c-surface-alt" and color & _WEAK_FILL_TOKENS:
        return True
    if color <= (_WEAK_FILL_TOKENS - {"c-surface"}) and mapped not in {"c-white", "c-bg", "c-surface"}:
        return True
    return False


def _apply_panel_class(elem: ET.Element, token: str) -> None:
    names = [name for name in (elem.get("class") or "").split() if name and not name.startswith("c-")]
    names.insert(0, token)
    elem.set("class", " ".join(names))
    fill = (elem.get("fill") or "").strip().lower()
    if fill in {"none", "transparent"}:
        del elem.attrib["fill"]


def _snap_panel_geometry(elem: ET.Element, box: dict[str, float], draft_elem: ET.Element) -> None:
    elem.set("x", f"{box['x']:g}")
    elem.set("y", f"{box['y']:g}")
    elem.set("width", f"{box['w']:g}")
    elem.set("height", f"{box['h']:g}")
    rx = draft_elem.get("rx")
    ry = draft_elem.get("ry")
    if rx:
        elem.set("rx", rx)
    if ry:
        elem.set("ry", ry)


def _clone_panel_rect(draft_elem: ET.Element, box: dict[str, float], token: str) -> ET.Element:
    rect = ET.Element(f"{{{SVG_NS}}}rect")
    rect.set("x", f"{box['x']:g}")
    rect.set("y", f"{box['y']:g}")
    rect.set("width", f"{box['w']:g}")
    rect.set("height", f"{box['h']:g}")
    rx = draft_elem.get("rx")
    ry = draft_elem.get("ry")
    if rx:
        rect.set("rx", rx)
    if ry:
        rect.set("ry", ry)
    rect.set("class", token)
    rect.set("data-retained-panel", "1")
    return rect


def _best_rect_match(
    draft_box: dict[str, float],
    design_rects: list[tuple[ET.Element, dict[str, float]]],
    used: set[int],
) -> int | None:
    best_idx: int | None = None
    best_score = 0.0
    for index, (_elem, box) in enumerate(design_rects):
        if index in used:
            continue
        score = _rect_iou(draft_box, box)
        close = _rects_close(draft_box, box)
        if score < 0.35 and not close:
            continue
        ranked = score if score >= 0.35 else 0.34
        if ranked > best_score:
            best_score = ranked
            best_idx = index
    return best_idx


def _rect_iou(left: dict[str, float], right: dict[str, float]) -> float:
    x1 = max(left["x"], right["x"])
    y1 = max(left["y"], right["y"])
    x2 = min(left["x"] + left["w"], right["x"] + right["w"])
    y2 = min(left["y"] + left["h"], right["y"] + right["h"])
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    union = left["w"] * left["h"] + right["w"] * right["h"] - inter
    return inter / union if union > 0 else 0.0


def _rects_close(left: dict[str, float], right: dict[str, float]) -> bool:
    return (
        abs((left["x"] + left["w"] / 2.0) - (right["x"] + right["w"] / 2.0)) <= 28
        and abs((left["y"] + left["h"] / 2.0) - (right["y"] + right["h"] / 2.0)) <= 28
        and abs(left["w"] - right["w"]) <= 36
        and abs(left["h"] - right["h"]) <= 36
    )


def _walk_validate(
    elem: ET.Element,
    xpath: str,
    stage: str,
    violations: list[SvgViolation],
    *,
    text_token: bool,
    in_defs: bool,
) -> None:
    tag = _local_tag(elem)
    lname = tag.lower()
    rule = _tag_violation(lname, stage=stage, in_defs=in_defs)
    if rule == "forbidden_element":
        violations.append(SvgViolation(xpath, "forbidden_element", f"禁止使用 <{tag}>"))
        return
    if rule == "unknown_element":
        violations.append(SvgViolation(xpath, "unknown_element", f"不支持的图元 <{tag}>"))
        return
    transform = (elem.get("transform") or "").strip()
    if transform and _has_forbidden_transform(transform):
        violations.append(SvgViolation(xpath, "transform", "只允许 translate，禁止 rotate/skew/matrix"))
    if tag == "path":
        commands = set(_PATH_CMD_RE.findall(elem.get("d") or ""))
        illegal = {cmd for cmd in commands if cmd not in ALLOWED_PATH_COMMANDS}
        if illegal:
            violations.append(
                SvgViolation(xpath, "path_commands", f"path 只允许 M/L/H/V/Z，出现了 {''.join(sorted(illegal))}")
            )
    if tag == "image":
        _validate_image(elem, xpath, violations)
    child_text_token = text_token
    if stage == "design":
        child_text_token = _validate_design_paint(elem, xpath, tag, violations, text_token=text_token)
        if tag == "rect" and _is_full_bleed_rect(elem):
            violations.append(SvgViolation(xpath, "full_bleed", "全幅背景矩形由系统合成，模型不得绘制"))
    counts: dict[str, int] = {}
    for child in list(elem):
        child_tag = _local_tag(child)
        counts[child_tag] = counts.get(child_tag, 0) + 1
        child_path = f"{xpath}/{child_tag}[{counts[child_tag]}]"
        _walk_validate(
            child,
            child_path,
            stage,
            violations,
            text_token=child_text_token,
            in_defs=in_defs or lname == "defs",
        )


def _tag_violation(lname: str, *, stage: str, in_defs: bool) -> str | None:
    if lname.startswith("fe") or lname in ALWAYS_FORBIDDEN_TAGS:
        return "forbidden_element"
    if lname in {"defs", "style"}:
        return "forbidden_element" if stage == "design" else None
    if lname in DRAFT_DEFS_TAGS:
        if stage == "draft" and in_defs:
            return None
        return "forbidden_element"
    if lname in ALLOWED_TAGS:
        return None
    return "unknown_element"


def _validate_design_paint(
    elem: ET.Element,
    xpath: str,
    tag: str,
    violations: list[SvgViolation],
    *,
    text_token: bool,
) -> bool:
    if elem.get("style"):
        violations.append(SvgViolation(xpath, "style_attr", "禁止 inline style，请用 class 令牌"))
    for attr in _COLOR_ATTRS:
        value = (elem.get(attr) or "").strip()
        if not value or value.lower() in {"none", "transparent"}:
            continue
        violations.append(SvgViolation(xpath, "literal_color", f"{attr}={value!r}，设计稿只能用 class 令牌"))
    classes = [name for name in (elem.get("class") or "").split() if name]
    unknown = [name for name in classes if name not in TOKEN_CLASS_NAMES]
    if unknown:
        violations.append(SvgViolation(xpath, "unknown_class", f"未知 class: {' '.join(unknown)}"))
    has_text_token = text_token or any(name.startswith("t-") for name in classes)
    if tag in {"text", "tspan"}:
        has_text = bool((elem.text or "").strip()) or any((child.tail or "").strip() for child in elem)
        if has_text and not has_text_token:
            violations.append(SvgViolation(xpath, "text_token", "文本必须使用语义字阶 class（t-page-title/t-card-title/t-kpi 等）"))
        return has_text_token
    if tag in {"rect", "circle", "ellipse", "polygon", "polyline", "path", "line"}:
        paint_classes = [name for name in classes if name.startswith("c-")]
        fill = (elem.get("fill") or "").strip().lower()
        if not paint_classes and fill not in {"none", "transparent"}:
            violations.append(SvgViolation(xpath, "color_token", "图形必须使用 c-* 令牌类名"))
    return has_text_token


def _validate_image(elem: ET.Element, xpath: str, violations: list[SvgViolation]) -> None:
    if (elem.get("data-chrome") or "").strip():
        return
    image_id = (elem.get("data-image-id") or "").strip()
    href = (elem.get("href") or elem.get(f"{{{XLINK_NS}}}href") or "").strip()
    if not image_id:
        violations.append(SvgViolation(xpath, "image_ref", "内容配图必须带 data-image-id"))
        return
    if href and not href.startswith("#") and not href.startswith("data:image/"):
        violations.append(SvgViolation(xpath, "image_href", "内容配图禁止文件路径或网络地址，只能用 data-image-id"))


def _has_forbidden_transform(transform: str) -> bool:
    lowered = transform.lower()
    return any(token in lowered for token in ("rotate(", "skewx(", "skewy(", "matrix(", "scale("))


def _is_full_bleed_rect(elem: ET.Element) -> bool:
    x = _attr_float(elem, "x", 0.0)
    y = _attr_float(elem, "y", 0.0)
    width = _attr_float(elem, "width", 0.0)
    height = _attr_float(elem, "height", 0.0)
    return x <= 1 and y <= 1 and width >= CANVAS_WIDTH - 2 and height >= CANVAS_HEIGHT - 2


def _attr_float(elem: ET.Element, name: str, default: float) -> float:
    raw = elem.get(name)
    if raw is None or raw == "":
        return default
    match = _NUMBER_RE.search(raw)
    if not match:
        return default
    try:
        return float(match.group(0))
    except ValueError:
        return default


def flatten_group_translates(root: ET.Element) -> None:
    _flatten(root, 0.0, 0.0)


def _flatten(elem: ET.Element, dx: float, dy: float) -> None:
    extra = _parse_translate(elem.get("transform"))
    local_dx = dx + (extra[0] if extra else 0.0)
    local_dy = dy + (extra[1] if extra else 0.0)
    if extra:
        attrib = dict(elem.attrib)
        attrib.pop("transform", None)
        elem.attrib.clear()
        elem.attrib.update(attrib)
    tag = _local_tag(elem)
    if tag == "g":
        for child in list(elem):
            _flatten(child, local_dx, local_dy)
        return
    if local_dx or local_dy:
        _apply_offset(elem, tag, local_dx, local_dy)
    next_dx, next_dy = (local_dx, local_dy) if tag in {"text", "tspan"} else (0.0, 0.0)
    for child in list(elem):
        _flatten(child, next_dx, next_dy)


def _parse_translate(transform: str | None) -> tuple[float, float] | None:
    if not transform:
        return None
    match = _TRANSLATE_RE.search(transform)
    if not match:
        return None
    tx = float(match.group(1))
    ty = float(match.group(2) or 0.0)
    return tx, ty


def _apply_offset(elem: ET.Element, tag: str, dx: float, dy: float) -> None:
    if tag in {"rect", "image", "text"}:
        _shift_attr(elem, "x", dx)
        _shift_attr(elem, "y", dy)
    elif tag == "tspan":
        if "x" in elem.attrib:
            _shift_attr(elem, "x", dx)
        if "y" in elem.attrib:
            _shift_attr(elem, "y", dy)
    elif tag in {"circle", "ellipse"}:
        _shift_attr(elem, "cx", dx)
        _shift_attr(elem, "cy", dy)
    elif tag == "line":
        _shift_attr(elem, "x1", dx)
        _shift_attr(elem, "y1", dy)
        _shift_attr(elem, "x2", dx)
        _shift_attr(elem, "y2", dy)
    elif tag == "polygon" or tag == "polyline":
        points = elem.get("points")
        if points:
            nums = [float(item) for item in _NUMBER_RE.findall(points)]
            shifted: list[str] = []
            for index in range(0, len(nums) - 1, 2):
                shifted.append(f"{nums[index] + dx:g},{nums[index + 1] + dy:g}")
            elem.set("points", " ".join(shifted))
    elif tag == "path":
        elem.set("d", _shift_path(elem.get("d") or "", dx, dy))


def _shift_attr(elem: ET.Element, name: str, delta: float) -> None:
    if name not in elem.attrib and delta == 0:
        return
    current = _attr_float(elem, name, 0.0)
    elem.set(name, f"{current + delta:g}")


def _shift_path(path_d: str, dx: float, dy: float) -> str:
    tokens = re.findall(r"[A-Za-z]|[-+]?\d*\.?\d+(?:e[-+]?\d+)?", path_d or "")
    out: list[str] = []
    cx = cy = sx = sy = 0.0
    index = 0

    def take(values: list[float], count: int) -> list[list[float]]:
        return [values[offset : offset + count] for offset in range(0, len(values) - count + 1, count)]

    while index < len(tokens):
        token = tokens[index]
        if not token.isalpha():
            index += 1
            continue
        command = token
        index += 1
        args: list[float] = []
        while index < len(tokens) and not tokens[index].isalpha():
            args.append(float(tokens[index]))
            index += 1
        relative = command.islower()
        cmd = command.upper()
        if cmd == "M":
            groups = take(args, 2)
            if not groups:
                continue
            x, y = groups[0]
            if relative:
                x, y = cx + x, cy + y
            cx, cy = x, y
            sx, sy = x, y
            out.extend(["M", f"{x + dx:g}", f"{y + dy:g}"])
            for x, y in groups[1:]:
                if relative:
                    x, y = cx + x, cy + y
                cx, cy = x, y
                out.extend(["L", f"{x + dx:g}", f"{y + dy:g}"])
        elif cmd == "L":
            for x, y in take(args, 2):
                if relative:
                    x, y = cx + x, cy + y
                cx, cy = x, y
                out.extend(["L", f"{x + dx:g}", f"{y + dy:g}"])
        elif cmd == "H":
            for x in args:
                if relative:
                    x = cx + x
                cx = x
                out.extend(["H", f"{cx + dx:g}"])
        elif cmd == "V":
            for y in args:
                if relative:
                    y = cy + y
                cy = y
                out.extend(["V", f"{cy + dy:g}"])
        elif cmd == "Z":
            out.append("Z")
            cx, cy = sx, sy
        else:
            raise RuntimeError(f"path 含有无法展开的指令: {command}")
    return " ".join(out)


def estimated_text_width(text: str, font_px: float) -> float:
    if not text:
        return font_px
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = max(len(text) - cjk, 0)
    return max(font_px, cjk * font_px + other * font_px * 0.62)


def parse_length(raw: str | None, default: float = 0.0) -> float:
    if raw is None or raw == "":
        return default
    match = _NUMBER_RE.search(raw)
    if not match:
        return default
    try:
        return float(match.group(0))
    except ValueError:
        return default
