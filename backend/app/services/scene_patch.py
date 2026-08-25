from __future__ import annotations

import re
from copy import deepcopy
from typing import Any
from xml.etree import ElementTree as ET

from app.services.content_plan import normalize_text
from app.services.layout_validator import SAFE_AREA, _inside
from app.services.page_scene import (
    CORE_TEXT_ROLES,
    _as_box,
    _node_map,
    normalize_page_scene,
)
from app.services.svg import (
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    SVG_NS,
    _ensure_svg_xmlns,
    _local_tag,
    _parse_svg,
    _serialize_svg,
)
from app.services.svg_contract import parse_length
from app.services.visual_plan import collect_visual_slots, normalize_visual_plan

MIN_BOX_SIZE = 8.0


class ScenePatchError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 422,
        error_code: str = "SCENE_PATCH_REJECTED",
        extra: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.extra = extra or {}

    def as_detail(self) -> dict[str, Any]:
        payload = {
            "error_code": self.error_code,
            "category": "user_action_required" if self.status_code == 409 else "quality_violation",
            "message": str(self),
            "retryability": "user_action_required" if self.status_code == 409 else "after_relayout",
        }
        payload.update(self.extra)
        return payload


def apply_scene_patch(
    *,
    svg_markup: str,
    content_plan: dict[str, Any] | None,
    visual_plan: dict[str, Any] | None,
    layout_plan: dict[str, Any] | None,
    text_edits: list[dict[str, Any]] | None = None,
    box_edits: list[dict[str, Any]] | None = None,
    slot_visibility: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    plan = deepcopy(content_plan) if isinstance(content_plan, dict) else {}
    visual = deepcopy(visual_plan) if isinstance(visual_plan, dict) else {"slots": []}
    scene = normalize_page_scene(deepcopy(layout_plan) if isinstance(layout_plan, dict) else {"nodes": []}, strict=False)
    texts = [item for item in text_edits or [] if isinstance(item, dict)]
    boxes = [item for item in box_edits or [] if isinstance(item, dict)]
    slots = [item for item in slot_visibility or [] if isinstance(item, dict)]
    if not texts and not boxes and not slots:
        raise ScenePatchError("没有可保存的修改")

    node_map = _node_map(scene)
    old_boxes = {node_id: _as_box(node.get("box")) for node_id, node in node_map.items()}
    _apply_text_edits(plan, scene, node_map, texts)
    layout_replanned = _apply_box_edits(scene, node_map, boxes)
    visual = _apply_slot_visibility(visual, scene, slots)
    scene = normalize_page_scene(scene, strict=True)
    _assert_boxes_legal(scene)
    markup = _rewrite_svg(svg_markup, scene, texts, visual, slots, old_boxes=old_boxes)
    return {
        "content_plan": plan,
        "visual_plan": visual,
        "layout_plan": {**scene, "layout_replanned": layout_replanned},
        "svg_markup": markup,
        "layout_replanned": layout_replanned,
    }


def _apply_text_edits(
    plan: dict[str, Any],
    scene: dict[str, Any],
    node_map: dict[str, dict[str, Any]],
    edits: list[dict[str, Any]],
) -> None:
    for item in edits:
        node_id = str(item.get("node_id") or "").strip()
        if not node_id:
            raise ScenePatchError("text_edits 缺少 node_id")
        node = node_map.get(node_id)
        if node is None:
            raise ScenePatchError(f"不能修改不存在的节点 {node_id}", error_code="SCENE_NODE_UNKNOWN")
        if str(node.get("kind") or "text") not in {"text"}:
            raise ScenePatchError(f"{node_id} 不是文案节点", error_code="SCENE_TEXT_ROLE_DENIED")
        text = normalize_text(item.get("text"))
        if not text:
            raise ScenePatchError(f"{node_id} 文案不能为空")
        text_ref = str(node.get("text_ref") or "") or _infer_text_ref(node_id)
        if not text_ref:
            raise ScenePatchError(f"{node_id} 没有可写回的文案引用", error_code="SCENE_TEXT_REF_MISSING")
        _write_text_ref(plan, text_ref, text, node_id=node_id)
        node["text"] = text
        node["text_ref"] = text_ref


def _infer_text_ref(node_id: str) -> str | None:
    if node_id == "page-title":
        return "content_plan.title"
    if node_id == "subtitle":
        return "content_plan.subtitle"
    matched = re.fullmatch(r"block-(\d+)-(title|body)", node_id)
    if not matched:
        return None
    index = int(matched.group(1)) - 1
    field = "label" if matched.group(2) == "title" else "note"
    return f"content_plan.blocks.{index}.{field}"


def _write_text_ref(plan: dict[str, Any], text_ref: str, text: str, *, node_id: str) -> None:
    if text_ref == "content_plan.title" or node_id == "page-title":
        plan["title"] = text
        return
    if text_ref == "content_plan.subtitle":
        plan["subtitle"] = text
        return
    if text_ref.startswith("content_plan.blocks."):
        parts = text_ref.split(".")
        if len(parts) < 4:
            raise ScenePatchError(f"无法解析 text_ref {text_ref}")
        try:
            index = int(parts[2])
        except ValueError as exc:
            raise ScenePatchError(f"无法解析 text_ref {text_ref}") from exc
        field = parts[3]
        blocks = plan.setdefault("blocks", [])
        if not isinstance(blocks, list) or index < 0 or index >= len(blocks) or not isinstance(blocks[index], dict):
            raise ScenePatchError(f"{node_id} 没有对应的内容模块")
        if field not in {"label", "note"}:
            raise ScenePatchError(f"不允许修改 {text_ref}")
        blocks[index][field] = text
        return
    if text_ref:
        raise ScenePatchError(f"不允许修改 {text_ref}")


def _apply_box_edits(
    scene: dict[str, Any],
    node_map: dict[str, dict[str, Any]],
    edits: list[dict[str, Any]],
) -> bool:
    requested: dict[str, dict[str, float]] = {}
    for item in edits:
        node_id = str(item.get("node_id") or "").strip()
        if not node_id:
            raise ScenePatchError("box_edits 缺少 node_id")
        if node_id not in node_map:
            raise ScenePatchError(f"不能移动不存在的节点 {node_id}", error_code="SCENE_NODE_UNKNOWN")
        box = _as_box(item.get("box"))
        if box is None:
            raise ScenePatchError(f"{node_id} 的布局盒无效")
        requested[node_id] = box
    if not requested:
        return False
    expanded = dict(requested)
    for node_id, new_box in list(requested.items()):
        node = node_map[node_id]
        if str(node.get("kind") or "") != "group":
            continue
        old_box = _as_box(node.get("box"))
        if old_box is None:
            continue
        for child_id in node.get("children") or []:
            child_id = str(child_id)
            if child_id in requested or child_id not in node_map:
                continue
            child_box = _as_box(node_map[child_id].get("box"))
            if child_box is None:
                continue
            expanded[child_id] = _map_child_box(old_box, new_box, child_box)
    for node_id, new_box in expanded.items():
        node_map[node_id]["box"] = new_box
    return True


def _map_child_box(old_group: dict[str, float], new_group: dict[str, float], child: dict[str, float]) -> dict[str, float]:
    sx = new_group["w"] / old_group["w"] if old_group["w"] else 1.0
    sy = new_group["h"] / old_group["h"] if old_group["h"] else 1.0
    return {
        "x": round(new_group["x"] + (child["x"] - old_group["x"]) * sx, 2),
        "y": round(new_group["y"] + (child["y"] - old_group["y"]) * sy, 2),
        "w": round(max(child["w"] * sx, MIN_BOX_SIZE), 2),
        "h": round(max(child["h"] * sy, MIN_BOX_SIZE), 2),
    }


def _apply_slot_visibility(
    visual_plan: dict[str, Any],
    scene: dict[str, Any],
    edits: list[dict[str, Any]],
) -> dict[str, Any]:
    if not edits:
        return normalize_visual_plan(visual_plan) if visual_plan.get("slots") or visual_plan.get("visual_slots") else visual_plan
    visual = normalize_visual_plan(visual_plan)
    slots = collect_visual_slots(visual)
    slot_map = {str(slot.get("slot_id")): slot for slot in slots}
    nodes = [node for node in scene.get("nodes") or [] if isinstance(node, dict)]
    node_map = {str(node.get("node_id")): node for node in nodes if node.get("node_id")}
    reading_order = [str(item) for item in scene.get("reading_order") or []]
    for item in edits:
        slot_id = str(item.get("slot_id") or "").strip()
        if not slot_id:
            raise ScenePatchError("slot_visibility 缺少 slot_id")
        slot = slot_map.get(slot_id)
        if slot is None:
            raise ScenePatchError(f"视觉槽 {slot_id} 不存在", error_code="SCENE_SLOT_UNKNOWN")
        visible = bool(item.get("visible"))
        priority = str(slot.get("priority") or "optional")
        if priority == "required" and not visible:
            raise ScenePatchError("不能去掉 required 视觉槽", error_code="SCENE_REQUIRED_SLOT")
        if str(slot.get("kind") or "") == "none":
            raise ScenePatchError("无图槽不能切换显隐")
        node = node_map.get(slot_id)
        if visible:
            slot["status"] = "bound" if slot.get("asset_id") else "planned"
            if slot_id not in node_map:
                box = _as_box(slot.get("box")) or {"x": 448.0, "y": 150.0, "w": 400.0, "h": 240.0}
                restored = {
                    "node_id": slot_id,
                    "kind": "visual-slot",
                    "role": "hero-visual",
                    "visual_slot_id": slot_id,
                    "box": box,
                }
                nodes.append(restored)
                node_map[slot_id] = restored
                if slot_id not in reading_order:
                    reading_order.append(slot_id)
        else:
            if node and node.get("box"):
                slot["box"] = node["box"]
            slot["status"] = "skipped"
            nodes = [candidate for candidate in nodes if str(candidate.get("node_id")) != slot_id]
            node_map.pop(slot_id, None)
            reading_order = [item_id for item_id in reading_order if item_id != slot_id]
    scene["nodes"] = nodes
    scene["reading_order"] = reading_order
    visual["slots"] = slots
    return visual


def _assert_boxes_legal(scene: dict[str, Any]) -> None:
    canvas = {"x": 0.0, "y": 0.0, "w": float(CANVAS_WIDTH), "h": float(CANVAS_HEIGHT)}
    for node in scene.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        box = _as_box(node.get("box"))
        if box is None:
            continue
        if box["w"] < MIN_BOX_SIZE or box["h"] < MIN_BOX_SIZE:
            raise ScenePatchError(f"{node.get('node_id')} 布局盒过小")
        if not _inside(box, canvas):
            raise ScenePatchError(f"{node.get('node_id')} 布局盒超出画布")
        kind = str(node.get("kind") or "")
        role = str(node.get("role") or "")
        if kind == "group" or role in CORE_TEXT_ROLES:
            if not _inside(box, SAFE_AREA):
                raise ScenePatchError(f"{node.get('node_id')} 布局盒超出 safe area")


def _rewrite_svg(
    svg_markup: str,
    scene: dict[str, Any],
    text_edits: list[dict[str, Any]],
    visual_plan: dict[str, Any],
    slot_edits: list[dict[str, Any]],
    *,
    old_boxes: dict[str, dict[str, float] | None] | None = None,
) -> str:
    root = _parse_svg(_ensure_svg_xmlns(svg_markup))
    node_map = _node_map(scene)
    text_map = {str(item.get("node_id")): normalize_text(item.get("text")) for item in text_edits if item.get("node_id")}
    previous = old_boxes or {}
    for node_id, node in node_map.items():
        box = _as_box(node.get("box"))
        elems = list(_iter_by_node_id(root, node_id))
        if node_id in text_map:
            if elems:
                for elem in elems:
                    if _local_tag(elem) in {"text", "tspan"}:
                        _set_text_content(elem, text_map[node_id])
            else:
                _append_text_node(root, node, text_map[node_id])
                elems = list(_iter_by_node_id(root, node_id))
        if box is None:
            continue
        for elem in elems:
            _apply_box_to_elem(elem, box, previous.get(node_id))
    hidden_ids = {str(item.get("slot_id")) for item in slot_edits if item.get("slot_id") and not item.get("visible")}
    shown_ids = {str(item.get("slot_id")) for item in slot_edits if item.get("slot_id") and item.get("visible")}
    parents = _parent_map(root)
    for slot_id in hidden_ids:
        for elem in list(_iter_by_node_id(root, slot_id)):
            parent = parents.get(elem)
            if parent is not None:
                parent.remove(elem)
                parents.pop(elem, None)
    slot_map = {str(slot.get("slot_id")): slot for slot in collect_visual_slots(visual_plan)}
    for slot_id in shown_ids:
        if list(_iter_by_node_id(root, slot_id)):
            continue
        node = node_map.get(slot_id)
        slot = slot_map.get(slot_id) or {}
        box = _as_box((node or {}).get("box")) or _as_box(slot.get("box")) or {"x": 448.0, "y": 150.0, "w": 400.0, "h": 240.0}
        image = ET.Element(f"{{{SVG_NS}}}image")
        image.set("data-node-id", slot_id)
        image.set("data-image-slot-id", slot_id)
        image.set("data-image-id", str(slot.get("asset_id") or slot_id))
        image.set("x", str(box["x"]))
        image.set("y", str(box["y"]))
        image.set("width", str(box["w"]))
        image.set("height", str(box["h"]))
        _set_layout_box_attr(image, box)
        root.append(image)
    return _serialize_svg(root)


def _iter_by_node_id(root: ET.Element, node_id: str):
    seen: set[int] = set()
    for elem in root.iter():
        ids = {
            (elem.get("data-node-id") or "").strip(),
            (elem.get("data-image-slot-id") or "").strip(),
            (elem.get("data-image-id") or "").strip(),
        }
        if node_id in ids and id(elem) not in seen:
            seen.add(id(elem))
            yield elem


def _parent_map(root: ET.Element) -> dict[ET.Element, ET.Element]:
    mapping: dict[ET.Element, ET.Element] = {}
    for parent in root.iter():
        for child in list(parent):
            mapping[child] = parent
    return mapping


def _set_text_content(elem: ET.Element, text: str) -> None:
    for child in list(elem):
        if _local_tag(child) == "tspan":
            elem.remove(child)
    elem.text = text
    elem.tail = None


def _append_text_node(root: ET.Element, node: dict[str, Any], text: str) -> None:
    box = _as_box(node.get("box")) or {"x": 64.0, "y": 72.0, "w": 200.0, "h": 40.0}
    elem = ET.Element(f"{{{SVG_NS}}}text")
    elem.set("data-node-id", str(node.get("node_id")))
    if node.get("role"):
        elem.set("data-text-role", str(node.get("role")))
        elem.set("class", f"t-{node.get('role')}")
    font_px = float(node.get("min_font_px") or 16)
    elem.set("x", str(box["x"]))
    elem.set("y", str(round(box["y"] + font_px * 0.88, 2)))
    elem.set("font-size", f"{font_px}px")
    elem.text = text
    _set_layout_box_attr(elem, box)
    root.append(elem)


def _apply_box_to_elem(elem: ET.Element, box: dict[str, float], old_box: dict[str, float] | None) -> None:
    tag = _local_tag(elem)
    if tag in {"image", "rect"}:
        elem.set("x", str(box["x"]))
        elem.set("y", str(box["y"]))
        elem.set("width", str(box["w"]))
        elem.set("height", str(box["h"]))
    elif tag in {"text", "tspan"}:
        origin = old_box or box
        _nudge(elem, box["x"] - origin["x"], box["y"] - origin["y"])
    _set_layout_box_attr(elem, box)


def _nudge(elem: ET.Element, dx: float, dy: float) -> None:
    if abs(dx) < 0.01 and abs(dy) < 0.01:
        return
    if elem.get("x") not in {None, ""}:
        elem.set("x", str(round(parse_length(elem.get("x")) + dx, 2)))
    if elem.get("y") not in {None, ""}:
        elem.set("y", str(round(parse_length(elem.get("y")) + dy, 2)))
    for child in list(elem):
        if _local_tag(child) in {"tspan", "text"}:
            _nudge(child, dx, dy)


def _set_layout_box_attr(elem: ET.Element, box: dict[str, float]) -> None:
    elem.set("data-layout-box", f'{box["x"]},{box["y"]},{box["w"]},{box["h"]}')
