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
    root = _parse_svg(_ensure_svg_xmlns(svg_markup))
    root.insert(0, _background_image_element(image_path))
    return _serialize_svg(root)


def _background_image_element(image_path: str | Path) -> ET.Element:
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
    image = ET.Element(f"{{{SVG_NS}}}image")
    image.set("href", href)
    image.set(f"{{{XLINK_NS}}}href", href)
    image.set("x", "0")
    image.set("y", "0")
    image.set("width", str(CANVAS_WIDTH))
    image.set("height", str(CANVAS_HEIGHT))
    image.set("preserveAspectRatio", "xMidYMid slice")
    return image


def inject_fixed_chrome(
    svg_markup: str,
    *,
    tokens: dict[str, dict[str, str]],
    background_path: str | Path | None = None,
    page_title: str = "",
    page_index: int = 1,
    page_count: int = 1,
    page_role: str = "content",
) -> str:
    root = _parse_svg(_ensure_svg_xmlns(svg_markup))
    layers: list[ET.Element] = []
    bg = _token_attrs(tokens, "c-bg")
    bg_rect = ET.Element(f"{{{SVG_NS}}}rect")
    bg_rect.set("x", "0")
    bg_rect.set("y", "0")
    bg_rect.set("width", str(CANVAS_WIDTH))
    bg_rect.set("height", str(CANVAS_HEIGHT))
    bg_rect.set("fill", bg.get("fill") or "#FFFFFF")
    bg_rect.set("data-chrome", "background")
    layers.append(bg_rect)

    if background_path:
        image = _background_image_element(background_path)
        image.set("data-chrome", "texture")
        layers.append(image)

    skip_title_bar = page_role in {"cover", "end"}
    if not skip_title_bar:
        accent = _token_attrs(tokens, "c-accent")
        bar = ET.Element(f"{{{SVG_NS}}}rect")
        bar.set("x", "0")
        bar.set("y", "0")
        bar.set("width", str(CANVAS_WIDTH))
        bar.set("height", "8")
        bar.set("fill", accent.get("fill") or "#111111")
        bar.set("data-chrome", "title_bar")
        layers.append(bar)
        caption = _token_attrs(tokens, "t-caption")
        if page_title.strip():
            title = ET.Element(f"{{{SVG_NS}}}text")
            title.set("x", "48")
            title.set("y", "36")
            title.text = page_title.strip()
            title.set("data-chrome", "page_title")
            for key, value in caption.items():
                title.set(key, value)
            layers.append(title)

    if page_role != "cover":
        caption = _token_attrs(tokens, "t-caption")
        number = ET.Element(f"{{{SVG_NS}}}text")
        number.set("x", "1232")
        number.set("y", "700")
        number.set("text-anchor", "end")
        number.text = f"{page_index} / {page_count}"
        number.set("data-chrome", "page_number")
        for key, value in caption.items():
            number.set(key, value)
        layers.append(number)

    for index, layer in enumerate(layers):
        root.insert(index, layer)
    return _serialize_svg(root)


def prepare_page_svg(
    svg_markup: str,
    background_path: str | Path | None = None,
    *,
    stage: str = "draft",
    style_pack: dict | None = None,
    chrome: dict | None = None,
    page_images: list | None = None,
) -> str:
    from app.services.page_images import resolve_image_refs
    from app.services.style_tokens import build_token_map
    from app.services.svg_contract import expand_token_classes, validate_svg_contract

    svg = extract_and_validate_svg(svg_markup)
    validate_svg_contract(svg, stage=stage, style_pack=style_pack)
    svg = resolve_image_refs(svg, page_images)
    if stage == "design":
        if style_pack is None:
            raise RuntimeError("设计稿缺少 style_pack")
        svg = expand_token_classes(svg, style_pack)
        svg = apply_cjk_fonts(svg)
        chrome = chrome or {}
        svg = inject_fixed_chrome(
            svg,
            tokens=build_token_map(style_pack.get("palette"), style_pack.get("typography")),
            background_path=background_path,
            page_title=str(chrome.get("page_title") or ""),
            page_index=int(chrome.get("page_index") or 1),
            page_count=int(chrome.get("page_count") or 1),
            page_role=str(chrome.get("page_role") or "content"),
        )
        return svg
    svg = apply_cjk_fonts(svg)
    if background_path:
        svg = embed_background_image(svg, background_path)
    return svg


def _token_attrs(tokens: dict[str, dict[str, str]], name: str) -> dict[str, str]:
    return dict(tokens.get(name) or {})


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
