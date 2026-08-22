from __future__ import annotations

from app.services.error_policy import QualityGateError, classify_exception, retryability_for_code
from app.services.quality_report import build_quality_report, make_check
from app.services.svg_contract import SvgContractError, SvgViolation


def test_layout_codes_map_to_relayout():
    assert retryability_for_code("TEXT_OUT_OF_BOUNDS") == "after_relayout"
    assert retryability_for_code("UNRESOLVED_REQUIRED_SLOT") == "after_asset_change"
    assert retryability_for_code("EXPORT_SHAPES_PREFLIGHT") == "not_retryable"


def test_quality_gate_error_uses_first_hard_fail():
    report = build_quality_report(
        [
            make_check(
                "text_out_of_bounds",
                "layout",
                status="fail",
                violations=[{"code": "TEXT_OUT_OF_BOUNDS", "node_id": "block-1-title", "detail": "出框"}],
            )
        ]
    )
    error = QualityGateError(report)
    detail = error.as_detail()
    assert detail["error_code"] == "TEXT_OUT_OF_BOUNDS"
    assert detail["category"] == "quality_violation"
    assert detail["retryability"] == "after_relayout"
    assert detail["violations"][0]["node_id"] == "block-1-title"


def test_svg_contract_error_is_not_retried_blindly():
    exc = SvgContractError("bad", [SvgViolation("/svg", "unknown_element", "<foo>")])
    payload = classify_exception(exc)
    assert payload["category"] == "contract_violation"
    assert payload["retryability"] == "after_provider_output_fix"


def test_sqlite_lock_is_transient():
    payload = classify_exception(RuntimeError("database is locked"))
    assert payload["category"] == "transient_infra"
    assert payload["retryability"] == "retry_now"
