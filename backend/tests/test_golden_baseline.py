from __future__ import annotations

import json
from pathlib import Path

from app.services.page_quality import evaluate_page_quality
from app.services.svg_contract import validate_svg_contract

GOLDEN_ROOT = Path(__file__).parent / "fixtures" / "golden"


def load_catalog() -> dict:
    return json.loads((GOLDEN_ROOT / "catalog.json").read_text(encoding="utf-8"))


def load_case_svg(case: dict) -> str:
    return (GOLDEN_ROOT / case["svg_file"]).read_text(encoding="utf-8")


def load_case_plan(case: dict) -> dict | None:
    plan_file = case.get("plan_file")
    if not plan_file:
        return None
    return json.loads((GOLDEN_ROOT / plan_file).read_text(encoding="utf-8"))


def test_golden_catalog_covers_required_page_types():
    catalog = load_catalog()
    ids = {case["id"] for case in catalog["cases"]}
    assert {
        "card-title-overflow",
        "kpi-as-title",
        "dense-cjk",
        "long-title",
        "tspan-wrap",
        "photo-hero",
        "unresolved-chart",
        "cover",
        "nested-tspan",
    } <= ids
    assert catalog["build_fingerprint"]["title_size_px"] == 40


def test_golden_svg_contract_matches_recorded_baseline():
    catalog = load_catalog()
    for case in catalog["cases"]:
        svg = load_case_svg(case)
        validate_svg_contract(svg, stage=case["svg_contract_stage"])


def test_golden_quality_gate_matches_m1_expectations():
    catalog = load_catalog()
    for case in catalog["cases"]:
        report = evaluate_page_quality(
            load_case_svg(case),
            stage=case["quality_stage"],
            content_plan=load_case_plan(case),
        )
        if case["quality"] == "fail":
            assert report["hard_fail"] is True, case["id"]
            codes = {item.get("code") for item in _fail_codes(report)}
            assert case["error_code"] in codes, (case["id"], codes)
        else:
            assert report["hard_fail"] is False, (case["id"], _fail_codes(report))


def _fail_codes(report: dict) -> list[dict]:
    items = []
    for check in report["checks"]:
        if check["status"] != "fail":
            continue
        if check.get("violations"):
            items.extend(check["violations"])
        else:
            items.append({"code": check["code"]})
    return items
