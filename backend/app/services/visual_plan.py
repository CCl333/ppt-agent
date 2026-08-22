from __future__ import annotations

from typing import Any

SCHEMA_VERSION = "visual-plan.v1"
ALLOWED_KINDS = frozenset({"photo", "chart", "generated_image", "diagram", "none"})
ALLOWED_SOURCE_MODES = frozenset({"search", "generate", "render", "none"})
ALLOWED_PRIORITIES = frozenset({"required", "optional"})
ALLOWED_STATUSES = frozenset(
    {"planned", "bound", "ready", "fallback_applied", "failed", "skipped"}
)
READY_SLOT_STATUSES = frozenset({"bound", "ready", "fallback_applied"})


def has_visual_plan(plan: dict[str, Any] | None) -> bool:
    return bool(isinstance(plan, dict) and (plan.get("slots") or plan.get("visual_slots")))


def collect_visual_slots(*plans: dict[str, Any] | None) -> list[dict[str, Any]]:
    for plan in plans:
        if not isinstance(plan, dict):
            continue
        slots = plan.get("slots") if isinstance(plan.get("slots"), list) else None
        if slots is None:
            slots = plan.get("visual_slots") if isinstance(plan.get("visual_slots"), list) else None
        if slots:
            return [item for item in slots if isinstance(item, dict)]
    return []


def default_slot_policy(
    *,
    kind: str,
    page_role: str = "content",
    placement: str | None = None,
) -> dict[str, str | None]:
    role = (page_role or "content").strip() or "content"
    place = (placement or "").strip()
    if kind == "none":
        return {"priority": "optional", "fallback": None}
    if kind == "generated_image":
        return {"priority": "optional", "fallback": "skip"}
    if kind == "chart":
        return {"priority": "required", "fallback": None}
    if kind == "diagram":
        return {"priority": "required", "fallback": None}
    if kind == "photo":
        if role == "section":
            return {"priority": "optional", "fallback": "skip"}
        if role == "cover" or place == "hero":
            return {"priority": "required", "fallback": "reflow_without_visual"}
        return {"priority": "optional", "fallback": "skip"}
    return {"priority": "optional", "fallback": "skip"}


def propose_visual_plan(
    content_plan: dict[str, Any] | None = None,
    *,
    page_images: list[dict[str, Any]] | None = None,
    page_role: str = "content",
) -> dict[str, Any]:
    plan = content_plan if isinstance(content_plan, dict) else {}
    catalog = {
        str(item.get("image_id") or "").strip(): item
        for item in page_images or []
        if isinstance(item, dict) and str(item.get("image_id") or "").strip()
    }
    slots: list[dict[str, Any]] = []
    existing = collect_visual_slots(plan)
    if existing:
        slots.extend(existing)
    else:
        for index, item in enumerate(plan.get("image_slots") or []):
            if not isinstance(item, dict):
                continue
            image_id = str(item.get("image_id") or "").strip()
            asset = catalog.get(image_id) if image_id else None
            raw_place = str(item.get("placement") or "").strip()
            if not raw_place:
                raw_place = "hero" if page_role == "cover" else "support"
            slots.append(
                {
                    "slot_id": image_id or f"visual-{index + 1}",
                    "kind": "photo",
                    "source_mode": "search",
                    "intent": str(item.get("label") or image_id or "配图"),
                    "content_refs": [],
                    "placement": raw_place,
                    "asset_id": image_id if image_id in catalog else None,
                    "status": "bound" if image_id in catalog else "planned",
                    **_license_fields(asset),
                }
            )
        if not slots and page_role == "section":
            visual_intent = str(plan.get("visual_intent") or plan.get("badge") or "").strip()
            if visual_intent:
                slots.append(
                    {
                        "slot_id": "visual-atmosphere",
                        "kind": "photo",
                        "source_mode": "search",
                        "intent": visual_intent,
                        "content_refs": [],
                        "placement": "hero",
                        "status": "planned",
                    }
                )
        if not slots:
            for index, item in enumerate(page_images or []):
                if not isinstance(item, dict):
                    continue
                image_id = str(item.get("image_id") or "").strip()
                if not image_id:
                    continue
                placement = "hero" if page_role == "cover" and index == 0 else "support"
                slots.append(
                    {
                        "slot_id": f"visual-{index + 1}",
                        "kind": "photo",
                        "source_mode": "search",
                        "intent": str(item.get("caption") or item.get("source_title") or "配图"),
                        "content_refs": [],
                        "placement": placement,
                        "asset_id": image_id,
                        "status": "bound",
                        **_license_fields(item),
                    }
                )
        if not slots:
            slots.append(
                {
                    "slot_id": "visual-none",
                    "kind": "none",
                    "source_mode": "none",
                    "intent": "明确无图版式",
                    "content_refs": [],
                    "placement": "none",
                    "status": "ready",
                }
            )
    return normalize_visual_plan(
        {
            "schema_version": SCHEMA_VERSION,
            "policy": "page",
            "page_role": page_role,
            "slots": slots,
        }
    )


def normalize_visual_plan(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError("VisualPlan 必须是 JSON 对象")
    raw_slots = payload.get("slots")
    if raw_slots is None:
        raw_slots = payload.get("visual_slots") or []
    if not isinstance(raw_slots, list):
        raise RuntimeError("VisualPlan slots 必须是数组")
    page_role = str(payload.get("page_role") or "").strip()
    slots: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_slots):
        if not isinstance(item, dict):
            raise RuntimeError("VisualPlan slot 必须是对象")
        slot_id = str(item.get("slot_id") or f"visual-{index + 1}").strip()
        if slot_id in seen:
            raise RuntimeError(f"VisualPlan slot_id 重复: {slot_id}")
        seen.add(slot_id)
        kind = str(item.get("kind") or "").strip()
        if kind not in ALLOWED_KINDS:
            raise RuntimeError(f"VisualPlan 不支持的 kind: {kind or '(empty)'}")
        source_mode = str(item.get("source_mode") or _default_source_mode(kind)).strip()
        if source_mode not in ALLOWED_SOURCE_MODES:
            raise RuntimeError(f"VisualPlan 不支持的 source_mode: {source_mode}")
        status = str(item.get("status") or ("ready" if kind == "none" else "planned")).strip()
        if status not in ALLOWED_STATUSES:
            raise RuntimeError(f"VisualPlan 不支持的 status: {status}")
        placement = str(item.get("placement") or "").strip() or None
        policy = default_slot_policy(kind=kind, page_role=page_role, placement=placement)
        raw_priority = str(item.get("priority") or "").strip()
        if raw_priority and raw_priority not in ALLOWED_PRIORITIES:
            raise RuntimeError(f"VisualPlan 不支持的 priority: {raw_priority}")
        priority = raw_priority or str(policy["priority"] or "optional")
        raw_fallback = str(item.get("fallback") or "").strip()
        fallback = raw_fallback or policy.get("fallback")
        slot: dict[str, Any] = {
            "slot_id": slot_id,
            "kind": kind,
            "source_mode": source_mode,
            "intent": str(item.get("intent") or "").strip(),
            "content_refs": [str(ref) for ref in item.get("content_refs") or [] if str(ref).strip()],
            "placement": placement,
            "status": status,
            "priority": priority,
        }
        if fallback:
            slot["fallback"] = str(fallback)
        if item.get("asset_id"):
            slot["asset_id"] = str(item.get("asset_id")).strip()
        if item.get("output_id"):
            slot["output_id"] = str(item.get("output_id")).strip()
        license_status = str(item.get("license_status") or "").strip() or ("unknown" if kind == "photo" and item.get("asset_id") else "")
        if license_status:
            slot["license_status"] = license_status
        if isinstance(item.get("box"), dict):
            slot["box"] = item.get("box")
        if item.get("aspect_ratio"):
            slot["aspect_ratio"] = str(item.get("aspect_ratio")).strip()
        slots.append(slot)
    if not slots:
        raise RuntimeError("VisualPlan 至少需要一个 slot")
    return {
        "schema_version": str(payload.get("schema_version") or SCHEMA_VERSION),
        "policy": str(payload.get("policy") or "page"),
        "page_role": page_role or None,
        "slots": slots,
    }


def _default_source_mode(kind: str) -> str:
    return {
        "photo": "search",
        "chart": "render",
        "generated_image": "generate",
        "diagram": "render",
        "none": "none",
    }.get(kind, "none")


def _license_fields(item: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(item, dict):
        return {}
    status = str(item.get("license_status") or "unknown").strip() or "unknown"
    return {"license_status": status}
