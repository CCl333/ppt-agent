from __future__ import annotations

from app.services.search_plan import (
    merge_search_results,
    next_search_round,
    normalize_query_plan,
    search_coverage,
    stamp_search_results,
)


def test_normalize_query_plan_flattens_dimensions():
    queries = normalize_query_plan(
        {
            "dimensions": [
                {
                    "id": "tickets",
                    "name": "门票预约规则",
                    "queries": [{"query_text": "故宫 2026 预约", "query_purpose": "官方规则"}],
                },
                {
                    "id": "transit",
                    "name": "交通接驳",
                    "queries": [{"query_text": "故宫 地铁", "query_purpose": "出行"}],
                },
            ]
        }
    )
    assert len(queries) == 2
    assert queries[0]["dimension"] == "门票预约规则"
    assert queries[0]["dimension_id"] == "tickets"
    assert queries[1]["query_text"] == "故宫 地铁"


def test_normalize_flat_queries_derives_dimension_from_purpose():
    queries = normalize_query_plan(
        {"queries": [{"query_text": "q1", "query_purpose": "定义类"}, {"query_text": "q2", "query_purpose": "证据类"}]}
    )
    assert queries[0]["dimension"] == "定义类"
    assert queries[1]["dimension_id"]


def test_next_round_and_merge_keep_previous_hits():
    existing = [
        {"url": "https://a.example", "round": 1, "id": "a"},
        {"url": "https://b.example", "round": 1, "id": "b"},
    ]
    assert next_search_round(existing) == 2
    incoming = stamp_search_results(
        [{"url": "https://b.example", "id": "b2"}, {"url": "https://c.example", "id": "c"}],
        search_round=2,
        default_dimension="补充",
    )
    merged = merge_search_results(existing, incoming)
    assert [item["url"] for item in merged] == ["https://a.example", "https://b.example", "https://c.example"]
    assert merged[1]["round"] == 1
    assert merged[2]["round"] == 2


def test_search_coverage_counts_dimensions_and_latest_round():
    queries = normalize_query_plan(
        {
            "dimensions": [
                {"id": "a", "name": "A", "queries": [{"query_text": "q1"}]},
                {"id": "b", "name": "B", "queries": [{"query_text": "q2"}, {"query_text": "q3"}]},
            ]
        }
    )
    results = [
        {"url": "https://a.example", "round": 1, "dimension_id": "a"},
        {"url": "https://b.example", "round": 2, "dimension_id": "b"},
        {"url": "https://c.example", "round": 2, "dimension_id": "b"},
    ]
    coverage = search_coverage(queries, results)
    assert coverage["dimension_count"] == 2
    assert coverage["query_count"] == 3
    assert coverage["latest_round"] == 2
    assert coverage["latest_round_hits"] == 2
    hits = {item["dimension_id"]: item["hit_count"] for item in coverage["dimensions"]}
    assert hits["a"] == 1
    assert hits["b"] == 2
