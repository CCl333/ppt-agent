from __future__ import annotations

import json
from pathlib import Path
from typing import Any

JUDGE_PROMPT_VERSION = "quality-eval-judge.v1"
RUBRIC_DIMENSIONS = (
    "hierarchy",
    "spatial",
    "readability",
    "visual_content",
    "completeness",
)
CONFIDENCES = frozenset({"low", "medium", "high"})
PAIRWISE_WINNERS = frozenset({"A", "B", "tie"})

RUBRIC_SYSTEM = (
    "你是演示文稿视觉评审。必须先引用渲染图中的证据，再给分。"
    "只评价页面层级、空间平衡、文字可读性、视觉与内容匹配、完成度。"
    "不要因为颜色偏好给分。不要把几何硬规则（出框、重叠、required slot）判成硬失败；"
    "可疑问题只能写入 candidate_issue。"
    "只输出 JSON。"
)

PAIRWISE_SYSTEM = (
    "请比较版本 A 和版本 B。只评价：页面层级、空间平衡、文字可读性、视觉与内容匹配、完成度。"
    "不要因为颜色偏好或风格不同而自动偏好某一版。"
    "输出 JSON：winner 只能是 A、B 或 tie。"
)


def empty_dimension(score: int = 0, *, confidence: str = "low", revision: str = "") -> dict[str, Any]:
    return {
        "score": score,
        "confidence": confidence,
        "evidence": [],
        "hard_issue": False,
        "revision": revision,
        "candidate_issue": None,
    }


def normalize_rubric(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    dimensions: dict[str, Any] = {}
    candidate_issues: list[dict[str, Any]] = []
    raw_dims = source.get("dimensions") if isinstance(source.get("dimensions"), dict) else source
    for name in RUBRIC_DIMENSIONS:
        item = raw_dims.get(name) if isinstance(raw_dims, dict) else None
        item = item if isinstance(item, dict) else {}
        score = _clamp_score(item.get("score"))
        confidence = str(item.get("confidence") or "low").lower()
        if confidence not in CONFIDENCES:
            confidence = "low"
        evidence = item.get("evidence") if isinstance(item.get("evidence"), list) else []
        candidate = item.get("candidate_issue") or (item.get("revision") if item.get("hard_issue") else None)
        normalized = {
            "score": score,
            "confidence": confidence,
            "evidence": [entry for entry in evidence if isinstance(entry, dict)],
            "hard_issue": False,
            "revision": str(item.get("revision") or ""),
            "candidate_issue": str(candidate) if candidate else None,
        }
        if item.get("hard_issue") or candidate:
            candidate_issues.append({"dimension": name, "detail": normalized["candidate_issue"] or normalized["revision"]})
        dimensions[name] = normalized
    scores = [dimensions[name]["score"] for name in RUBRIC_DIMENSIONS]
    mean = round(sum(scores) / len(scores), 3) if scores else None
    return {
        "status": "ok",
        "prompt_version": JUDGE_PROMPT_VERSION,
        "dimensions": dimensions,
        "mean_score": mean,
        "candidate_issues": candidate_issues,
    }


def normalize_pairwise(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    winner = str(source.get("winner") or "tie").upper()
    if winner not in PAIRWISE_WINNERS:
        winner = "tie"
    return {
        "status": "ok",
        "prompt_version": JUDGE_PROMPT_VERSION,
        "winner": winner,
        "reason": str(source.get("reason") or source.get("revision") or ""),
        "top_issue": str(source.get("top_issue") or ""),
        "dimensions": source.get("dimensions") if isinstance(source.get("dimensions"), dict) else {},
    }


def judge_rubric(
    *,
    context: dict[str, Any],
    image_paths: list[str],
) -> dict[str, Any]:
    readable = [path for path in image_paths if path and Path(path).is_file()]
    if not readable:
        return {
            "status": "skipped",
            "reason": "render_unavailable",
            "prompt_version": JUDGE_PROMPT_VERSION,
            "dimensions": {name: empty_dimension() for name in RUBRIC_DIMENSIONS},
            "mean_score": None,
            "candidate_issues": [],
        }
    try:
        raw = _call_judge(RUBRIC_SYSTEM, _rubric_user_prompt(context), readable)
    except RuntimeError as exc:
        reason = str(exc)
        if "vision_input_unsupported" in reason or "VLM 评审不可用" in reason:
            return {
                "status": "skipped",
                "reason": "vision_input_unsupported" if "vision_input_unsupported" in reason else "vlm_unavailable",
                "prompt_version": JUDGE_PROMPT_VERSION,
                "dimensions": {name: empty_dimension() for name in RUBRIC_DIMENSIONS},
                "mean_score": None,
                "candidate_issues": [],
            }
        raise
    return normalize_rubric(raw)


def judge_pairwise(
    *,
    context: dict[str, Any],
    image_a: str | None,
    image_b: str | None,
) -> dict[str, Any]:
    skipped = {
        "status": "skipped",
        "prompt_version": JUDGE_PROMPT_VERSION,
        "winner": "tie",
        "reason": "缺少可比较的渲染图",
        "top_issue": "",
        "dimensions": {},
    }
    if not image_a or not image_b or not Path(image_a).is_file() or not Path(image_b).is_file():
        skipped["reason"] = "render_unavailable"
        return skipped
    try:
        raw = _call_judge(PAIRWISE_SYSTEM, _pairwise_user_prompt(context), [image_a, image_b])
    except RuntimeError as exc:
        skipped["reason"] = "vision_input_unsupported" if "vision_input_unsupported" in str(exc) else "vlm_unavailable"
        return skipped
    return normalize_pairwise(raw)


def _call_judge(system_prompt: str, user_prompt: str, image_paths: list[str]) -> dict[str, Any]:
    # 第一版没有多模态协议：禁止把本地路径写进文本 prompt 假装看过渲染图。
    if image_paths:
        raise RuntimeError("vision_input_unsupported")
    raise RuntimeError("render_unavailable")


def _parse_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw[3:]
        if raw.lower().startswith("json"):
            raw = raw[4:]
        end = raw.rfind("```")
        if end >= 0:
            raw = raw[:end]
        raw = raw.strip()
    start = raw.find("{")
    if start < 0:
        raise RuntimeError("VLM 未返回 JSON")
    payload, _offset = json.JSONDecoder().raw_decode(raw[start:])
    if not isinstance(payload, dict):
        raise RuntimeError("VLM JSON 不是对象")
    return payload


def _clamp_score(value: Any) -> int:
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(5, score))


def _rubric_user_prompt(context: dict[str, Any]) -> str:
    return (
        "评审这一页设计稿。上下文 JSON：\n"
        + json.dumps(context, ensure_ascii=False)
        + "\n输出：{\"dimensions\":{"
        + ",".join(f'"{name}":{{"score":0,"confidence":"low","evidence":[],"hard_issue":false,"revision":""}}' for name in RUBRIC_DIMENSIONS)
        + "}}"
    )


def _pairwise_user_prompt(context: dict[str, Any]) -> str:
    return (
        "比较 A/B。上下文 JSON：\n"
        + json.dumps(context, ensure_ascii=False)
        + '\n输出：{"winner":"A","reason":"","top_issue":""}'
    )
