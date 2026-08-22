from __future__ import annotations

from typing import Any

FIXED_PREFIX_ROLES = ("cover", "toc")
FIXED_SUFFIX_ROLES = ("end",)
FIXED_PAGE_COUNT = 3
SECTION_ROLE = "section"
CONTENT_ROLE = "content"


def decide_section_strategy(page_count_target: int, part_count: int) -> str:
    """Product-default section page strategy.

    compact: 不插入章节页，避免正文被挤没。
    standard / strong: 每个 part 一页章节页。
    Users can still force a chapter on or off via storyboard section_page.
    """
    total = int(page_count_target or 0)
    parts = int(part_count or 0)
    if parts <= 0:
        return "compact"
    remaining_if_enabled = total - FIXED_PAGE_COUNT - parts
    if remaining_if_enabled < parts:
        return "compact"
    if total >= 16 and parts >= 2:
        return "strong"
    if total >= 10 and parts >= 2:
        return "standard"
    return "compact"


def section_pages_enabled(page_count_target: int, part_count: int) -> bool:
    return decide_section_strategy(page_count_target, part_count) != "compact"


def content_page_budget(page_count_target: int, part_count: int, *, enabled: bool | None = None) -> int:
    use_section = section_pages_enabled(page_count_target, part_count) if enabled is None else enabled
    section_count = part_count if use_section else 0
    return max(int(page_count_target or 0) - FIXED_PAGE_COUNT - section_count, 0)


def normalize_part_id(raw: Any, index: int) -> str:
    text = str(raw or "").strip()
    return text or f"part-{index}"


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def want_section_page(section_payload: Any, *, has_existing: bool) -> bool:
    """Whether this part should keep/create a real section ProjectPage.

    Frontend sends `null` or `{enabled: false}` when the chapter has no section page.
    `enabled: true` creates or updates. A dict without `enabled` keeps an existing
    page but does not auto-insert one on legacy decks.
    """
    if section_payload is None:
        return False
    if not isinstance(section_payload, dict):
        return has_existing
    if section_payload.get("enabled") is False:
        return False
    if section_payload.get("enabled") is True:
        return True
    return has_existing


def normalize_section_page(payload: Any, *, part_title: str, content_titles: list[str], enabled: bool) -> dict[str, Any]:
    raw = payload if isinstance(payload, dict) else {}
    title = str(raw.get("title") or "").strip() or part_title
    subtitle = str(raw.get("subtitle") or "").strip()
    preview_items = _string_list(raw.get("preview_items")) or list(content_titles)
    visual_intent = str(raw.get("visual_intent") or "").strip()
    return {
        "enabled": bool(enabled),
        "title": title,
        "subtitle": subtitle,
        "preview_items": preview_items,
        "visual_intent": visual_intent,
    }


def build_section_summary(
    *,
    part_title: str,
    section_page: dict[str, Any] | None,
    content_titles: list[str],
) -> str:
    meta = section_page if isinstance(section_page, dict) else {}
    title = str(meta.get("title") or part_title or "").strip() or "章节"
    subtitle = str(meta.get("subtitle") or "").strip()
    preview_items = _string_list(meta.get("preview_items"))
    visual_intent = str(meta.get("visual_intent") or "").strip()
    lines = [title]
    if part_title and part_title != title:
        lines.append(part_title)
    if subtitle:
        lines.append(subtitle)
    if preview_items:
        lines.append("本章预告：" + "；".join(preview_items))
    if content_titles:
        lines.append("本章页面：" + "；".join(content_titles))
    if visual_intent:
        lines.append("视觉意图：" + visual_intent)
    return "\n".join(lines).strip()


def enrich_outline_section_pages(payload: dict[str, Any], *, page_count_target: int) -> dict[str, Any]:
    ppt_outline = payload.get("ppt_outline") if isinstance(payload.get("ppt_outline"), dict) else payload
    if not isinstance(ppt_outline, dict):
        return payload
    parts = ppt_outline.get("parts")
    if not isinstance(parts, list):
        parts = []
        ppt_outline["parts"] = parts
    enabled = section_pages_enabled(page_count_target, len(parts))
    rebuilt: list[dict[str, Any]] = []
    for index, part in enumerate(parts, start=1):
        if not isinstance(part, dict):
            continue
        part_title = str(part.get("part_title") or "").strip() or "未命名章节"
        pages_raw = part.get("pages") if isinstance(part.get("pages"), list) else []
        pages: list[dict[str, Any]] = []
        content_titles: list[str] = []
        for item in pages_raw:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip() or "新内容页"
            content = _string_list(item.get("content"))
            pages.append({"title": title, "content": content})
            content_titles.append(title)
        rebuilt.append(
            {
                "part_id": normalize_part_id(part.get("part_id"), index),
                "part_title": part_title,
                "section_page": normalize_section_page(
                    part.get("section_page"),
                    part_title=part_title,
                    content_titles=content_titles,
                    enabled=enabled,
                ),
                "pages": pages,
            }
        )
    ppt_outline["parts"] = rebuilt
    if "ppt_outline" in payload:
        payload["ppt_outline"] = ppt_outline
        return payload
    return {"ppt_outline": ppt_outline}


def iter_outline_page_defs(ppt_outline: dict[str, Any]) -> list[dict[str, Any]]:
    defs: list[dict[str, Any]] = []
    cover = ppt_outline.get("cover") if isinstance(ppt_outline.get("cover"), dict) else {}
    toc = ppt_outline.get("table_of_contents") if isinstance(ppt_outline.get("table_of_contents"), dict) else {}
    end_page = ppt_outline.get("end_page") if isinstance(ppt_outline.get("end_page"), dict) else {}
    defs.append(
        {
            "page_role": "cover",
            "part_id": None,
            "part_title": None,
            "title": str(cover.get("title") or "封面").strip() or "封面",
            "content": _string_list(cover.get("content")),
            "section_page": None,
        }
    )
    defs.append(
        {
            "page_role": "toc",
            "part_id": None,
            "part_title": None,
            "title": str(toc.get("title") or "目录").strip() or "目录",
            "content": _string_list(toc.get("content")),
            "section_page": None,
        }
    )
    for part in ppt_outline.get("parts") or []:
        if not isinstance(part, dict):
            continue
        part_id = str(part.get("part_id") or "").strip()
        part_title = str(part.get("part_title") or "").strip() or "未命名章节"
        pages = part.get("pages") if isinstance(part.get("pages"), list) else []
        content_titles = [str(item.get("title") or "").strip() for item in pages if isinstance(item, dict) and str(item.get("title") or "").strip()]
        section_page = part.get("section_page") if isinstance(part.get("section_page"), dict) else {}
        if section_page.get("enabled"):
            defs.append(
                {
                    "page_role": SECTION_ROLE,
                    "part_id": part_id,
                    "part_title": part_title,
                    "title": str(section_page.get("title") or part_title).strip() or part_title,
                    "content": _string_list(section_page.get("preview_items")) or content_titles,
                    "section_page": section_page,
                }
            )
        for item in pages:
            if not isinstance(item, dict):
                continue
            defs.append(
                {
                    "page_role": CONTENT_ROLE,
                    "part_id": part_id,
                    "part_title": part_title,
                    "title": str(item.get("title") or "").strip() or "新内容页",
                    "content": _string_list(item.get("content")),
                    "section_page": None,
                }
            )
    defs.append(
        {
            "page_role": "end",
            "part_id": None,
            "part_title": None,
            "title": str(end_page.get("title") or "谢谢").strip() or "谢谢",
            "content": _string_list(end_page.get("content")),
            "section_page": None,
        }
    )
    return defs


def _page_ref(page: dict[str, Any]) -> dict[str, Any]:
    return {
        "page_id": page.get("page_id"),
        "page_role": page.get("page_role"),
        "title": page.get("title") or "",
        "part_id": page.get("part_id"),
        "part_title": page.get("part_title"),
    }


def build_storyboard_tree(pages: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(pages, key=lambda item: int(item.get("sort_order") or 0))
    prefix = [item for item in ordered if item.get("page_role") in FIXED_PREFIX_ROLES]
    suffix = [item for item in ordered if item.get("page_role") in FIXED_SUFFIX_ROLES]
    body = [item for item in ordered if item.get("page_role") not in {*FIXED_PREFIX_ROLES, *FIXED_SUFFIX_ROLES}]
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for page in body:
        part_id = str(page.get("part_id") or "").strip() or f"legacy-{(page.get('part_title') or 'untitled').strip() or 'untitled'}"
        if current is None or current["part_id"] != part_id:
            current = {
                "part_id": part_id,
                "part_title": page.get("part_title") or "",
                "section_page": None,
                "content_pages": [],
            }
            sections.append(current)
        if page.get("part_title"):
            current["part_title"] = page.get("part_title")
        if page.get("page_role") == SECTION_ROLE:
            current["section_page"] = _page_ref(page)
        else:
            current["content_pages"].append(_page_ref(page))
    play_order: list[str] = []
    for item in prefix:
        if item.get("page_id"):
            play_order.append(item["page_id"])
    for section in sections:
        section_page = section.get("section_page") or {}
        if section_page.get("page_id"):
            play_order.append(section_page["page_id"])
        for item in section.get("content_pages") or []:
            if item.get("page_id"):
                play_order.append(item["page_id"])
    for item in suffix:
        if item.get("page_id"):
            play_order.append(item["page_id"])
    return {
        "fixed_prefix": [_page_ref(item) for item in prefix],
        "sections": sections,
        "fixed_suffix": [_page_ref(item) for item in suffix],
        "play_order": play_order,
        "composition": page_composition(ordered),
    }


def page_composition(pages: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"cover": 0, "toc": 0, "section": 0, "content": 0, "end": 0}
    for page in pages:
        role = str(page.get("page_role") or "")
        if role in counts:
            counts[role] += 1
        elif role not in {*FIXED_PREFIX_ROLES, *FIXED_SUFFIX_ROLES, SECTION_ROLE}:
            counts["content"] += 1
    total = sum(counts.values())
    return {
        **counts,
        "total": total,
        "label": (
            f"封面 {counts['cover']} + 目录 {counts['toc']} + 章节过渡 {counts['section']}"
            f" + 正文 {counts['content']} + 收尾 {counts['end']} = {total}"
        ),
    }


def expand_play_order(tree: dict[str, Any]) -> list[str]:
    return [str(item) for item in (tree.get("play_order") or []) if str(item)]
