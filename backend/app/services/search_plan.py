from __future__ import annotations

import re
from typing import Any

_SLUG_RE = re.compile(r"[^a-z0-9\u4e00-\u9fff-]+")
MAX_DIMENSIONS = 8


def slugify_dimension_id(raw: str, fallback: str) -> str:
    value = _SLUG_RE.sub("-", str(raw or "").strip().lower()).strip("-")
    return value or fallback


def normalize_query_plan(payload: Any) -> list[dict[str, str]]:
    raw_dimensions = payload.get("dimensions") if isinstance(payload, dict) else None
    if isinstance(raw_dimensions, list) and raw_dimensions:
        return _flatten_dimensions(raw_dimensions)
    raw_queries = payload
    if isinstance(payload, dict):
        raw_queries = payload.get("queries") or payload.get("page_search_queries") or payload.get("items")
    if not isinstance(raw_queries, list):
        return []
    items: list[dict[str, str]] = []
    for index, raw in enumerate(raw_queries, start=1):
        item = _normalize_query_item(raw, fallback_id=f"dim-{index}")
        if item:
            items.append(item)
    return items


def dimensions_from_plan(queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for query in queries:
        dimension_id = str(query.get("dimension_id") or "").strip() or slugify_dimension_id(
            str(query.get("dimension") or query.get("query_purpose") or ""),
            "general",
        )
        name = str(query.get("dimension") or query.get("query_purpose") or "综合").strip() or "综合"
        if dimension_id not in seen:
            seen[dimension_id] = {"dimension_id": dimension_id, "name": name, "query_count": 0}
            order.append(dimension_id)
        seen[dimension_id]["query_count"] += 1
        if name and seen[dimension_id]["name"] == "综合":
            seen[dimension_id]["name"] = name
    return [seen[key] for key in order]


def next_search_round(existing: list[dict[str, Any]] | None) -> int:
    rounds = []
    for item in existing or []:
        if not isinstance(item, dict):
            continue
        try:
            rounds.append(int(item.get("round") or 1))
        except (TypeError, ValueError):
            rounds.append(1)
    return (max(rounds) if rounds else 0) + 1


def stamp_search_results(
    items: list[dict[str, Any]],
    *,
    search_round: int,
    default_dimension: str = "",
    default_dimension_id: str = "",
) -> list[dict[str, Any]]:
    stamped: list[dict[str, Any]] = []
    for item in items:
        payload = dict(item)
        dimension = str(payload.get("dimension") or default_dimension or payload.get("query_purpose") or "").strip()
        dimension_id = str(payload.get("dimension_id") or default_dimension_id or "").strip()
        if not dimension_id:
            dimension_id = slugify_dimension_id(dimension, "general")
        payload["round"] = int(search_round)
        payload["dimension"] = dimension
        payload["dimension_id"] = dimension_id
        stamped.append(payload)
    return stamped


def merge_search_results(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in list(existing or []) + list(incoming or []):
        if not isinstance(item, dict):
            continue
        key = str(item.get("url") or item.get("id") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def search_coverage(queries: list[dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, Any]:
    dimensions = dimensions_from_plan(queries)
    hits_by_dim: dict[str, int] = {}
    rounds: list[int] = []
    for item in results or []:
        if not isinstance(item, dict):
            continue
        dim_id = str(item.get("dimension_id") or "").strip()
        if dim_id:
            hits_by_dim[dim_id] = hits_by_dim.get(dim_id, 0) + 1
        try:
            rounds.append(int(item.get("round") or 1))
        except (TypeError, ValueError):
            rounds.append(1)
    for item in dimensions:
        item["hit_count"] = hits_by_dim.get(item["dimension_id"], 0)
    latest_round = max(rounds) if rounds else 0
    latest_hits = sum(1 for item in results or [] if int(item.get("round") or 1) == latest_round) if latest_round else 0
    return {
        "dimension_count": len(dimensions),
        "query_count": len(queries or []),
        "result_count": len(results or []),
        "rounds": sorted(set(rounds)),
        "latest_round": latest_round,
        "latest_round_hits": latest_hits,
        "dimensions": dimensions,
    }


def copy_plan_fields(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "round": int(item.get("round") or 1),
        "dimension": str(item.get("dimension") or item.get("query_purpose") or ""),
        "dimension_id": str(item.get("dimension_id") or ""),
    }


def _flatten_dimensions(raw_dimensions: list[Any]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for index, raw in enumerate(raw_dimensions[:MAX_DIMENSIONS], start=1):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or raw.get("dimension") or raw.get("label") or "").strip()
        dimension_id = slugify_dimension_id(str(raw.get("id") or raw.get("dimension_id") or name), f"dim-{index}")
        queries = raw.get("queries") if isinstance(raw.get("queries"), list) else [raw]
        for query in queries:
            item = _normalize_query_item(query, fallback_id=dimension_id, dimension=name, dimension_id=dimension_id)
            if item:
                items.append(item)
    return items


def _normalize_query_item(
    raw: Any,
    *,
    fallback_id: str,
    dimension: str = "",
    dimension_id: str = "",
) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    query_text = str(raw.get("query_text") or raw.get("query") or "").strip()
    if not query_text:
        return None
    query_purpose = str(raw.get("query_purpose") or raw.get("query_intent") or raw.get("intent") or "").strip()
    dim_name = str(raw.get("dimension") or dimension or query_purpose or "综合").strip() or "综合"
    dim_id = str(raw.get("dimension_id") or dimension_id or "").strip() or slugify_dimension_id(dim_name, fallback_id)
    return {
        "query_text": query_text,
        "query_purpose": query_purpose or dim_name,
        "dimension": dim_name,
        "dimension_id": dim_id,
    }
