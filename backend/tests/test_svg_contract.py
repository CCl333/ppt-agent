from __future__ import annotations

import re

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


def test_expand_tokens_can_keep_draft_font_size():
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <text class="t-page-title" data-node-id="page-title" data-text-role="page-title" font-size="32px">半日游路线总览</text>
    </svg>
    """
    expanded = expand_token_classes(svg, STYLE_PACKS["consulting"], preserve_font_size=True)
    assert 'font-size="32px"' in expanded
    assert 'font-size="36px"' not in expanded
    assert 'font-size="40px"' not in expanded
    assert 'fill="' in expanded


def test_prepare_design_keeps_draft_font_size_not_style_pack():
    draft = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <style>.t-page-title { font-size: 32px; }</style>
      <text class="t-page-title" data-node-id="page-title" data-text-role="page-title">半日游路线总览</text>
    </svg>
    """
    design = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <text class="t-page-title" data-node-id="page-title" data-text-role="page-title">半日游路线总览</text>
    </svg>
    """
    prepared = prepare_page_svg(
        design,
        stage="design",
        style_pack=STYLE_PACKS["consulting"],
        chrome={"page_title": "半日游路线总览", "page_index": 4, "page_count": 10},
        draft_svg=draft,
    )
    assert 'data-node-id="page-title"' in prepared
    title = re.search(r"<text[^>]*data-node-id=\"page-title\"[^>]*>", prepared)
    assert title is not None
    assert 'font-size="32px"' in title.group(0)
    assert 'font-size="40px"' not in title.group(0)
    assert 'font-size="36px"' not in title.group(0)


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
    assert 'data-chrome="page_title"' not in svg
    assert "实践路径" not in svg


def test_prepare_design_keeps_draft_panel_fill_when_model_drops_rect():
    draft = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <rect x="900" y="140" width="280" height="96" rx="12" fill="#f0fdfa"/>
      <text x="920" y="190" font-size="20">4-5 小时</text>
    </svg>
    """
    design = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <text x="920" y="190" class="t-kpi" data-node-id="kpi-1" data-text-role="kpi">4-5 小时</text>
    </svg>
    """
    prepared = prepare_page_svg(
        design,
        stage="design",
        style_pack=STYLE_PACKS["consulting"],
        chrome={"page_title": "半日游路线总览", "page_index": 4, "page_count": 10},
        draft_svg=draft,
    )
    assert 'data-retained-panel="1"' in prepared
    assert 'class="c-accent-8"' in prepared
    assert 'fill-opacity="0.08"' in prepared
    assert 'x="900"' in prepared
    assert 'y="140"' in prepared


def test_prepare_design_remaps_white_panel_to_tinted_token():
    draft = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <rect x="900" y="140" width="280" height="96" rx="12" fill="#ccfbf1"/>
      <text x="920" y="190">4-5 小时</text>
    </svg>
    """
    design = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <rect x="900" y="140" width="280" height="96" rx="12" class="c-white"/>
      <text x="920" y="190" class="t-kpi" data-node-id="kpi-1" data-text-role="kpi">4-5 小时</text>
    </svg>
    """
    prepared = prepare_page_svg(
        design,
        stage="design",
        style_pack=STYLE_PACKS["consulting"],
        chrome={"page_title": "半日游路线总览", "page_index": 4, "page_count": 10},
        draft_svg=draft,
    )
    assert 'class="c-accent-8"' in prepared
    assert 'class="c-white"' not in prepared
    assert 'fill-opacity="0.08"' in prepared


def test_prepare_design_upgrades_css_card_bg_to_visible_surface():
    draft = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <style>.card-bg { fill: #ffffff; stroke: #e2e8f0; } .inner-panel { fill: #f8fafc; }</style>
      <rect class="card-bg" x="60" y="122" width="750" height="288" rx="14"/>
      <rect class="inner-panel" x="84" y="182" width="160" height="64" rx="8"/>
    </svg>
    """
    design = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <rect class="c-surface" x="60" y="122" width="750" height="288" rx="14"/>
      <text x="108" y="156" class="t-card-title" data-node-id="block-1-title" data-text-role="card-title">推荐主线</text>
    </svg>
    """
    prepared = prepare_page_svg(
        design,
        stage="design",
        style_pack=STYLE_PACKS["consulting"],
        chrome={"page_title": "半日游路线总览", "page_index": 4, "page_count": 10},
        draft_svg=draft,
    )
    assert prepared.count("c-surface-alt") >= 2
    assert 'width="750"' in prepared
    assert 'width="160"' in prepared


def test_prepare_design_applies_draft_badge_size_to_label():
    draft = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <style>.t-badge { font-size: 12px; }</style>
      <rect x="60" y="44" width="80" height="22" rx="4" fill="#ccfbf1"/>
      <text class="t-badge" x="68" y="59">经典半日游</text>
    </svg>
    """
    design = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <rect x="60" y="44" width="80" height="22" rx="4" class="c-accent-16"/>
      <text class="t-label" x="68" y="59">经典半日游</text>
    </svg>
    """
    prepared = prepare_page_svg(
        design,
        stage="design",
        style_pack=STYLE_PACKS["consulting"],
        chrome={"page_title": "半日游路线总览", "page_index": 4, "page_count": 10},
        draft_svg=draft,
    )
    label = re.search(r"<text[^>]*>经典半日游</text>", prepared)
    assert label is not None
    assert 'font-size="12px"' in label.group(0)


def test_flatten_shifts_tspan_x_under_translate():
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg">
      <g transform="translate(200,80)">
        <text x="10" y="20">
          <tspan x="14" dy="22">hello</tspan>
        </text>
      </g>
    </svg>
    """
    root = _parse_svg(_ensure_svg_xmlns(svg))
    flatten_group_translates(root)
    text = next(elem for elem in root.iter() if elem.tag.endswith("text"))
    tspan = next(elem for elem in root.iter() if elem.tag.endswith("tspan"))
    assert text.get("x") == "210"
    assert text.get("y") == "100"
    assert tspan.get("x") == "214"
    assert tspan.get("dy") == "22"


def test_flatten_converts_relative_path_under_translate():
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><g transform="translate(10,20)"><path d="m0 0 l30 0"/></g></svg>'
    root = _parse_svg(_ensure_svg_xmlns(svg))
    flatten_group_translates(root)
    path = next(elem for elem in root.iter() if elem.tag.endswith("path"))
    points, _closed = parse_path_contours(path.get("d") or "")[0]
    assert points[0] == (10.0, 20.0)
    assert (40.0, 20.0) in points


def test_draft_allows_defs_and_style():
    svg = """
    <svg viewBox="0 0 1280 720">
      <defs>
        <style>.x{font-family:Microsoft YaHei}</style>
      </defs>
      <rect width="100" height="100" fill="#111111"/>
    </svg>
    """
    validate_svg_contract(svg, stage="draft")


def test_design_rejects_defs_and_style():
    svg = """
    <svg viewBox="0 0 1280 720">
      <defs>
        <style>.x{font-family:Microsoft YaHei}</style>
      </defs>
      <rect x="80" y="80" width="100" height="100" class="c-surface"/>
    </svg>
    """
    with pytest.raises(SvgContractError, match="forbidden_element"):
        validate_svg_contract(svg, stage="design", style_pack=STYLE_PACKS["consulting"])


def test_draft_still_rejects_filter():
    svg = '<svg viewBox="0 0 1280 720"><filter id="a"/><rect width="10" height="10" fill="#111"/></svg>'
    with pytest.raises(SvgContractError, match="forbidden_element"):
        validate_svg_contract(svg, stage="draft")


def test_draft_allows_gradient_inside_defs():
    svg = """
    <svg viewBox="0 0 1280 720">
      <defs>
        <linearGradient id="g">
          <stop offset="0" stop-color="#2563EB"/>
          <stop offset="1" stop-color="#93C5FD"/>
        </linearGradient>
        <marker id="arrow" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
          <path d="M0 0 L6 3 L0 6 Z" fill="#111"/>
        </marker>
      </defs>
      <rect width="100" height="100" fill="url(#g)"/>
    </svg>
    """
    validate_svg_contract(svg, stage="draft")


def test_draft_rejects_gradient_outside_defs():
    svg = """
    <svg viewBox="0 0 1280 720">
      <linearGradient id="g">
        <stop offset="0" stop-color="#2563EB"/>
      </linearGradient>
      <rect width="100" height="100" fill="#111"/>
    </svg>
    """
    with pytest.raises(SvgContractError, match="forbidden_element"):
        validate_svg_contract(svg, stage="draft")
