from __future__ import annotations

from typing import Any

from app.services.svg import CJK_FONT_STACK

TOKEN_CLASS_NAMES = (
    "c-bg",
    "c-surface",
    "c-surface-alt",
    "c-accent",
    "c-accent-8",
    "c-accent-16",
    "c-white",
    "c-ink-1",
    "c-ink-2",
    "c-ink-3",
    "t-title",
    "t-subtitle",
    "t-body",
    "t-caption",
    "t-label",
    "shadow-sm",
)

_FALLBACK_PALETTE = {
    "background": "#F8FAFC",
    "surface": "#FFFFFF",
    "surface_alt": "#E2E8F0",
    "text_primary": "#0F172A",
    "text_secondary": "#475569",
    "accent_primary": "#2563EB",
}


def build_token_map(
    palette: dict[str, str] | None,
    typography: dict[str, Any] | None = None,
) -> dict[str, dict[str, str]]:
    colors = {**_FALLBACK_PALETTE, **(palette or {})}
    accent = _norm_hex(colors["accent_primary"])
    ink1 = _norm_hex(colors["text_primary"])
    ink2 = _norm_hex(colors["text_secondary"])
    bg = _norm_hex(colors["background"])
    ink3 = _mix_hex(ink2, bg, 0.35)
    type_spec = typography or {}
    title_family = str(type_spec.get("title_family") or CJK_FONT_STACK)
    body_family = str(type_spec.get("body_family") or CJK_FONT_STACK)
    title_size = str(type_spec.get("title_size_px") or 36)
    body_size = str(type_spec.get("body_size_px") or 16)
    subtitle_size = str(max(int(float(title_size)) - 14, 18))
    caption_size = str(max(int(float(body_size)) - 3, 11))
    return {
        "c-bg": {"fill": bg},
        "c-surface": {"fill": _norm_hex(colors["surface"])},
        "c-surface-alt": {"fill": _norm_hex(colors["surface_alt"])},
        "c-accent": {"fill": accent},
        "c-accent-8": {"fill": accent, "fill-opacity": "0.08"},
        "c-accent-16": {"fill": accent, "fill-opacity": "0.16"},
        "c-white": {"fill": "#FFFFFF"},
        "c-ink-1": {"fill": ink1},
        "c-ink-2": {"fill": ink2},
        "c-ink-3": {"fill": ink3},
        "t-title": {
            "fill": ink1,
            "font-size": f"{title_size}px",
            "font-weight": str(type_spec.get("title_weight") or "700"),
            "font-family": title_family,
        },
        "t-subtitle": {"fill": ink1, "font-size": f"{subtitle_size}px", "font-weight": "600", "font-family": title_family},
        "t-body": {
            "fill": ink2,
            "font-size": f"{body_size}px",
            "font-weight": str(type_spec.get("body_weight") or "400"),
            "font-family": body_family,
        },
        "t-caption": {"fill": ink3, "font-size": f"{caption_size}px", "font-weight": "400", "font-family": body_family},
        "t-label": {"fill": accent, "font-size": "12px", "font-weight": "600", "font-family": body_family},
        "shadow-sm": {},
    }


def style_pack_for_prompt(style_pack: dict[str, Any]) -> dict[str, Any]:
    return {
        "style_id": style_pack.get("style_id"),
        "style_name": style_pack.get("style_name"),
        "description": style_pack.get("description"),
        "token_classes": list(TOKEN_CLASS_NAMES),
        "tags": list(style_pack.get("tags") or []),
        "name_en": style_pack.get("name_en") or "",
        "token_roles": {
            "c-bg": "页面底色，由系统合成，不要自己画全幅背景",
            "c-surface": "卡片/面板底",
            "c-surface-alt": "次级面板底",
            "c-accent": "主强调色",
            "c-accent-8": "主色 8% 透明浅底",
            "c-accent-16": "主色 16% 透明浅底",
            "c-white": "反白",
            "c-ink-1": "主文字",
            "c-ink-2": "次文字",
            "c-ink-3": "辅助文字",
            "t-title": "标题字阶",
            "t-subtitle": "副标题字阶",
            "t-body": "正文字阶",
            "t-caption": "说明文字",
            "t-label": "标签/徽章",
            "shadow-sm": "轻投影意图，不要用 filter",
        },
    }


def _norm_hex(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("#") and len(text) == 4:
        return "#" + "".join(ch * 2 for ch in text[1:]).upper()
    if text.startswith("#") and len(text) == 7:
        return text.upper()
    raise RuntimeError(f"非法色值: {value}")


def _mix_hex(left: str, right: str, t: float) -> str:
    lrgb = _hex_to_rgb(left)
    rrgb = _hex_to_rgb(right)
    mixed = tuple(round(a + (b - a) * t) for a, b in zip(lrgb, rrgb))
    return "#{:02X}{:02X}{:02X}".format(*mixed)


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    raw = _norm_hex(value)[1:]
    return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)
