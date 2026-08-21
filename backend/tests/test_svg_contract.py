from __future__ import annotations

import pytest

from app.services.generation import STYLE_PACKS
from app.services.svg import _ensure_svg_xmlns, _parse_svg, prepare_page_svg
from app.services.svg_contract import SvgContractError, expand_token_classes, flatten_group_translates, validate_svg_contract
from app.services.svg_pptx import parse_path_contours


VALID_DESIGN = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <rect x="80" y="80" width="400" height="200" rx="12" class="c-surface"/>
  <text x="100" y="140" class="t-title">标题</text>
  <line x1="80" y1="300" x2="480" y2="300" class="c-accent"/>
</svg>
""".strip()


def test_design_rejects_literal_hex():
    svg = '<svg viewBox="0 0 1280 720"><rect x="0" y="80" width="100" height="100" fill="#003366"/></svg>'
    with pytest.raises(SvgContractError, match="literal_color"):
        validate_svg_contract(svg, stage="design", style_pack=STYLE_PACKS["consulting"])


def test_design_rejects_filter_and_curve():
    svg = """
    <svg viewBox="0 0 1280 720">
      <filter id="glow"><feGaussianBlur stdDeviation="2"/></filter>
      <path d="M10 10 C 20 20, 40 20, 50 10" class="c-accent"/>
    </svg>
    """
    with pytest.raises(SvgContractError):
        validate_svg_contract(svg, stage="design", style_pack=STYLE_PACKS["consulting"])


def test_design_rejects_full_bleed_background():
    svg = """
    <svg viewBox="0 0 1280 720">
      <rect x="0" y="0" width="1280" height="720" class="c-bg"/>
      <text x="80" y="80" class="t-title">标题</text>
    </svg>
    """
    with pytest.raises(SvgContractError, match="full_bleed"):
        validate_svg_contract(svg, stage="design", style_pack=STYLE_PACKS["consulting"])


def test_draft_allows_hex_but_rejects_filter():
    svg = '<svg viewBox="0 0 1280 720"><rect width="100" height="100" fill="#111111"/></svg>'
    validate_svg_contract(svg, stage="draft")
    bad = '<svg viewBox="0 0 1280 720"><filter id="a"/><rect width="10" height="10" fill="#111"/></svg>'
    with pytest.raises(SvgContractError, match="forbidden_element"):
        validate_svg_contract(bad, stage="draft")


def test_expand_tokens_writes_presentation_attrs():
    expanded = expand_token_classes(VALID_DESIGN, STYLE_PACKS["consulting"])
    assert 'fill="#EEF4F8"' in expanded
    assert 'stroke="#003366"' in expanded
    assert 'font-size="36px"' in expanded


def test_prepare_design_svg_injects_chrome():
    svg = prepare_page_svg(
        VALID_DESIGN,
        stage="design",
        style_pack=STYLE_PACKS["consulting"],
        chrome={"page_title": "实践路径", "page_index": 3, "page_count": 8, "page_role": "content"},
    )
    assert 'data-chrome="background"' in svg
    assert 'data-chrome="title_bar"' in svg
    assert "3 / 8" in svg
    assert "实践路径" in svg


def test_flatten_converts_relative_path_under_translate():
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><g transform="translate(10,20)"><path d="m0 0 l30 0"/></g></svg>'
    root = _parse_svg(_ensure_svg_xmlns(svg))
    flatten_group_translates(root)
    path = next(elem for elem in root.iter() if elem.tag.endswith("path"))
    points, _closed = parse_path_contours(path.get("d") or "")[0]
    assert points[0] == (10.0, 20.0)
    assert (40.0, 20.0) in points
