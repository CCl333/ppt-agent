from __future__ import annotations

from typing import Any

from app.services.content_plan import collect_plan_skeleton, collect_svg_texts, has_content_plan, normalize_text
from app.services.error_policy import QualityGateError
from app.services.export_preflight import preflight_shapes_slide
from app.services.layout_validator import check_layout
from app.services.page_scene import (
    CORE_TEXT_ROLES,
    SKELETON_DRIFT_HARD_FAIL,
    box_delta,
    diff_page_scenes,
    extract_page_scene,
    layout_drift_class,
)

DRAFT_EQUIVALENT_ROLES = (
    frozenset({"display", "page-title"}),
    frozenset({"card-title", "toc-item"}),
)
from app.services.quality_report import (
    CONTRACT_VERSION,
    build_quality_report,
    hash_svg,
    make_check,
    report_retryability,
    report_status_reason,
)

from app.services.style_tokens import FORBIDDEN_LEGACY_TITLE_ROLES
from app.services.svg_contract import SvgContractError, validate_svg_contract
from app.services.visual_plan import READY_SLOT_STATUSES, collect_visual_slots


def evaluate_page_quality(
    svg_markup: str,
    *,
    stage: str,
    content_plan: dict[str, Any] | None = None,
    layout_plan: dict[str, Any] | None = None,
    visual_plan: dict[str, Any] | None = None,
    style_pack: dict[str, Any] | None = None,
    run_export_preflight: bool | None = None,
) -> dict[str, Any]:
    layout_checks, metrics, _texts = check_layout(
        svg_markup,
        layout_plan=layout_plan,
        typography=(style_pack or {}).get("typography") if isinstance(style_pack, dict) else None,
    )
    checks = [_svg_profile_check(svg_markup, stage=stage, style_pack=style_pack), *layout_checks]
    checks.append(_content_skeleton_check(svg_markup, content_plan))
    checks.append(_text_role_check(_texts))
    slot_check, unresolved = _required_slot_check(visual_plan, content_plan)
    checks.append(slot_check)
    extracted = extract_page_scene(svg_markup, content_plan=content_plan, base_scene=layout_plan)
    identity, drift_warnings = _scene_identity_check(
        extracted, layout_plan, stage=stage, visual_plan=visual_plan
    )
    checks.append(identity)
    if stage in {"draft", "design"}:
        checks, drift_warnings = _soften_draft_layout_box_check(checks, drift_warnings)
    metrics["unresolved_required_slot_count"] = unresolved
    metrics["visual_slot_count"] = len(collect_visual_slots(visual_plan, content_plan))
    metrics["scene_node_count"] = len(extracted.get("nodes") or [])
    should_preflight = run_export_preflight if run_export_preflight is not None else stage == "design"
    export_payload: dict[str, Any] | None = None
    if should_preflight:
        export_payload = preflight_shapes_slide(svg_markup)
        export_status = "fail" if export_payload.get("status") == "fail" else "pass"
        checks.append(
            make_check(
                "export_shapes_preflight",
                "exportability",
                status=export_status,
                violations=list(export_payload.get("violations") or []),
                detail=str(export_payload.get("detail") or ""),
            )
        )
    report = build_quality_report(checks, metrics=metrics, warnings=drift_warnings)
    report["stage"] = stage
    report["svg_hash"] = hash_svg(svg_markup)
    report["contract_version"] = CONTRACT_VERSION
    report["retryability"] = report_retryability(report) if report["hard_fail"] else None
    report["status_reason"] = report_status_reason(report)
    report["export_preflight"] = export_payload
    report["layout_plan"] = extracted
    report["visual_plan"] = visual_plan or {"slots": collect_visual_slots(content_plan)}
    return report


def assert_quality_ready(report: dict[str, Any], *, stage: str) -> None:
    if report.get("hard_fail"):
        raise QualityGateError(report, message=f"{stage} 质量闸门未通过: {report.get('status_reason')}")


def apply_report_to_version(version: Any, report: dict[str, Any], *, svg_markup: str) -> None:
    version.quality_report_json = report
    version.svg_hash = report.get("svg_hash") or hash_svg(svg_markup)
    version.contract_version = report.get("contract_version") or CONTRACT_VERSION
    version.status_reason = report.get("status_reason")
    version.retryability = report.get("retryability")
    if hasattr(version, "export_preflight_json"):
        version.export_preflight_json = report.get("export_preflight") or {}
    version.status = "failed" if report.get("hard_fail") else "ready"


def _soften_draft_layout_box_check(
    checks: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    extra = list(warnings)
    softened: list[dict[str, Any]] = []
    for check in checks:
        if check.get("code") != "text_out_of_bounds" or check.get("status") != "fail":
            softened.append(check)
            continue
        extra.extend(list(check.get("violations") or []))
        item = dict(check)
        item["status"] = "pass"
        softened.append(item)
    return softened, extra


def _svg_profile_check(
    svg_markup: str,
    *,
    stage: str,
    style_pack: dict[str, Any] | None = None,
) -> dict[str, Any]:
    contract_stage = stage if style_pack is not None or stage != "design" else "draft"
    try:
        validate_svg_contract(svg_markup, stage=contract_stage, style_pack=style_pack)
    except SvgContractError as exc:
        return make_check(
            "svg_profile",
            "structural",
            status="fail",
            violations=[
                {
                    "code": "SVG_CONTRACT_FAILED",
                    "xpath": getattr(item, "xpath", ""),
                    "rule": getattr(item, "rule", ""),
                    "detail": getattr(item, "detail", str(exc)),
                }
                for item in exc.violations
            ]
            or [{"code": "SVG_CONTRACT_FAILED", "detail": str(exc)}],
        )
    return make_check("svg_profile", "structural", status="pass")


def _text_role_check(texts: list[dict[str, Any]]) -> dict[str, Any]:
    violations: list[dict[str, Any]] = []
    page_title_nodes: list[str] = []
    for item in texts:
        if item.get("chrome"):
            continue
        token = item.get("class_token") or item.get("token")
        role = item.get("role")
        if token == "t-title" and role in FORBIDDEN_LEGACY_TITLE_ROLES:
            violations.append(
                {
                    "code": "ROLE_TOKEN_MISMATCH",
                    "node_id": item.get("node_id"),
                    "detail": f"{item.get('node_id') or item.get('text')} 使用 t-title 承载 {role}",
                    "role": role,
                    "token": token,
                }
            )
        if token == "t-card-title" and role == "kpi":
            violations.append(
                {
                    "code": "ROLE_TOKEN_MISMATCH",
                    "node_id": item.get("node_id"),
                    "detail": "KPI 不能使用 t-card-title",
                    "role": role,
                    "token": token,
                }
            )
        if item.get("token") in {"t-page-title", "t-display"} or (
            (item.get("class_token") or item.get("token")) == "t-title" and role in {None, "page-title", "display"}
        ):
            page_title_nodes.append(str(item.get("node_id") or item.get("text") or "page-title"))
    if len(page_title_nodes) > 1:
        violations.append(
            {
                "code": "TOO_MANY_PAGE_TITLES",
                "detail": "页主标题超过一个: " + "、".join(page_title_nodes[:6]),
                "node_id": page_title_nodes[0],
            }
        )
    return make_check(
        "text_role",
        "semantic",
        status="fail" if violations else "pass",
        violations=violations,
    )


def _scene_identity_check(
    extracted: dict[str, Any],
    expected: dict[str, Any] | None,
    *,
    stage: str = "draft",
    visual_plan: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    violations: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for node in extracted.get("nodes") or []:
        node_id = str(node.get("node_id") or "")
        if not node_id:
            continue
        seen[node_id] = seen.get(node_id, 0) + 1
    for node_id, count in seen.items():
        if count > 1:
            violations.append(
                {
                    "code": "NODE_ID_DUPLICATE",
                    "node_id": node_id,
                    "detail": f"node_id 重复: {node_id}",
                }
            )
    if isinstance(expected, dict) and expected.get("nodes"):
        diff = diff_page_scenes(expected, extracted)
        expected_map = {
            str(node.get("node_id")): node
            for node in expected.get("nodes") or []
            if isinstance(node, dict) and node.get("node_id")
        }
        extracted_map = {
            str(node.get("node_id")): node
            for node in extracted.get("nodes") or []
            if isinstance(node, dict) and node.get("node_id")
        }
        for node_id in diff["missing"]:
            node = expected_map.get(node_id) or {}
            if node.get("kind") == "group":
                continue
            if node.get("kind") == "visual-slot" and stage != "design":
                continue
            if node.get("role") in CORE_TEXT_ROLES or node.get("kind") == "visual-slot":
                violations.append(
                    {
                        "code": "SCENE_NODE_MISSING",
                        "node_id": node_id,
                        "detail": f"Design 丢失核心节点 {node_id}",
                        "expected_box": node.get("box"),
                    }
                )
        for node_id in diff["added"]:
            node = extracted_map.get(node_id) or {}
            if node.get("role") in CORE_TEXT_ROLES:
                item = {
                    "code": "SCENE_NODE_ADDED",
                    "node_id": node_id,
                    "detail": f"新增核心节点 {node_id}",
                    "actual_bbox": node.get("actual_bbox") or node.get("box"),
                }
                if stage == "draft":
                    warnings.append(item)
                else:
                    violations.append(item)
        for item in diff["role_changed"]:
            if _roles_equivalent(item.get("from"), item.get("to"), stage=stage):
                continue
            if item.get("from") in CORE_TEXT_ROLES or item.get("to") in CORE_TEXT_ROLES:
                violations.append(
                    {
                        "code": "SCENE_ROLE_CHANGED",
                        "node_id": item.get("node_id"),
                        "detail": f"{item.get('node_id')} role {item.get('from')} → {item.get('to')}",
                    }
                )
        expected_order = [
            node_id
            for node_id in expected.get("reading_order") or []
            if (expected_map.get(node_id) or {}).get("role") in CORE_TEXT_ROLES
        ]
        extracted_order = [
            node_id
            for node_id in extracted.get("reading_order") or []
            if node_id in expected_order
        ]
        if expected_order and extracted_order != expected_order:
            violations.append(
                {
                    "code": "SCENE_READING_ORDER_CHANGED",
                    "detail": "核心节点阅读顺序被改变",
                    "node_id": expected_order[0],
                }
            )
        visual_slots = collect_visual_slots(visual_plan)
        for node_id, left in expected_map.items():
            right = extracted_map.get(node_id)
            if not right:
                continue
            planned = left.get("box")
            actual = right.get("actual_bbox") or right.get("box")
            if not planned or not actual:
                continue
            delta = box_delta(planned, actual)
            if delta <= 0.05:
                continue
            drift_class = layout_drift_class(left, visual_slots=visual_slots)
            if drift_class == "decorative":
                continue
            item = {
                "code": "LAYOUT_BOX_DRIFT",
                "node_id": node_id,
                "detail": f"{node_id} 布局盒变化 {round(delta * 100)}%",
                "expected_box": planned,
                "actual_bbox": actual,
                "drift_class": drift_class,
            }
            if drift_class == "skeleton" and SKELETON_DRIFT_HARD_FAIL:
                violations.append(item)
            else:
                warnings.append(item)
    return (
        make_check(
            "scene_identity",
            "semantic",
            status="fail" if violations else "pass",
            violations=violations,
        ),
        warnings,
    )


def _roles_equivalent(left: Any, right: Any, *, stage: str) -> bool:
    if left == right:
        return True
    if stage != "draft":
        return False
    left_role = str(left or "").strip()
    right_role = str(right or "").strip()
    for group in DRAFT_EQUIVALENT_ROLES:
        if left_role in group and right_role in group:
            return True
    return False


def _content_skeleton_check(svg_markup: str, plan: dict[str, Any] | None) -> dict[str, Any]:
    if not has_content_plan(plan):
        return make_check("content_skeleton", "semantic", status="pass")
    blob = normalize_text("".join(collect_svg_texts(svg_markup)))
    missing = [item for item in collect_plan_skeleton(plan) if item not in blob]
    if not missing:
        return make_check("content_skeleton", "semantic", status="pass")
    return make_check(
        "content_skeleton",
        "semantic",
        status="fail",
        violations=[
            {
                "code": "CONTENT_SKELETON_MISSING",
                "detail": "缺失: " + "、".join(missing[:8]),
                "missing": missing,
            }
        ],
    )


def _required_slot_check(
    visual_plan: dict[str, Any] | None,
    content_plan: dict[str, Any] | None,
) -> tuple[dict[str, Any], int]:
    unresolved: list[dict[str, Any]] = []
    for slot in collect_visual_slots(visual_plan, content_plan):
        if str(slot.get("priority") or "optional") != "required":
            continue
        status = str(slot.get("status") or "")
        if status in READY_SLOT_STATUSES:
            continue
        unresolved.append(
            {
                "code": "UNRESOLVED_REQUIRED_SLOT",
                "node_id": slot.get("slot_id"),
                "detail": f"required visual slot {slot.get('slot_id') or ''} 未实现",
                "kind": slot.get("kind"),
            }
        )
    check = make_check(
        "required_visual_slot",
        "semantic",
        status="fail" if unresolved else "pass",
        violations=unresolved,
    )
    return check, len(unresolved)
