from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from app.models.base import new_id

QUALITY_SCHEMA_VERSION = "quality-report.v1"
VALIDATOR_VERSION = "quality.v1"
CONTRACT_VERSION = "page-quality.v1"


def hash_svg(markup: str) -> str:
    return "sha256:" + hashlib.sha256((markup or "").encode("utf-8")).hexdigest()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def make_check(
    code: str,
    category: str,
    *,
    status: str = "pass",
    violations: list[dict[str, Any]] | None = None,
    detail: str = "",
) -> dict[str, Any]:
    payload = {
        "code": code,
        "category": category,
        "status": status,
        "violations": list(violations or []),
    }
    if detail:
        payload["detail"] = detail
    return payload


def build_quality_report(
    checks: list[dict[str, Any]],
    *,
    metrics: dict[str, Any] | None = None,
    warnings: list[dict[str, Any]] | None = None,
    subjective_score: float | None = None,
) -> dict[str, Any]:
    normalized: list[dict[str, Any]] = []
    hard_fail = False
    for check in checks:
        item = dict(check)
        status = item.get("status") or ("fail" if item.get("violations") else "pass")
        item["status"] = status
        item.setdefault("violations", [])
        if status == "fail":
            hard_fail = True
        normalized.append(item)
    report_status = "fail" if hard_fail else "pass"
    warning_items = list(warnings or [])
    return {
        "schema_version": QUALITY_SCHEMA_VERSION,
        "report_id": new_id(),
        "status": report_status,
        "hard_fail": hard_fail,
        "checks": normalized,
        "metrics": dict(metrics or {}),
        "warnings": warning_items,
        "subjective_score": subjective_score,
        "tracks": {
            "hard_fail": hard_fail,
            "measured_warning": warning_items,
            "subjective_score": subjective_score,
            "human_calibrated": None,
        },
        "generated_at": utc_now_iso(),
        "validator_version": VALIDATOR_VERSION,
    }


def report_retryability(report: dict[str, Any]) -> str:
    from app.services.error_policy import first_violation, retryability_for_code

    violation = first_violation(report)
    return retryability_for_code(str(violation.get("code") or "QUALITY_GATE_FAILED"))


def report_status_reason(report: dict[str, Any]) -> str | None:
    from app.services.error_policy import first_violation

    if not report.get("hard_fail"):
        return None
    violation = first_violation(report)
    return str(violation.get("code") or "QUALITY_GATE_FAILED")
