from __future__ import annotations

from pathlib import Path

import pytest

from app.services.svg import (
    CJK_FONT_STACK,
    apply_cjk_fonts,
    embed_background_image,
    extract_and_validate_svg,
    prepare_page_svg,
)
from tests.helpers import PNG_1X1

SAMPLE = """
前导文字
<svg width="1600" height="900">
  <rect width="1600" height="900" fill="#0f172a"/>
  <text x="80" y="120" font-size="42" fill="#ffffff">标题</text>
</svg>
收尾
""".strip()


def test_extract_and_validate_forces_canvas():
    svg = extract_and_validate_svg(SAMPLE)
    assert 'viewBox="0 0 1280 720"' in svg
    assert 'width="1280"' in svg
    assert 'height="720"' in svg


def test_extract_and_validate_rejects_missing_svg():
    with pytest.raises(RuntimeError, match="没有返回有效"):
        extract_and_validate_svg("not an svg document")


def test_extract_and_validate_rejects_malformed_xml():
    with pytest.raises(RuntimeError, match="不是合法 XML"):
        extract_and_validate_svg("<svg><rect></svg>")


def test_apply_cjk_fonts_sets_stack():
    svg = apply_cjk_fonts(extract_and_validate_svg(SAMPLE))
    assert "Microsoft YaHei" in svg
    assert CJK_FONT_STACK.split(",")[0].strip('"') in svg


def test_embed_background_image(tmp_path: Path):
    image_path = tmp_path / "bg.png"
    image_path.write_bytes(PNG_1X1)
    svg = embed_background_image(extract_and_validate_svg(SAMPLE), image_path)
    assert "data:image/png;base64," in svg
    assert svg.index("<image") < svg.index("<rect")


def test_prepare_page_svg_rejects_missing_background(tmp_path: Path):
    with pytest.raises(RuntimeError, match="背景图不存在"):
        prepare_page_svg(SAMPLE, background_path=tmp_path / "missing.png")
