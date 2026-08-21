from __future__ import annotations

import re
from typing import Any

from app.services.export_name import looks_like_title

CLARIFICATION_CODES = ("audience", "occasion", "duration", "working_title")

CLARIFICATION_INTRO = (
    "为了让我更好地帮助您完成演示文稿，方便告诉我一些关键信息吗？"
    "请先确认受众、场合、时长和演示标题。"
)

_PAGE_COUNT_HINT = re.compile(r"约\s*\d+\s*页")
_REQUEST_LEAD = re.compile(
    r"^(请|麻烦)?(帮我)?(生成|做|写|制作|出)?(一份|一个|一套|一篇)?",
)
_DECK_WORDS = re.compile(r"(的)?(PPT|ppt|pptx|演示文稿|幻灯片)")
_CLARIFICATION_LABEL_HINTS = ("受众", "听众", "场合", "时长", "讲多久", "封面标题", "演示标题")


def topic_from_request(request_text: str) -> str:
    text = re.sub(r"\s+", " ", request_text or "").strip()
    text = _REQUEST_LEAD.sub("", text).strip()
    text = _PAGE_COUNT_HINT.sub("", text)
    text = _DECK_WORDS.sub("", text)
    return text.strip(" ，,。、的")


def placeholder_project_title(request_text: str) -> str:
    cleaned = re.sub(r"\s+", " ", request_text or "").strip()
    if not cleaned:
        return "未命名项目"
    topic = topic_from_request(cleaned)
    if looks_like_title(topic):
        return topic[:40]
    if looks_like_title(cleaned):
        return cleaned[:40]
    return "未命名项目"


def project_title_from_answers(answers: dict[str, Any] | None) -> str | None:
    value = str((answers or {}).get("working_title") or "").strip()
    if looks_like_title(value):
        return value[:80]
    return None


def apply_working_title_to_outline(outline: dict[str, Any], answers: dict[str, Any] | None) -> dict[str, Any]:
    title = project_title_from_answers(answers)
    if not title:
        return outline
    if not isinstance(outline, dict):
        raise RuntimeError("大纲不是 JSON 对象，无法写入封面标题")
    ppt_outline = outline.get("ppt_outline") if isinstance(outline.get("ppt_outline"), dict) else outline
    if not isinstance(ppt_outline, dict):
        raise RuntimeError("大纲缺少 ppt_outline，无法写入封面标题")
    cover = ppt_outline.get("cover")
    if not isinstance(cover, dict):
        cover = {}
        ppt_outline["cover"] = cover
    cover["title"] = title
    if "ppt_outline" in outline:
        outline["ppt_outline"] = ppt_outline
    return outline


def merge_clarification_questions(
    extra: list[dict[str, Any]] | None,
    *,
    working_title_options: list[str] | None = None,
    request_text: str = "",
    existing: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    existing_by_code = {
        str(item.get("question_code") or ""): item
        for item in existing or []
        if isinstance(item, dict) and str(item.get("question_code") or "").strip()
    }
    title_options = _usable_title_options(
        list(working_title_options or []),
        request_text,
        existing_by_code.get("working_title"),
    )
    structured = [
        _question(
            "audience",
            "这份演示主要讲给谁听？",
            "受众决定语气、证据粒度和结论写法。",
            ["管理层", "业务同事", "客户或外部听众"],
        ),
        _question(
            "occasion",
            "将在什么场合使用？",
            "场合决定正式程度和信息密度。",
            ["内部汇报", "对外路演", "培训分享"],
        ),
        _question(
            "duration",
            "大概讲多长时间？",
            "时长约束页数分配和每页信息量。",
            ["约 5 分钟", "约 15 分钟", "约 30 分钟"],
        ),
        _question(
            "working_title",
            "封面和导出文件希望用哪个标题？",
            "不要用需求原文；这里会成为项目名、封面标题和导出文件名。",
            title_options,
        ),
    ]
    extras: list[dict[str, Any]] = []
    seen_codes = set(CLARIFICATION_CODES)
    for item in extra or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("question_code") or "").strip()
        if not code or code in seen_codes or _looks_like_clarification_duplicate(item):
            continue
        seen_codes.add(code)
        extras.append(item)
        if len(extras) >= 2:
            break
    return structured + extras


def _usable_title_options(
    raw: list[str],
    request_text: str,
    existing: dict[str, Any] | None,
) -> list[str]:
    seen: set[str] = set()
    options: list[str] = []
    for item in raw:
        value = re.sub(r"\s+", " ", str(item or "")).strip()
        if not looks_like_title(value) or value in seen:
            continue
        seen.add(value)
        options.append(value[:40])
        if len(options) == 3:
            return options
    if existing:
        for option in existing.get("options") or []:
            if not isinstance(option, dict):
                continue
            value = str(option.get("label") or "").strip()
            if not looks_like_title(value) or value in seen:
                continue
            seen.add(value)
            options.append(value[:40])
            if len(options) == 3:
                return options
    for fallback in _fallback_title_options(request_text):
        if fallback in seen:
            continue
        seen.add(fallback)
        options.append(fallback)
        if len(options) == 3:
            break
    while len(options) < 3:
        options.append(f"方案 {len(options) + 1}")
    return options[:3]


def _fallback_title_options(request_text: str) -> list[str]:
    topic = topic_from_request(request_text)
    if not topic or not looks_like_title(topic):
        return ["主题演示", "专题汇报", "方案介绍"]
    compact = topic[:18]
    options = [compact]
    if not compact.endswith("方案"):
        options.append(f"{compact}方案")
    if not compact.endswith("全攻略"):
        options.append(f"{compact}全攻略")
    if len(options) < 3:
        options.append(f"{compact}要点")
    return options[:3]


def _question(code: str, label: str, description: str, option_labels: list[str]) -> dict[str, Any]:
    return {
        "question_code": code,
        "label": label,
        "description": description,
        "options": [
            {"option_code": option_code, "label": option_label}
            for option_code, option_label in zip(("A", "B", "C"), option_labels[:3], strict=False)
        ],
        "allow_custom": True,
    }


def _looks_like_clarification_duplicate(item: dict[str, Any]) -> bool:
    blob = f"{item.get('question_code') or ''} {item.get('label') or ''} {item.get('description') or ''}"
    return any(hint in blob for hint in _CLARIFICATION_LABEL_HINTS)
