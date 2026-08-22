from __future__ import annotations

from typing import Any


class QualityGateError(RuntimeError):
    def __init__(self, report: dict[str, Any], *, message: str | None = None):
        self.report = report
        violation = first_violation(report)
        self.error_code = str(violation.get("code") or "QUALITY_GATE_FAILED")
        self.retryability = str(report.get("retryability") or retryability_for_code(self.error_code))
        self.category = str(report.get("category") or category_for_code(self.error_code))
        super().__init__(message or _message_from_report(report, self.error_code))

    def as_detail(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "category": self.category,
            "message": str(self),
            "retryability": self.retryability,
            "report_id": self.report.get("report_id"),
            "violations": flatten_violations(self.report),
        }


def retryability_for_code(code: str) -> str:
    mapping = {
        "TEXT_OUT_OF_BOUNDS": "after_relayout",
        "TEXT_OUTSIDE_CANVAS": "after_relayout",
        "TEXT_OUTSIDE_SAFE_AREA": "after_relayout",
        "TEXT_OVERLAP": "after_relayout",
        "FONT_BELOW_MINIMUM": "after_relayout",
        "TEXT_COVERS_CHROME": "after_relayout",
        "ROLE_TOKEN_MISMATCH": "after_provider_output_fix",
        "TOO_MANY_PAGE_TITLES": "after_relayout",
        "CONTENT_SKELETON_MISSING": "after_provider_output_fix",
        "UNRESOLVED_REQUIRED_SLOT": "after_asset_change",
        "NODE_ID_DUPLICATE": "after_provider_output_fix",
        "SCENE_NODE_MISSING": "after_provider_output_fix",
        "SCENE_NODE_ADDED": "after_provider_output_fix",
        "SCENE_ROLE_CHANGED": "after_provider_output_fix",
        "SCENE_READING_ORDER_CHANGED": "after_relayout",
        "EXPORT_SHAPES_PREFLIGHT": "not_retryable",
        "EXPORT_SHAPES_FAILED": "not_retryable",
        "SVG_CONTRACT_FAILED": "after_provider_output_fix",
        "CONFIGURATION": "after_config_change",
        "TRANSIENT_INFRA": "retry_now",
    }
    return mapping.get(code, "after_provider_output_fix")


def category_for_code(code: str) -> str:
    mapping = {
        "TEXT_OUT_OF_BOUNDS": "quality_violation",
        "TEXT_OUTSIDE_CANVAS": "quality_violation",
        "TEXT_OUTSIDE_SAFE_AREA": "quality_violation",
        "TEXT_OVERLAP": "quality_violation",
        "FONT_BELOW_MINIMUM": "quality_violation",
        "TEXT_COVERS_CHROME": "quality_violation",
        "ROLE_TOKEN_MISMATCH": "quality_violation",
        "TOO_MANY_PAGE_TITLES": "quality_violation",
        "CONTENT_SKELETON_MISSING": "quality_violation",
        "UNRESOLVED_REQUIRED_SLOT": "quality_violation",
        "NODE_ID_DUPLICATE": "quality_violation",
        "SCENE_NODE_MISSING": "quality_violation",
        "SCENE_NODE_ADDED": "quality_violation",
        "SCENE_ROLE_CHANGED": "quality_violation",
        "SCENE_READING_ORDER_CHANGED": "quality_violation",
        "EXPORT_SHAPES_PREFLIGHT": "contract_violation",
        "EXPORT_SHAPES_FAILED": "contract_violation",
        "SVG_CONTRACT_FAILED": "contract_violation",
        "CONFIGURATION": "configuration",
        "TRANSIENT_INFRA": "transient_infra",
        "USER_ACTION_REQUIRED": "user_action_required",
    }
    return mapping.get(code, "provider_output")


def classify_exception(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, QualityGateError):
        return exc.as_detail()
    from app.services.svg_contract import SvgContractError

    if isinstance(exc, SvgContractError):
        return {
            "error_code": "SVG_CONTRACT_FAILED",
            "category": "contract_violation",
            "message": str(exc),
            "retryability": "after_provider_output_fix",
            "violations": [violation.__dict__ for violation in exc.violations],
        }
    text = str(exc)
    lowered = text.lower()
    if "database is locked" in lowered or "database table is locked" in lowered:
        code = "TRANSIENT_INFRA"
    elif "429" in lowered or "timeout" in lowered or "timed out" in lowered:
        code = "TRANSIENT_INFRA"
    elif "无法翻译" in text or "嵌套 tspan" in text or "native shapes" in lowered:
        code = "EXPORT_SHAPES_FAILED"
    elif "style_preset" in lowered or "api key" in lowered or "未绑定" in text or "缺少 style" in text:
        code = "CONFIGURATION"
    elif "内容超载" in text or "页数预算" in text:
        code = "USER_ACTION_REQUIRED"
    else:
        code = "UNCLASSIFIED"
    return {
        "error_code": code,
        "category": category_for_code(code),
        "message": text or exc.__class__.__name__,
        "retryability": retryability_for_code(code),
        "violations": [],
    }


def first_violation(report: dict[str, Any] | None) -> dict[str, Any]:
    for check in (report or {}).get("checks") or []:
        if not isinstance(check, dict) or check.get("status") != "fail":
            continue
        violations = check.get("violations") or []
        if violations and isinstance(violations[0], dict):
            payload = dict(violations[0])
            payload.setdefault("code", check.get("code"))
            return payload
        return {"code": check.get("code"), "detail": check.get("detail") or ""}
    return {}


def flatten_violations(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for check in (report or {}).get("checks") or []:
        if not isinstance(check, dict):
            continue
        for violation in check.get("violations") or []:
            if isinstance(violation, dict):
                payload = dict(violation)
                payload.setdefault("code", check.get("code"))
                payload.setdefault("category", check.get("category"))
                items.append(payload)
    return items


def _message_from_report(report: dict[str, Any], error_code: str) -> str:
    violation = first_violation(report)
    detail = str(violation.get("detail") or violation.get("node_id") or "").strip()
    if detail:
        return f"质量闸门失败: {error_code}: {detail}"
    return f"质量闸门失败: {error_code}"
