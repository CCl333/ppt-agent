from __future__ import annotations

import re
from typing import Any

_ILLEGAL_FILENAME = re.compile(r'[\\/:*?"<>|]')
_REQUEST_PREFIXES = ("请", "帮我", "生成")
_PAGE_COUNT_HINT = re.compile(r"约\s*\d+\s*页")


def looks_like_title(text: str | None) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if any(value.startswith(prefix) for prefix in _REQUEST_PREFIXES):
        return False
    if _PAGE_COUNT_HINT.search(value):
        return False
    return True


def safe_filename(text: str, *, max_len: int = 80) -> str:
    cleaned = _ILLEGAL_FILENAME.sub("_", str(text or "").strip())
    cleaned = cleaned.replace("..", "_")
    cleaned = re.sub(r"[\x00-\x1f]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    cleaned = cleaned[:max_len].rstrip(" ._")
    return cleaned or "未命名演示文稿"


def cover_title_from_outline(outline_json: dict[str, Any] | None) -> str | None:
    payload = outline_json or {}
    ppt_outline = payload.get("ppt_outline") if isinstance(payload.get("ppt_outline"), dict) else payload
    cover = ppt_outline.get("cover") if isinstance(ppt_outline, dict) else None
    if not isinstance(cover, dict):
        return None
    title = str(cover.get("title") or "").strip()
    return title or None


def resolve_export_stem(
    *,
    outline_json: dict[str, Any] | None,
    first_page_title: str | None,
    project_title: str | None,
) -> str:
    for candidate in (
        cover_title_from_outline(outline_json),
        str(first_page_title or "").strip() or None,
        project_title if looks_like_title(project_title) else None,
    ):
        if candidate and candidate.strip():
            return safe_filename(candidate.strip())
    raise RuntimeError("无法确定导出文件名：大纲缺少封面标题")
