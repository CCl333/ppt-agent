from __future__ import annotations

import base64
import re
from pathlib import Path
from xml.etree import ElementTree as ET

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
CANVAS_WIDTH = 1280
CANVAS_HEIGHT = 720
REQUIRED_VIEWBOX = f"0 0 {CANVAS_WIDTH} {CANVAS_HEIGHT}"
CJK_FONT_STACK = '"Microsoft YaHei", "PingFang SC", "Noto Sans SC", "Source Han Sans SC", sans-serif'
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)


def extract_and_validate_svg(text: str) -> str:
    matched = re.search(r"<svg[\s\S]*?</svg>", text, flags=re.IGNORECASE)
    if matched is None:
        raise RuntimeError("SVG 模型没有返回有效的 <svg> 文档")
    markup = _ensure_svg_xmlns(matched.group(0))
    root = _parse_svg(markup)
    _force_canvas(root)
    return _serialize_svg(root)


def apply_cjk_fonts(svg_markup: str) -> str:
    root = _parse_svg(_ensure_svg_xmlns(svg_markup))
    for elem in root.iter():
        if _local_tag(elem) not in {"text", "tspan"}:
            continue
        current = (elem.get("font-family") or "").strip()
        if not current or current in {"sans-serif", "sans serif"}:
            elem.set("font-family", CJK_FONT_STACK)
        elif "Microsoft YaHei" not in current and "PingFang SC" not in current:
            elem.set("font-family", f"{current}, {CJK_FONT_STACK}")
        style = elem.get("style")
        if style and re.search(r"font-family\s*:", style, flags=re.IGNORECASE):
            elem.set("style", _rewrite_style_font_family(style))
    return _serialize_svg(root)


def embed_background_image(svg_markup: str, image_path: str | Path) -> str:
    path = Path(image_path)
    if not path.is_file():
        raise RuntimeError(f"背景图不存在: {image_path}")
    mime = _IMAGE_MIME.get(path.suffix.lower())
    if mime is None:
        raise RuntimeError(f"不支持的背景图类型: {path.suffix}")
    data = path.read_bytes()
    if not data:
        raise RuntimeError(f"背景图为空: {image_path}")
    href = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    root = _parse_svg(_ensure_svg_xmlns(svg_markup))
    image = ET.Element(f"{{{SVG_NS}}}image")
    image.set("href", href)
    image.set(f"{{{XLINK_NS}}}href", href)
    image.set("x", "0")
    image.set("y", "0")
    image.set("width", str(CANVAS_WIDTH))
    image.set("height", str(CANVAS_HEIGHT))
    image.set("preserveAspectRatio", "xMidYMid slice")
    root.insert(0, image)
    return _serialize_svg(root)


def prepare_page_svg(svg_markup: str, background_path: str | Path | None = None) -> str:
    svg = apply_cjk_fonts(extract_and_validate_svg(svg_markup))
    if background_path:
        svg = embed_background_image(svg, background_path)
    return svg


def canvas_size(svg_markup: str) -> tuple[float, float]:
    root = _parse_svg(_ensure_svg_xmlns(svg_markup))
    view_box = root.attrib.get("viewBox") or root.attrib.get("viewbox")
    if view_box:
        parts = [part for part in re.split(r"[,\s]+", view_box.strip()) if part]
        if len(parts) == 4:
            width = float(parts[2])
            height = float(parts[3])
            if width > 0 and height > 0:
                return width, height
    width = _parse_dimension(root.attrib.get("width"))
    height = _parse_dimension(root.attrib.get("height"))
    if width > 0 and height > 0:
        return width, height
    return float(CANVAS_WIDTH), float(CANVAS_HEIGHT)


def _ensure_svg_xmlns(markup: str) -> str:
    if re.search(r"<svg\b[^>]*xmlns=", markup, flags=re.IGNORECASE):
        return markup
    return re.sub(r"<svg\b", f'<svg xmlns="{SVG_NS}"', markup, count=1, flags=re.IGNORECASE)


def _parse_svg(markup: str) -> ET.Element:
    try:
        return ET.fromstring(markup)
    except ET.ParseError as exc:
        raise RuntimeError(f"SVG 不是合法 XML: {exc}") from exc


def _force_canvas(root: ET.Element) -> None:
    root.set("viewBox", REQUIRED_VIEWBOX)
    root.set("width", str(CANVAS_WIDTH))
    root.set("height", str(CANVAS_HEIGHT))


def _serialize_svg(root: ET.Element) -> str:
    root.attrib.pop("xmlns", None)
    return ET.tostring(root, encoding="unicode")


def _local_tag(elem: ET.Element) -> str:
    return elem.tag.rsplit("}", 1)[-1]


def _parse_dimension(raw_value: str | None) -> float:
    if not raw_value:
        return 0.0
    match = re.search(r"[-+]?\d*\.?\d+", raw_value)
    if not match:
        return 0.0
    try:
        return float(match.group(0))
    except ValueError:
        return 0.0


def _rewrite_style_font_family(style: str) -> str:
    if re.search(r"font-family\s*:\s*[^;]*(Microsoft YaHei|PingFang SC)", style, flags=re.IGNORECASE):
        return style
    return re.sub(
        r"font-family\s*:\s*[^;]+",
        f"font-family: {CJK_FONT_STACK}",
        style,
        count=1,
        flags=re.IGNORECASE,
    )
