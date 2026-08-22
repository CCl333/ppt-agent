from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import StyleLibraryEntry
from app.services.font_policy import lookup_font, normalize_family
from app.services.style_tokens import _norm_hex
from app.services.svg import CJK_FONT_STACK

MAX_UNIQUE_COLORS = 8
MAX_CANDIDATES = 3
_SLUG_RE = re.compile(r"[^a-z0-9-]+")
_TOKEN_TO_PALETTE = {
    "c-bg": "background",
    "c-surface": "surface",
    "c-surface-alt": "surface_alt",
    "c-accent": "accent_primary",
    "c-ink-1": "text_primary",
    "c-ink-2": "text_secondary",
}
_FIXED_CHROME = ("background", "title_bar", "page_number")


def slugify_style_id(raw: str, fallback: str) -> str:
    value = _SLUG_RE.sub("-", str(raw or "").strip().lower()).strip("-")
    return value or fallback


def unique_colors(palette: dict[str, str]) -> list[str]:
    seen: list[str] = []
    for value in palette.values():
        hex_value = _norm_hex(value)
        if hex_value not in seen:
            seen.append(hex_value)
    return seen


def pack_from_card(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "style_id": card["style_id"],
        "style_name": card["name"],
        "description": card.get("rationale") or card.get("description") or "",
        "palette": dict(card["palette"]),
        "typography": dict(card.get("typography") or {}),
        "tags": list(card.get("tags") or []),
        "name_en": card.get("name_en") or "",
        "texture": card.get("texture") or "none",
        "fixed_chrome": list(card.get("fixed_chrome") or _FIXED_CHROME),
        "source": card.get("source") or "library",
    }


def card_from_preset(pack: dict[str, Any], *, source: str = "builtin") -> dict[str, Any]:
    style_id = str(pack.get("style_id") or "").strip()
    if not style_id:
        raise RuntimeError("风格预设缺少 style_id")
    palette = {
        "background": _norm_hex(pack["palette"]["background"]),
        "surface": _norm_hex(pack["palette"]["surface"]),
        "surface_alt": _norm_hex(pack["palette"]["surface_alt"]),
        "text_primary": _norm_hex(pack["palette"]["text_primary"]),
        "text_secondary": _norm_hex(pack["palette"]["text_secondary"]),
        "accent_primary": _norm_hex(pack["palette"]["accent_primary"]),
    }
    return normalize_style_card(
        {
            "id": style_id,
            "name": pack.get("style_name") or style_id,
            "name_en": pack.get("name_en") or style_id,
            "rationale": pack.get("description") or "",
            "tags": pack.get("tags") or [pack.get("style_name") or style_id],
            "palette": palette,
            "texture": pack.get("texture") or "none",
            "fixed_chrome": list(_FIXED_CHROME),
        },
        source=source,
        fallback_id=style_id,
    )


def normalize_style_card(payload: Any, *, source: str, fallback_id: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError("风格卡必须是 JSON 对象")
    style_id = slugify_style_id(str(payload.get("id") or payload.get("style_id") or ""), fallback_id)
    name = str(payload.get("name") or payload.get("style_name") or "").strip()
    if not name:
        raise RuntimeError("风格卡缺少 name")
    rationale = str(payload.get("rationale") or payload.get("description") or "").strip()
    tags = [str(item).strip() for item in (payload.get("tags") or []) if str(item).strip()]
    palette = _palette_from_payload(payload)
    colors = unique_colors(palette)
    tokens = payload.get("tokens") if isinstance(payload.get("tokens"), dict) else {}
    for value in tokens.values():
        if isinstance(value, str) and value.strip().startswith("#"):
            hex_value = _norm_hex(value)
            if hex_value not in colors:
                colors.append(hex_value)
    if len(colors) > MAX_UNIQUE_COLORS:
        raise RuntimeError(f"风格卡颜色超过 {MAX_UNIQUE_COLORS} 个上限（当前 {len(colors)}）")
    typography = _typography_from_payload(payload)
    texture = str(payload.get("texture") or "none").strip() or "none"
    chrome = [str(item).strip() for item in (payload.get("fixed_chrome") or _FIXED_CHROME) if str(item).strip()]
    if not chrome:
        chrome = list(_FIXED_CHROME)
    return {
        "style_id": style_id,
        "name": name,
        "name_en": str(payload.get("name_en") or style_id).strip() or style_id,
        "rationale": rationale,
        "tags": tags,
        "palette": palette,
        "typography": typography,
        "texture": texture,
        "fixed_chrome": chrome,
        "source": source,
        "colors": colors,
    }


def normalize_style_cards(payload: Any, *, source: str) -> list[dict[str, Any]]:
    raw_cards = payload.get("cards") if isinstance(payload, dict) else payload
    if not isinstance(raw_cards, list) or not raw_cards:
        raise RuntimeError("风格卡生成结果必须是 1 到 3 张卡片")
    if len(raw_cards) > MAX_CANDIDATES:
        raise RuntimeError("一次最多生成 3 张风格卡")
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_cards, start=1):
        card = normalize_style_card(raw, source=source, fallback_id=f"card-{index}")
        if card["style_id"] in seen:
            card["style_id"] = f"{card['style_id']}-{index}"
        seen.add(card["style_id"])
        cards.append(card)
    return cards


def serialize_card(card: dict[str, Any] | None) -> dict[str, Any] | None:
    if not card:
        return None
    return {
        "style_id": card["style_id"],
        "name": card["name"],
        "name_en": card.get("name_en") or "",
        "rationale": card.get("rationale") or "",
        "tags": list(card.get("tags") or []),
        "palette": dict(card.get("palette") or {}),
        "typography": dict(card.get("typography") or {}),
        "texture": card.get("texture") or "none",
        "fixed_chrome": list(card.get("fixed_chrome") or _FIXED_CHROME),
        "source": card.get("source") or "library",
        "colors": list(card.get("colors") or unique_colors(card.get("palette") or {})),
    }


def seed_style_library(session: Session) -> None:
    from app.services.generation import STYLE_PACKS

    existing = {item.style_id for item in session.scalars(select(StyleLibraryEntry)).all()}
    for pack in STYLE_PACKS.values():
        card = card_from_preset(pack, source="builtin")
        if card["style_id"] in existing:
            continue
        session.add(
            StyleLibraryEntry(
                style_id=card["style_id"],
                name=card["name"],
                name_en=card["name_en"],
                builtin=True,
                card_json=card,
            )
        )
        existing.add(card["style_id"])
    session.flush()


def list_library_cards(session: Session) -> list[dict[str, Any]]:
    seed_style_library(session)
    rows = list(session.scalars(select(StyleLibraryEntry).order_by(StyleLibraryEntry.builtin.desc(), StyleLibraryEntry.name.asc())))
    return [serialize_card(row.card_json) for row in rows if isinstance(row.card_json, dict)]


def get_library_card(session: Session, style_id: str) -> dict[str, Any] | None:
    seed_style_library(session)
    row = session.get(StyleLibraryEntry, style_id)
    if row and isinstance(row.card_json, dict):
        return dict(row.card_json)
    return None


def save_card_to_library(session: Session, card: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_style_card(card, source="library", fallback_id=str(card.get("style_id") or "card"))
    existing = session.get(StyleLibraryEntry, normalized["style_id"])
    if existing and existing.builtin:
        normalized["style_id"] = slugify_style_id(f"{normalized['style_id']}-saved", f"{normalized['style_id']}-saved")
        existing = session.get(StyleLibraryEntry, normalized["style_id"])
    if existing:
        if existing.builtin:
            raise RuntimeError("不能覆盖内置风格库条目")
        existing.name = normalized["name"]
        existing.name_en = normalized["name_en"]
        existing.card_json = normalized
        session.flush()
        return serialize_card(normalized) or normalized
    session.add(
        StyleLibraryEntry(
            style_id=normalized["style_id"],
            name=normalized["name"],
            name_en=normalized["name_en"],
            builtin=False,
            card_json=normalized,
        )
    )
    session.flush()
    return serialize_card(normalized) or normalized


def _palette_from_payload(payload: dict[str, Any]) -> dict[str, str]:
    palette_raw = payload.get("palette") if isinstance(payload.get("palette"), dict) else {}
    tokens = payload.get("tokens") if isinstance(payload.get("tokens"), dict) else {}
    mapped: dict[str, str] = {}
    for token_name, palette_key in _TOKEN_TO_PALETTE.items():
        value = tokens.get(token_name)
        if isinstance(value, str) and value.strip():
            mapped[palette_key] = _norm_hex(value)
    for key in ("background", "surface", "surface_alt", "text_primary", "text_secondary", "accent_primary"):
        value = palette_raw.get(key)
        if isinstance(value, str) and value.strip():
            mapped.setdefault(key, _norm_hex(value))
    required = ("background", "accent_primary", "text_primary")
    missing = [key for key in required if key not in mapped]
    if missing:
        raise RuntimeError(f"风格卡缺少必要色值: {', '.join(missing)}")
    mapped.setdefault("text_secondary", mapped["text_primary"])
    mapped.setdefault("surface", mapped["background"])
    mapped.setdefault("surface_alt", mapped["surface"])
    return {
        "background": mapped["background"],
        "surface": mapped["surface"],
        "surface_alt": mapped["surface_alt"],
        "text_primary": mapped["text_primary"],
        "text_secondary": mapped["text_secondary"],
        "accent_primary": mapped["accent_primary"],
    }


def _typography_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    tokens = payload.get("tokens") if isinstance(payload.get("tokens"), dict) else {}
    title = tokens.get("t-title") if isinstance(tokens.get("t-title"), dict) else {}
    body = tokens.get("t-body") if isinstance(tokens.get("t-body"), dict) else {}
    title_family = _safe_family(title.get("family"))
    body_family = _safe_family(body.get("family"))
    return {
        "title_family": title_family,
        "body_family": body_family,
        "title_size_px": _safe_size(title.get("size_px"), 36),
        "body_size_px": _safe_size(body.get("size_px"), 16),
        "title_weight": str(title.get("weight") or "700"),
        "body_weight": str(body.get("weight") or "400"),
        "roles": _role_sizes_from_tokens(tokens, title.get("size_px"), body.get("size_px")),
    }


def _role_sizes_from_tokens(tokens: dict[str, Any], title_size: Any, body_size: Any) -> dict[str, dict[str, int]]:
    from app.services.style_tokens import derive_role_scale

    overlay: dict[str, dict[str, int]] = {}
    for name, spec in tokens.items():
        if not str(name).startswith("t-") or not isinstance(spec, dict):
            continue
        size = spec.get("size_px")
        if size is None:
            continue
        overlay[str(name)] = {"size_px": _safe_size(size, 16)}
    derived = derive_role_scale(
        {
            "title_size_px": _safe_size(title_size, 36),
            "body_size_px": _safe_size(body_size, 16),
            "roles": overlay,
        }
    )
    return derived


def _safe_family(raw: Any) -> str:
    family = normalize_family(str(raw or ""))
    if not family:
        return CJK_FONT_STACK
    policy = lookup_font(family)
    if policy is None:
        return f"{family}, {CJK_FONT_STACK}"
    return family


def _safe_size(raw: Any, default: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(10, min(value, 72))
