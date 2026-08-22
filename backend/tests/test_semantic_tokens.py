from __future__ import annotations

from app.services.generation import STYLE_PACKS
from app.services.page_quality import evaluate_page_quality
from app.services.style_tokens import build_token_map, derive_role_scale
from app.services.svg_contract import expand_token_classes


def test_role_scale_does_not_reuse_page_title_size_for_cards():
    roles = derive_role_scale({"title_size_px": 40, "body_size_px": 16})
    assert roles["t-page-title"]["size_px"] == 40
    assert roles["t-card-title"]["size_px"] <= 22
    assert roles["t-card-title"]["size_px"] < roles["t-page-title"]["size_px"]
    assert roles["t-kpi"]["size_px"] != roles["t-card-title"]["size_px"]
    assert roles["t-toc-item"]["size_px"] < roles["t-page-title"]["size_px"]


def test_layout_min_font_shares_role_scale_table():
    from app.services.layout_validator import MIN_FONT_PX
    from app.services.style_tokens import default_min_font_map, min_font_px_for_token

    expected = default_min_font_map()
    for token, min_px in expected.items():
        assert MIN_FONT_PX[token] == min_px
    assert min_font_px_for_token("t-page-title") == 26
    assert min_font_px_for_token("t-card-title") == 16
    overlay = {"roles": {"t-body": {"min_px": 15}}}
    assert min_font_px_for_token("t-body", overlay) == 15


def test_title_role_overlay_is_not_wiped_by_page_title_copy():
    roles = derive_role_scale(
        {
            "title_size_px": 40,
            "body_size_px": 16,
            "roles": {"t-title": {"size_px": 28}, "t-page-title": {"size_px": 40}},
        }
    )
    assert roles["t-page-title"]["size_px"] == 40
    assert roles["t-title"]["size_px"] == 28


def test_expand_remaps_legacy_title_class_using_text_role():
    pack = {
        **STYLE_PACKS["consulting"],
        "typography": {"title_size_px": 40, "body_size_px": 16},
    }
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <text class="t-title" data-text-role="page-title">页标题</text>
      <text class="t-title" data-text-role="card-title">卡标题</text>
      <text class="t-title" data-text-role="kpi">1500</text>
      <text class="t-title" data-text-role="toc-item">目录项</text>
    </svg>
    """
    expanded = expand_token_classes(svg, pack)
    token_map = build_token_map(pack["palette"], pack["typography"])
    assert f'font-size="{token_map["t-page-title"]["font-size"]}"' in expanded
    assert f'font-size="{token_map["t-card-title"]["font-size"]}"' in expanded
    assert f'font-size="{token_map["t-toc-item"]["font-size"]}"' in expanded
    assert token_map["t-card-title"]["font-size"] != "40px"
    assert token_map["t-toc-item"]["font-size"] != token_map["t-page-title"]["font-size"]
    assert token_map["t-kpi"]["font-size"] != token_map["t-card-title"]["font-size"]


def test_kpi_using_t_title_fails_semantic_role_gate():
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
      <text x="80" y="140" class="t-page-title" data-text-role="page-title" font-size="32px">行程预算</text>
      <text x="80" y="260" class="t-title" data-node-id="kpi-1" data-text-role="kpi" font-size="40px">1500</text>
    </svg>
    """
    report = evaluate_page_quality(svg, stage="draft")
    assert report["hard_fail"] is True
    codes = {
        violation.get("code")
        for check in report["checks"]
        for violation in check.get("violations") or []
    }
    assert "ROLE_TOKEN_MISMATCH" in codes
