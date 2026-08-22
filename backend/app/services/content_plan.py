from __future__ import annotations

import re
from typing import Any
from xml.etree import ElementTree as ET

from app.services.svg import _ensure_svg_xmlns, _local_tag, _parse_svg

_SKIP_CHROME = {"background", "texture", "page_number"}
MAX_LABEL_CHARS = 22
MAX_NOTE_CHARS = 22
MAX_SUBTITLE_CHARS = 40
CONTENT_BLOCK_MIN = 3
CONTENT_BLOCK_MAX = 5


def normalize_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def compact_char_count(text: str | None) -> int:
    return len(re.sub(r"\s+", "", str(text or "")))


def has_content_plan(plan: dict[str, Any] | None) -> bool:
    return bool(isinstance(plan, dict) and normalize_text(str(plan.get("title") or "")))


def normalize_content_plan(
    payload: Any,
    *,
    page_code: str,
    title: str,
    page_role: str = "content",
    page_images: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError("内容策划必须是 JSON 对象")
    plan_title = normalize_text(payload.get("title")) or normalize_text(title)
    if not plan_title:
        raise RuntimeError("内容策划缺少 title")
    subtitle = normalize_text(payload.get("subtitle"))
    if compact_char_count(subtitle) > MAX_SUBTITLE_CHARS:
        raise RuntimeError(f"内容策划 subtitle 超过 {MAX_SUBTITLE_CHARS} 字")
    blocks_raw = payload.get("blocks")
    if blocks_raw is None:
        blocks_raw = []
    if not isinstance(blocks_raw, list):
        raise RuntimeError("内容策划 blocks 必须是数组")
    blocks: list[dict[str, str]] = []
    for item in blocks_raw:
        if not isinstance(item, dict):
            raise RuntimeError("内容策划 block 必须是对象")
        label = normalize_text(item.get("label"))
        if not label:
            raise RuntimeError("内容策划 block 缺少 label")
        note = normalize_text(item.get("note"))
        _assert_block_density(label, note)
        blocks.append(
            {
                "role": normalize_text(item.get("role")) or "point",
                "label": label,
                "note": note,
            }
        )
    footer_raw = payload.get("footer") if isinstance(payload.get("footer"), dict) else {}
    presenter = normalize_text(footer_raw.get("presenter"))
    date = normalize_text(footer_raw.get("date"))
    footer = {key: value for key, value in (("presenter", presenter), ("date", date)) if value}
    if page_role in {"content", "toc", "section"}:
        footer = {}
    if page_role == "content" and not blocks:
        raise RuntimeError("内容页策划稿缺少 blocks")
    if page_role == "content" and not (CONTENT_BLOCK_MIN <= len(blocks) <= CONTENT_BLOCK_MAX):
        raise RuntimeError(f"内容策划 blocks 数量必须为 {CONTENT_BLOCK_MIN}～{CONTENT_BLOCK_MAX}")
    if page_role == "section" and not blocks and not subtitle:
        raise RuntimeError("章节页策划稿缺少 subtitle 或预告要点")
    if page_role in {"cover", "end", "toc"} and not blocks and not subtitle:
        raise RuntimeError("封面/目录/结尾策划稿缺少 subtitle 或价值点")
    catalog_ids = {
        str(item.get("image_id") or "").strip()
        for item in page_images or []
        if isinstance(item, dict) and str(item.get("image_id") or "").strip()
    }
    slots: list[dict[str, str]] = []
    raw_slots = payload.get("image_slots") or []
    if raw_slots and not isinstance(raw_slots, list):
        raise RuntimeError("内容策划 image_slots 必须是数组")
    for item in raw_slots:
        if not isinstance(item, dict):
            raise RuntimeError("内容策划 image_slot 必须是对象")
        image_id = normalize_text(item.get("image_id"))
        if not image_id:
            raise RuntimeError("内容策划 image_slot 缺少 image_id")
        if image_id not in catalog_ids:
            raise RuntimeError(f"内容策划引用了不存在的配图 {image_id}")
        slots.append(
            {
                "image_id": image_id,
                "label": normalize_text(item.get("label")) or image_id,
                "placement": normalize_text(item.get("placement")) or ("hero" if page_role == "cover" else "support"),
            }
        )
    if slots and not catalog_ids:
        raise RuntimeError("没有可用配图，内容策划不能指定 image_slots")
    return {
        "page_code": normalize_text(payload.get("page_code")) or page_code,
        "title": plan_title,
        "subtitle": subtitle,
        "badge": normalize_text(payload.get("badge")),
        "blocks": blocks,
        "footer": footer,
        "image_slots": slots,
        **({"visual_intent": normalize_text(payload.get("visual_intent"))} if page_role == "section" and normalize_text(payload.get("visual_intent")) else {}),
    }


def collect_plan_skeleton(plan: dict[str, Any] | None) -> list[str]:
    if not isinstance(plan, dict):
        return []
    found: list[str] = []
    title = normalize_text(plan.get("title"))
    if title:
        found.append(title)
    for key in ("subtitle", "badge"):
        value = normalize_text(plan.get(key))
        if value:
            found.append(value)
    for block in plan.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        label = normalize_text(block.get("label"))
        if label:
            found.append(label)
    return found


def collect_svg_texts(svg_markup: str) -> list[str]:
    root = _parse_svg(_ensure_svg_xmlns(svg_markup))
    texts: list[str] = []

    def walk(elem: ET.Element, chrome: str) -> None:
        current = chrome or (elem.get("data-chrome") or "").strip()
        if _local_tag(elem) == "text" and current not in _SKIP_CHROME:
            full = normalize_text("".join(elem.itertext()))
            if full:
                texts.append(full)
        for child in list(elem):
            walk(child, current)

    walk(root, "")
    return texts


def assert_svg_matches_plan(svg_markup: str, plan: dict[str, Any] | None) -> None:
    if not has_content_plan(plan):
        return
    skeleton = collect_plan_skeleton(plan)
    blob = normalize_text("".join(collect_svg_texts(svg_markup)))
    missing = [item for item in skeleton if item not in blob]
    if missing:
        raise RuntimeError("SVG 文案与内容策划稿不一致。缺失: " + "、".join(missing[:8]))


def _assert_block_density(label: str, note: str) -> None:
    compact_label = re.sub(r"\s+", "", label)
    if len(compact_label) < 2:
        raise RuntimeError("内容策划 label 过短")
    if re.fullmatch(r"[0-9]+", compact_label):
        raise RuntimeError("内容策划 label 不能是纯数字")
    if len(compact_label) > MAX_LABEL_CHARS:
        raise RuntimeError(f"内容策划 label 超过 {MAX_LABEL_CHARS} 字")
    if compact_char_count(note) > MAX_NOTE_CHARS:
        raise RuntimeError(f"内容策划 note 超过 {MAX_NOTE_CHARS} 字")
