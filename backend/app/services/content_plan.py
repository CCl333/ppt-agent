from __future__ import annotations

import re
from typing import Any
from xml.etree import ElementTree as ET

from app.services.svg import _ensure_svg_xmlns, _local_tag, _parse_svg

_SKIP_CHROME = {"background", "texture", "page_number"}
_SKIP_PLAN_KEYS = {"page_code", "role", "image_id", "placement", "image_slots"}


def normalize_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


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
        blocks.append(
            {
                "role": normalize_text(item.get("role")) or "point",
                "label": label,
                "note": normalize_text(item.get("note")),
            }
        )
    footer_raw = payload.get("footer") if isinstance(payload.get("footer"), dict) else {}
    presenter = normalize_text(footer_raw.get("presenter"))
    date = normalize_text(footer_raw.get("date"))
    footer = {key: value for key, value in (("presenter", presenter), ("date", date)) if value}
    if page_role == "content" and not blocks:
        raise RuntimeError("内容页策划稿缺少 blocks")
    if page_role in {"cover", "end", "toc"} and not blocks and not normalize_text(payload.get("subtitle")):
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
                "placement": normalize_text(item.get("placement")) or "hero",
            }
        )
    if catalog_ids and page_role in {"content", "cover"} and not slots:
        raise RuntimeError("有可用配图但内容策划未指定 image_slots")
    if slots and not catalog_ids:
        raise RuntimeError("没有可用配图，内容策划不能指定 image_slots")
    return {
        "page_code": normalize_text(payload.get("page_code")) or page_code,
        "title": plan_title,
        "subtitle": normalize_text(payload.get("subtitle")),
        "badge": normalize_text(payload.get("badge")),
        "blocks": blocks,
        "footer": footer,
        "image_slots": slots,
    }


def collect_plan_strings(plan: dict[str, Any] | None) -> list[str]:
    found: list[str] = []

    def walk(node: Any, key: str | None = None) -> None:
        if isinstance(node, dict):
            for child_key, value in node.items():
                walk(value, child_key)
            return
        if isinstance(node, list):
            for item in node:
                walk(item, key)
            return
        if key in _SKIP_PLAN_KEYS:
            return
        text = normalize_text(node if isinstance(node, str) else None)
        if text:
            found.append(text)

    walk(plan or {})
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
    plan_texts = collect_plan_strings(plan)
    svg_texts = collect_svg_texts(svg_markup)
    missing = [
        item
        for item in plan_texts
        if item not in svg_texts and not any(item in node or node in item for node in svg_texts)
    ]
    extra = [
        item
        for item in svg_texts
        if item not in plan_texts and not any(item in planned for planned in plan_texts)
    ]
    if not missing and not extra:
        return
    details: list[str] = []
    if missing:
        details.append("缺失: " + "、".join(missing[:8]))
    if extra:
        details.append("多出: " + "、".join(extra[:8]))
    raise RuntimeError("SVG 文案与内容策划稿不一致。" + "；".join(details))
