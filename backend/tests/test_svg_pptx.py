from __future__ import annotations

import pytest
import zipfile
from pathlib import Path

from app.services.export import build_pptx
from app.services.svg_pptx import parse_path_contours


SAMPLE_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" width="1280" height="720">
  <rect x="80" y="80" width="400" height="200" rx="12" fill="#003366"/>
  <text x="100" y="140" font-size="36" fill="#ffffff">导出测试</text>
  <line x1="80" y1="300" x2="480" y2="300" stroke="#003366" stroke-width="2"/>
</svg>
""".strip()


def test_parse_path_contours_supports_hvz():
    contours = parse_path_contours("M0 0 H10 V10 Z")
    assert len(contours) == 1
    points, closed = contours[0]
    assert closed is True
    assert points[0] == (0.0, 0.0)
    assert (10.0, 0.0) in points
    assert (10.0, 10.0) in points


def test_build_pptx_shapes_writes_native_text_and_geometry(tmp_path: Path):
    export_path = tmp_path / "deck.pptx"
    build_pptx([("page-01", SAMPLE_SVG)], export_path, mode="shapes")
    with zipfile.ZipFile(export_path) as archive:
        presentation = archive.read("ppt/presentation.xml").decode("utf-8")
        assert 'cx="12192000"' in presentation
        slide = archive.read("ppt/slides/slide1.xml").decode("utf-8")
        assert "<a:t>导出测试</a:t>" in slide
        assert "roundRect" in slide or "rect" in slide
        assert not any(name.endswith(".png") for name in archive.namelist())


def test_build_pptx_image_mode_still_embeds_raster(tmp_path: Path):
    export_path = tmp_path / "deck-image.pptx"
    build_pptx([("page-01", SAMPLE_SVG)], export_path, mode="image")
    with zipfile.ZipFile(export_path) as archive:
        assert any(name.endswith(".png") for name in archive.namelist())
        assert any(name.endswith(".svg") for name in archive.namelist())
        slide = archive.read("ppt/slides/slide1.xml").decode("utf-8")
        assert "svgBlip" in slide


def test_build_pptx_rejects_unknown_element(tmp_path: Path):
    with pytest.raises(RuntimeError, match="无法翻译"):
        build_pptx(
            [("page-01", '<svg viewBox="0 0 1280 720"><foo/></svg>')],
            tmp_path / "bad.pptx",
            mode="shapes",
        )


def test_build_pptx_emits_tspan_coordinates_as_native_text(tmp_path: Path):
    svg = """
    <svg viewBox="0 0 1280 720">
      <text x="10" y="20" font-size="16" fill="#111111">a<tspan x="30" y="40">b</tspan></text>
    </svg>
    """
    export_path = tmp_path / "tspan.pptx"
    build_pptx([("page-01", svg)], export_path, mode="shapes")
    with zipfile.ZipFile(export_path) as archive:
        slide = archive.read("ppt/slides/slide1.xml").decode("utf-8")
        assert "<a:t>a</a:t>" in slide
        assert "<a:t>b</a:t>" in slide


def test_build_pptx_emits_wrapped_tspans_inside_translated_group(tmp_path: Path):
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <g transform="translate(200,80)">
        <text x="10" y="20" font-size="16" fill="#111111">
          <tspan x="14" dy="0">hello</tspan>
          <tspan x="14" dy="22">world</tspan>
        </text>
      </g>
    </svg>
    """
    export_path = tmp_path / "wrap.pptx"
    build_pptx([("page-01", svg)], export_path, mode="shapes")
    with zipfile.ZipFile(export_path) as archive:
        slide = archive.read("ppt/slides/slide1.xml").decode("utf-8")
        assert "<a:t>hello</a:t>" in slide
        assert "<a:t>world</a:t>" in slide


def test_build_pptx_rejects_file_image_href(tmp_path: Path):
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"not-an-image")
    svg = f'<svg viewBox="0 0 1280 720"><image href="{secret.as_posix()}" x="0" y="0" width="10" height="10"/></svg>'
    with pytest.raises(RuntimeError, match="data:image"):
        build_pptx([("page-01", svg)], tmp_path / "image.pptx", mode="shapes")


def test_decode_image_href_rejects_oversize(monkeypatch):
    import base64

    from app.services import svg_pptx

    monkeypatch.setattr(svg_pptx, "MAX_IMAGE_BYTES", 16)
    payload = base64.b64encode(b"x" * 32).decode("ascii")
    with pytest.raises(RuntimeError, match="8MB"):
        svg_pptx._decode_image_href(f"data:image/png;base64,{payload}")
