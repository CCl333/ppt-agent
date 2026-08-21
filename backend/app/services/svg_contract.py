from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET

from app.services.style_tokens import TOKEN_CLASS_NAMES, build_token_map
from app.services.svg import (
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
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


def expand_token_classes(svg_markup: str, style_pack: dict[str, Any]) -> str:
    root = _parse_svg(_ensure_svg_xmlns(svg_markup))
    token_map = build_token_map(style_pack.get("palette"), style_pack.get("typography"))
    for elem in root.iter():
        raw_class = (elem.get("class") or "").strip()
        if not raw_class:
            continue
        names = [name for name in raw_class.split() if name]
        for name in names:
            attrs = token_map.get(name)
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
                elem.set(key, value)
    return _serialize_svg(root)


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
            violations.append(SvgViolation(xpath, "text_token", "文本必须使用 t-title/t-subtitle/t-body/t-caption/t-label"))
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
