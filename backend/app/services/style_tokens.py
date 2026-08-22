from __future__ import annotations

from typing import Any

from app.services.svg import CJK_FONT_STACK

COLOR_CLASS_NAMES = (
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
)
SEMANTIC_TEXT_TOKENS = (
    "t-display",
    "t-page-title",
    "t-card-title",
    "t-kpi",
    "t-kpi-unit",
    "t-body",
    "t-caption",
    "t-label",
    "t-table-header",
    "t-toc-item",
)
LEGACY_TEXT_TOKENS = ("t-title", "t-subtitle")
TOKEN_CLASS_NAMES = COLOR_CLASS_NAMES + SEMANTIC_TEXT_TOKENS + LEGACY_TEXT_TOKENS + ("shadow-sm",)

TEXT_ROLE_TO_TOKEN = {
    "display": "t-display",
    "page-title": "t-page-title",
    "card-title": "t-card-title",
    "kpi": "t-kpi",
    "kpi-unit": "t-kpi-unit",
    "body": "t-body",
    "caption": "t-caption",
    "label": "t-label",
    "table-header": "t-table-header",
    "toc-item": "t-toc-item",
}
TITLE_ONLY_ROLES = {"display", "page-title"}
FORBIDDEN_LEGACY_TITLE_ROLES = {"card-title", "kpi", "kpi-unit", "toc-item", "table-header"}

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
    title_weight = str(type_spec.get("title_weight") or "700")
    body_weight = str(type_spec.get("body_weight") or "400")
    roles = derive_role_scale(type_spec)
    token_map: dict[str, dict[str, str]] = {
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
        "shadow-sm": {},
    }
    families = {
        "t-display": title_family,
        "t-page-title": title_family,
        "t-card-title": title_family,
        "t-kpi": title_family,
        "t-title": title_family,
        "t-subtitle": title_family,
        "t-toc-item": title_family,
        "t-table-header": title_family,
        "t-kpi-unit": body_family,
        "t-body": body_family,
        "t-caption": body_family,
        "t-label": body_family,
    }
    weights = {
        "t-display": title_weight,
        "t-page-title": title_weight,
        "t-card-title": "600",
        "t-kpi": title_weight,
        "t-title": title_weight,
        "t-subtitle": "600",
        "t-toc-item": "600",
        "t-table-header": "600",
        "t-kpi-unit": body_weight,
        "t-body": body_weight,
        "t-caption": "400",
        "t-label": "600",
    }
    fills = {
        "t-display": ink1,
        "t-page-title": ink1,
        "t-card-title": ink1,
        "t-kpi": ink1,
        "t-title": ink1,
        "t-subtitle": ink1,
        "t-toc-item": ink1,
        "t-table-header": ink1,
        "t-kpi-unit": ink2,
        "t-body": ink2,
        "t-caption": ink3,
        "t-label": accent,
    }
    for name in SEMANTIC_TEXT_TOKENS + LEGACY_TEXT_TOKENS:
        spec = roles[name]
        token_map[name] = {
            "fill": fills[name],
            "font-size": f"{spec['size_px']}px",
            "font-weight": weights[name],
            "font-family": families[name],
        }
    return token_map


def derive_role_scale(typography: dict[str, Any] | None) -> dict[str, dict[str, int]]:
    type_spec = typography or {}
    title = _as_int(type_spec.get("title_size_px"), 36)
    body = _as_int(type_spec.get("body_size_px"), 16)
    defaults = {
        "t-display": {"size_px": min(title + 6, 56), "min_px": 28, "max_px": 56},
        "t-page-title": {"size_px": title, "min_px": 26, "max_px": 48},
        "t-card-title": {"size_px": max(min(body + 4, 22), 16), "min_px": 16, "max_px": 24},
        "t-kpi": {"size_px": max(body * 2, 24), "min_px": 20, "max_px": 48},
        "t-kpi-unit": {"size_px": body, "min_px": 12, "max_px": 18},
        "t-subtitle": {"size_px": max(title - 14, 18), "min_px": 14, "max_px": 28},
        "t-body": {"size_px": body, "min_px": 14, "max_px": 20},
        "t-caption": {"size_px": max(body - 3, 11), "min_px": 11, "max_px": 14},
        "t-label": {"size_px": 12, "min_px": 10, "max_px": 14},
        "t-table-header": {"size_px": max(body, 14), "min_px": 12, "max_px": 18},
        "t-toc-item": {"size_px": max(body + 2, 16), "min_px": 14, "max_px": 22},
        "t-title": {"size_px": title, "min_px": 26, "max_px": 48},
    }
    defaults["t-title"] = dict(defaults["t-page-title"])
    overlay = type_spec.get("roles") if isinstance(type_spec.get("roles"), dict) else {}
    for name, spec in defaults.items():
        raw = overlay.get(name)
        if not isinstance(raw, dict):
            continue
        if raw.get("size_px") is not None:
            spec["size_px"] = _as_int(raw.get("size_px"), spec["size_px"])
        if raw.get("min_px") is not None:
            spec["min_px"] = _as_int(raw.get("min_px"), spec["min_px"])
        if raw.get("max_px") is not None:
            spec["max_px"] = _as_int(raw.get("max_px"), spec["max_px"])
    return defaults


def min_font_px_for_token(token: str | None, typography: dict[str, Any] | None = None) -> float:
    if not token:
        return 10.0
    spec = derive_role_scale(typography).get(token)
    if spec:
        return float(spec["min_px"])
    return 10.0


def default_min_font_map(typography: dict[str, Any] | None = None) -> dict[str, float]:
    return {name: float(spec["min_px"]) for name, spec in derive_role_scale(typography).items()}


def resolve_text_token(class_names: list[str], text_role: str | None) -> str | None:
    role_token = TEXT_ROLE_TO_TOKEN.get((text_role or "").strip())
    if role_token:
        return role_token
    for name in class_names:
        if name.startswith("t-"):
            return name
    return None


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
            "t-display": "封面/章节/结束页主标题，每页最多一个",
            "t-page-title": "内容页主标题，每页最多一个",
            "t-card-title": "卡片和模块标题，不得用于 KPI",
            "t-kpi": "关键数字",
            "t-kpi-unit": "KPI 单位",
            "t-title": "兼容旧标题，只允许页主标题",
            "t-subtitle": "副标题字阶",
            "t-body": "正文字阶",
            "t-caption": "来源、注释、辅助说明",
            "t-label": "标签/徽章/系统 chrome 小标题",
            "t-table-header": "表头",
            "t-toc-item": "目录项",
            "shadow-sm": "轻投影意图，不要用 filter",
        },
    }


def _as_int(raw: Any, default: int) -> int:
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


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
