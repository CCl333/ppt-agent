from __future__ import annotations

import pytest
from fastapi import HTTPException

from sqlalchemy import select

from app.models.entities import StyleLibraryEntry
from app.services.style_cards import MAX_UNIQUE_COLORS, normalize_style_card, normalize_style_cards
from tests.helpers import make_content_page, make_project


def _valid_tokens(**overrides):
    payload = {
        "id": "imperial-heritage",
        "name": "宫墙红影",
        "name_en": "Imperial Heritage",
        "rationale": "提取宫墙红做主色",
        "tags": ["故宫红", "极简中式"],
        "tokens": {
            "c-bg": "#FDFCFB",
            "c-accent": "#B0352F",
            "c-ink-1": "#1A1A1A",
            "c-ink-2": "#4A4A4A",
        },
    }
    payload.update(overrides)
    return payload


def test_normalize_style_card_maps_tokens_and_caps_colors():
    card = normalize_style_card(_valid_tokens(), source="generated", fallback_id="card-1")
    assert card["style_id"] == "imperial-heritage"
    assert card["palette"]["accent_primary"] == "#B0352F"
    assert len(card["colors"]) <= MAX_UNIQUE_COLORS


def test_normalize_rejects_too_many_unique_colors():
    tokens = {f"c-extra-{index}": f"#{index:02x}{index:02x}{index:02x}" for index in range(1, 10)}
    tokens.update({"c-bg": "#FDFCFB", "c-accent": "#B0352F", "c-ink-1": "#1A1A1A"})
    with pytest.raises(RuntimeError, match="超过"):
        normalize_style_card(_valid_tokens(tokens=tokens), source="generated", fallback_id="too-many")


def test_style_library_seeds_builtin_presets(db_session):
    rows = list(db_session.scalars(select(StyleLibraryEntry)))
    ids = {row.style_id for row in rows}
    assert "minimalism" in ids
    assert "consulting" in ids
    assert all(row.builtin for row in rows if row.style_id == "minimalism")


def test_generate_style_cards_blocked_before_outline_confirm(service, db_session):
    project = make_project(db_session, stage="outline")
    with pytest.raises(HTTPException) as exc:
        service.generate_style_cards(project.id)
    assert exc.value.status_code == 409


def test_generate_confirm_and_save_style_card(service, db_session, monkeypatch):
    project = make_project(db_session, stage="search")
    page = make_content_page(db_session, project, page_code="page-03", title="实践路径", sort_order=1)
    cards = normalize_style_cards({"cards": [_valid_tokens()]}, source="generated")
    monkeypatch.setattr(service.generator, "generate_style_cards", lambda **_kwargs: cards)

    generated = service.generate_style_cards(project.id)
    assert len(generated["candidates"]) == 1
    assert generated["frozen"] is None

    confirmed = service.confirm_style_card(project.id, "imperial-heritage", source="candidates")
    assert confirmed["frozen"]["style_id"] == "imperial-heritage"
    db_session.refresh(project)
    db_session.refresh(page)
    assert project.style_preset == "imperial-heritage"
    assert page.design_status == "stale"

    saved = service.save_style_card_to_library(project.id)
    assert saved["saved"]["style_id"] == "imperial-heritage"
    assert any(item["style_id"] == "imperial-heritage" for item in saved["library"])


def test_confirm_library_preset_freezes_builtin(service, db_session):
    project = make_project(db_session, stage="search", style_preset="consulting")
    result = service.confirm_style_card(project.id, "consulting", source="library")
    assert result["frozen"]["style_id"] == "consulting"
    assert result["frozen"]["palette"]["accent_primary"] == "#003366"
