from __future__ import annotations

from typing import Any

from app.services.layout_validator import (
    SAFE_AREA,
    box_from_scene,
    collect_text_items,
)
from app.services.style_tokens import min_font_px_for_token
from app.services.svg import CANVAS_HEIGHT, CANVAS_WIDTH, _ensure_svg_xmlns, _local_tag, _parse_svg
from app.services.svg_contract import flatten_group_translates, parse_length

SCHEMA_VERSION = "page-scene.v1"
CORE_TEXT_ROLES = frozenset({"display", "page-title", "card-title", "kpi", "toc-item"})
SKELETON_ROLES = frozenset({"display", "page-title", "card-title", "kpi", "toc-item"})
AUXILIARY_ROLES = frozenset({"subtitle", "body", "caption", "label", "table-header", "kpi-unit"})
SKELETON_DRIFT_HARD_FAIL = False
DEFAULT_BUDGETS = {
    "max_core_text_overlap": 0,
    "min_body_font_px": 14,
    "max_card_count": 5,
}


def normalize_page_scene(payload: Any, *, strict: bool = True) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError("PageScene 必须是 JSON 对象")
    nodes_raw = payload.get("nodes") or []
    if not isinstance(nodes_raw, list):
        raise RuntimeError("PageScene nodes 必须是数组")
    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in nodes_raw:
        if not isinstance(item, dict):
            raise RuntimeError("PageScene node 必须是对象")
        node_id = str(item.get("node_id") or "").strip()
        if not node_id:
            raise RuntimeError("PageScene node 缺少 node_id")
        if node_id in seen:
            if strict:
                raise RuntimeError(f"PageScene node_id 重复: {node_id}")
        else:
            seen.add(node_id)
        node = {
            "node_id": node_id,
            "kind": str(item.get("kind") or "text"),
            "role": str(item.get("role") or "").strip() or None,
            "box": _as_box(item.get("box")),
            "children": [str(child) for child in item.get("children") or [] if str(child).strip()],
        }
        if item.get("text_ref"):
            node["text_ref"] = str(item["text_ref"])
        if item.get("visual_slot_id"):
            node["visual_slot_id"] = str(item["visual_slot_id"])
        if item.get("max_lines") is not None:
            node["max_lines"] = int(item["max_lines"])
        if item.get("min_font_px") is not None:
            node["min_font_px"] = float(item["min_font_px"])
        if item.get("overflow"):
            node["overflow"] = str(item["overflow"])
        if item.get("actual_bbox"):
            node["actual_bbox"] = _as_box(item.get("actual_bbox"))
        if item.get("text"):
            node["text"] = str(item["text"])
        nodes.append(node)
    reading_order = [str(item).strip() for item in payload.get("reading_order") or [] if str(item).strip()]
    if not reading_order:
        reading_order = [node["node_id"] for node in nodes]
    unknown = [node_id for node_id in reading_order if node_id not in seen]
    if unknown:
        raise RuntimeError("PageScene reading_order 引用了不存在的节点: " + "、".join(unknown[:6]))
    budgets = dict(DEFAULT_BUDGETS)
    raw_budgets = payload.get("budgets") if isinstance(payload.get("budgets"), dict) else {}
    budgets.update({key: raw_budgets[key] for key in raw_budgets})
    canvas = payload.get("canvas") if isinstance(payload.get("canvas"), dict) else {}
    safe_area = payload.get("safe_area") if isinstance(payload.get("safe_area"), dict) else {}
    return {
        "schema_version": str(payload.get("schema_version") or SCHEMA_VERSION),
        "canvas": {
            "width": int(canvas.get("width") or CANVAS_WIDTH),
            "height": int(canvas.get("height") or CANVAS_HEIGHT),
        },
        "safe_area": _as_box(safe_area) or dict(SAFE_AREA),
        "nodes": nodes,
        "reading_order": reading_order,
        "budgets": budgets,
    }


def propose_layout_plan(content_plan: dict[str, Any] | None, *, page_role: str = "content") -> dict[str, Any]:
    plan = content_plan if isinstance(content_plan, dict) else {}
    nodes: list[dict[str, Any]] = []
    reading_order: list[str] = []
    title_role = "display" if page_role in {"cover", "section", "end"} else "page-title"
    title_node = {
        "node_id": "page-title",
        "kind": "text",
        "role": title_role,
        "text_ref": "content_plan.title",
        "box": {"x": 64.0, "y": 72.0, "w": 1152.0, "h": 52.0},
        "max_lines": 1,
        "min_font_px": 26.0,
        "overflow": "wrap_then_relayout",
        "text": str(plan.get("title") or ""),
    }
    nodes.append(title_node)
    reading_order.append("page-title")
    if str(plan.get("subtitle") or "").strip():
        nodes.append(
            {
                "node_id": "subtitle",
                "kind": "text",
                "role": "subtitle",
                "text_ref": "content_plan.subtitle",
                "box": {"x": 64.0, "y": 128.0, "w": 1152.0, "h": 36.0},
                "max_lines": 1,
                "min_font_px": 14.0,
                "overflow": "wrap_then_relayout",
                "text": str(plan.get("subtitle") or ""),
            }
        )
        reading_order.append("subtitle")
    blocks = [item for item in plan.get("blocks") or [] if isinstance(item, dict)]
    gap = 20.0
    area_y = 160.0
    area_h = 500.0
    count = max(len(blocks), 1)
    width = (1152.0 - gap * (count - 1)) / count if count else 1152.0
    for index, block in enumerate(blocks):
        card_id = f"block-{index + 1}"
        x = 64.0 + index * (width + gap)
        title_id = f"{card_id}-title"
        body_id = f"{card_id}-body"
        block_title_role = "toc-item" if page_role == "section" else "card-title"
        nodes.append(
            {
                "node_id": card_id,
                "kind": "group",
                "role": "card",
                "box": {"x": x, "y": area_y, "w": width, "h": area_h},
                "children": [title_id, body_id],
            }
        )
        nodes.append(
            {
                "node_id": title_id,
                "kind": "text",
                "role": block_title_role,
                "text_ref": f"content_plan.blocks.{index}.label",
                "box": {"x": x + 16.0, "y": area_y + 16.0, "w": max(width - 32.0, 80.0), "h": 54.0},
                "max_lines": 2,
                "min_font_px": 16.0,
                "overflow": "wrap_then_relayout",
                "text": str(block.get("label") or ""),
            }
        )
        nodes.append(
            {
                "node_id": body_id,
                "kind": "text",
                "role": "body",
                "text_ref": f"content_plan.blocks.{index}.note",
                "box": {"x": x + 16.0, "y": area_y + 78.0, "w": max(width - 32.0, 80.0), "h": max(area_h - 102.0, 40.0)},
                "max_lines": 6,
                "min_font_px": 14.0,
                "overflow": "wrap_then_relayout",
                "text": str(block.get("note") or ""),
            }
        )
        reading_order.extend([card_id, title_id, body_id])
    for index, slot in enumerate(plan.get("visual_slots") or plan.get("image_slots") or []):
        if not isinstance(slot, dict):
            continue
        kind = str(slot.get("kind") or "photo")
        if kind == "none":
            continue
        slot_id = str(slot.get("slot_id") or slot.get("image_id") or f"visual-{index + 1}")
        nodes.append(
            {
                "node_id": slot_id,
                "kind": "visual-slot",
                "role": "hero-visual",
                "visual_slot_id": slot_id,
                "box": slot.get("box") if isinstance(slot.get("box"), dict) else {"x": 448.0, "y": 150.0, "w": 768.0, "h": 360.0},
            }
        )
        reading_order.append(slot_id)
    return normalize_page_scene(
        {
            "schema_version": SCHEMA_VERSION,
            "canvas": {"width": CANVAS_WIDTH, "height": CANVAS_HEIGHT},
            "safe_area": dict(SAFE_AREA),
            "nodes": nodes,
            "reading_order": reading_order,
            "budgets": dict(DEFAULT_BUDGETS),
        }
    )


def extract_page_scene(
    svg_markup: str,
    *,
    content_plan: dict[str, Any] | None = None,
    base_scene: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = _parse_svg(_ensure_svg_xmlns(svg_markup))
    flatten_group_translates(root)
    texts = collect_text_items(root, layout_plan=base_scene)
    nodes: list[dict[str, Any]] = []
    reading_order: list[str] = []
    for item in texts:
        if item.get("chrome"):
            continue
        node_id = str(item.get("node_id") or "").strip()
        if not node_id:
            continue
        role = item.get("role") or _role_from_token(item.get("token"))
        box = item.get("layout_box") or box_from_scene(base_scene, node_id) or item.get("bbox")
        nodes.append(
            {
                "node_id": node_id,
                "kind": "text",
                "role": role,
                "box": _as_box(box),
                "actual_bbox": _as_box(item.get("bbox")),
                "min_font_px": min_font_px_for_token(item.get("token")),
                "overflow": "wrap_then_relayout",
                "text": item.get("text") or "",
            }
        )
        reading_order.append(node_id)
    for elem in root.iter():
        if _local_tag(elem) != "image":
            continue
        node_id = (
            (elem.get("data-node-id") or "").strip()
            or (elem.get("data-image-slot-id") or "").strip()
            or (elem.get("data-image-id") or "").strip()
        )
        if not node_id:
            continue
        box = {
            "x": parse_length(elem.get("x")),
            "y": parse_length(elem.get("y")),
            "w": parse_length(elem.get("width")),
            "h": parse_length(elem.get("height")),
        }
        nodes.append(
            {
                "node_id": node_id,
                "kind": "visual-slot",
                "role": (elem.get("data-text-role") or "hero-visual"),
                "visual_slot_id": (elem.get("data-image-slot-id") or elem.get("data-image-id") or node_id),
                "box": box,
                "actual_bbox": box,
            }
        )
        reading_order.append(node_id)
    nodes, reading_order = _merge_base_scene(nodes, reading_order, base_scene)
    scene = {
        "schema_version": SCHEMA_VERSION,
        "canvas": {"width": CANVAS_WIDTH, "height": CANVAS_HEIGHT},
        "safe_area": dict(SAFE_AREA),
        "nodes": nodes,
        "reading_order": reading_order,
        "budgets": dict(DEFAULT_BUDGETS),
        "content_plan_title": str((content_plan or {}).get("title") or ""),
    }
    return normalize_page_scene(scene, strict=False)


def _merge_base_scene(
    nodes: list[dict[str, Any]],
    reading_order: list[str],
    base_scene: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    base_map = _node_map(base_scene)
    if not base_map:
        return nodes, reading_order
    for node in nodes:
        base = base_map.get(str(node.get("node_id") or ""))
        if not base:
            continue
        if base.get("text_ref") and not node.get("text_ref"):
            node["text_ref"] = base["text_ref"]
        if base.get("children") and not node.get("children"):
            node["children"] = list(base["children"])
        if base.get("visual_slot_id") and not node.get("visual_slot_id"):
            node["visual_slot_id"] = base["visual_slot_id"]
        if base.get("max_lines") is not None and node.get("max_lines") is None:
            node["max_lines"] = base["max_lines"]
        if base.get("min_font_px") is not None and node.get("min_font_px") is None:
            node["min_font_px"] = base["min_font_px"]
    extracted_ids = {str(node.get("node_id") or "") for node in nodes}
    for node_id, base in base_map.items():
        if node_id in extracted_ids or str(base.get("kind") or "") != "group":
            continue
        nodes.append(
            {
                "node_id": node_id,
                "kind": "group",
                "role": base.get("role"),
                "box": _as_box(base.get("box")),
                "children": list(base.get("children") or []),
            }
        )
        extracted_ids.add(node_id)
    ordered: list[str] = []
    seen: set[str] = set()
    for node_id in list((base_scene or {}).get("reading_order") or []) + reading_order:
        if node_id in extracted_ids and node_id not in seen:
            ordered.append(node_id)
            seen.add(node_id)
    for node in nodes:
        node_id = str(node.get("node_id") or "")
        if node_id and node_id not in seen:
            ordered.append(node_id)
            seen.add(node_id)
    return nodes, ordered


def diff_page_scenes(base: dict[str, Any] | None, other: dict[str, Any] | None) -> dict[str, Any]:
    base_map = _node_map(base)
    other_map = _node_map(other)
    missing = [node_id for node_id in base_map if node_id not in other_map]
    added = [node_id for node_id in other_map if node_id not in base_map]
    role_changed: list[dict[str, Any]] = []
    box_changed: list[dict[str, Any]] = []
    for node_id, left in base_map.items():
        right = other_map.get(node_id)
        if not right:
            continue
        if (left.get("role") or "") != (right.get("role") or ""):
            role_changed.append({"node_id": node_id, "from": left.get("role"), "to": right.get("role")})
        if left.get("box") and right.get("box") and box_delta(left["box"], right["box"]) > 0.05:
            box_changed.append(
                {
                    "node_id": node_id,
                    "from": left.get("box"),
                    "to": right.get("box"),
                    "delta": box_delta(left["box"], right["box"]),
                }
            )
    return {
        "missing": missing,
        "added": added,
        "role_changed": role_changed,
        "box_changed": box_changed,
        "reading_order_changed": list((base or {}).get("reading_order") or []) != list((other or {}).get("reading_order") or []),
    }


def _node_map(scene: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(scene, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for node in scene.get("nodes") or []:
        if isinstance(node, dict) and node.get("node_id"):
            result[str(node["node_id"])] = node
    return result


def _role_from_token(token: str | None) -> str | None:
    if not token:
        return None
    if token.startswith("t-"):
        return token[2:]
    return token


def _as_box(raw: Any) -> dict[str, float] | None:
    if not isinstance(raw, dict):
        return None
    try:
        return {
            "x": round(float(raw["x"]), 2),
            "y": round(float(raw["y"]), 2),
            "w": round(float(raw["w"]), 2),
            "h": round(float(raw["h"]), 2),
        }
    except (KeyError, TypeError, ValueError):
        return None


def box_delta(left: dict[str, float], right: dict[str, float]) -> float:
    deltas = []
    for key in ("x", "y", "w", "h"):
        base = abs(float(left.get(key) or 0)) or 1.0
        deltas.append(abs(float(right.get(key) or 0) - float(left.get(key) or 0)) / base)
    return max(deltas) if deltas else 0.0


def layout_drift_class(node: dict[str, Any] | None, *, visual_slots: list[dict[str, Any]] | None = None) -> str:
    item = node if isinstance(node, dict) else {}
    kind = str(item.get("kind") or "")
    role = str(item.get("role") or "")
    if kind == "group" or role in SKELETON_ROLES:
        return "skeleton"
    if kind == "visual-slot":
        slot_id = str(item.get("visual_slot_id") or item.get("node_id") or "")
        slot = next(
            (
                candidate
                for candidate in visual_slots or []
                if str(candidate.get("slot_id") or "") == slot_id
            ),
            None,
        )
        if slot and str(slot.get("priority") or "") == "required":
            return "skeleton"
        return "decorative"
    if kind in {"decoration", "atmosphere"}:
        return "decorative"
    if role in AUXILIARY_ROLES:
        return "auxiliary"
    return "auxiliary"
