from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.models.entities import DraftVersion
from app.services.page_scene import propose_layout_plan
from app.services.scene_patch import ScenePatchError, apply_scene_patch
from app.services.visual_plan import normalize_visual_plan
from tests.helpers import make_content_page, make_project

DRAFT_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" width="1280" height="720">
  <text x="64" y="110" class="t-page-title" data-node-id="page-title" data-text-role="page-title"
        data-layout-box="64,72,1152,52" font-size="32px">行程预算</text>
  <text x="80" y="196" class="t-card-title" data-node-id="block-1-title" data-text-role="card-title"
        data-layout-box="64,176,360,54" font-size="18px">预约制</text>
  <text x="80" y="260" class="t-body" data-node-id="block-1-body" data-text-role="body"
        data-layout-box="80,238,328,200" font-size="14px">提前约</text>
</svg>
"""

SLOT_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" width="1280" height="720">
  <text x="64" y="110" class="t-page-title" data-node-id="page-title" data-text-role="page-title"
        data-layout-box="64,72,1152,52" font-size="32px">行程预算</text>
  <text x="80" y="196" class="t-card-title" data-node-id="block-1-title" data-text-role="card-title"
        data-layout-box="80,176,1120,54" font-size="18px">预约制</text>
  <image data-node-id="visual-1" data-image-slot-id="visual-1" data-image-id="visual-1"
         x="720" y="160" width="400" height="240"/>
</svg>
"""


def _content_plan() -> dict:
    return {
        "page_code": "page-03",
        "title": "行程预算",
        "subtitle": "",
        "badge": "",
        "blocks": [{"role": "point", "label": "预约制", "note": "提前约"}],
        "footer": {},
        "image_slots": [],
    }


def _none_visual() -> dict:
    return normalize_visual_plan(
        {
            "schema_version": "visual-plan.v1",
            "page_role": "content",
            "slots": [
                {
                    "slot_id": "visual-none",
                    "kind": "none",
                    "source_mode": "none",
                    "intent": "明确无图版式",
                    "placement": "none",
                    "status": "ready",
                }
            ],
        }
    )


def _optional_visual() -> dict:
    return normalize_visual_plan(
        {
            "schema_version": "visual-plan.v1",
            "page_role": "content",
            "slots": [
                {
                    "slot_id": "visual-1",
                    "kind": "photo",
                    "source_mode": "search",
                    "intent": "配图",
                    "placement": "support",
                    "status": "bound",
                    "asset_id": "visual-1",
                    "priority": "optional",
                    "box": {"x": 720, "y": 160, "w": 400, "h": 240},
                }
            ],
        }
    )


def _seed_draft(session, page, *, svg: str = DRAFT_SVG, visual=None, content=None, extra_slots=None):
    content_plan = content or _content_plan()
    layout = propose_layout_plan(content_plan, page_role="content")
    if extra_slots:
        layout["nodes"].extend(extra_slots)
        layout["reading_order"].extend(node["node_id"] for node in extra_slots)
    draft = session.get(DraftVersion, page.current_draft_version_id)
    assert draft is not None
    draft.draft_svg_markup = svg
    draft.content_plan_json = content_plan
    draft.visual_plan_json = visual if visual is not None else _none_visual()
    draft.layout_plan_json = layout
    session.commit()
    session.refresh(page)
    return draft


def test_apply_scene_patch_updates_text_and_layout_box():
    content = _content_plan()
    layout = propose_layout_plan(content)
    result = apply_scene_patch(
        svg_markup=DRAFT_SVG,
        content_plan=content,
        visual_plan=_none_visual(),
        layout_plan=layout,
        text_edits=[{"node_id": "page-title", "text": "行程安排"}],
        box_edits=[{"node_id": "page-title", "box": {"x": 64, "y": 92, "w": 1152, "h": 52}}],
    )
    assert result["content_plan"]["title"] == "行程安排"
    title = next(node for node in result["layout_plan"]["nodes"] if node["node_id"] == "page-title")
    assert title["box"]["y"] == 92
    assert result["layout_replanned"] is True
    assert "行程安排" in result["svg_markup"]
    assert "64.0,92.0,1152.0,52.0" in result["svg_markup"]


def test_apply_scene_patch_rejects_required_slot_hide():
    content = _content_plan()
    layout = propose_layout_plan({**content, "image_slots": []})
    layout["nodes"].append(
        {
            "node_id": "visual-1",
            "kind": "visual-slot",
            "role": "hero-visual",
            "visual_slot_id": "visual-1",
            "box": {"x": 720, "y": 160, "w": 400, "h": 240},
        }
    )
    visual = normalize_visual_plan(
        {
            "page_role": "cover",
            "slots": [
                {
                    "slot_id": "visual-1",
                    "kind": "photo",
                    "source_mode": "search",
                    "intent": "封面主图",
                    "placement": "hero",
                    "status": "bound",
                    "asset_id": "visual-1",
                    "priority": "required",
                }
            ],
        }
    )
    with pytest.raises(ScenePatchError) as exc:
        apply_scene_patch(
            svg_markup=SLOT_SVG,
            content_plan=content,
            visual_plan=visual,
            layout_plan=layout,
            slot_visibility=[{"slot_id": "visual-1", "visible": False}],
        )
    assert exc.value.error_code == "SCENE_REQUIRED_SLOT"


def test_apply_scene_patch_hides_optional_slot():
    content = _content_plan()
    layout = propose_layout_plan(content)
    layout["nodes"].append(
        {
            "node_id": "visual-1",
            "kind": "visual-slot",
            "role": "hero-visual",
            "visual_slot_id": "visual-1",
            "box": {"x": 720, "y": 160, "w": 400, "h": 240},
        }
    )
    result = apply_scene_patch(
        svg_markup=SLOT_SVG,
        content_plan=content,
        visual_plan=_optional_visual(),
        layout_plan=layout,
        slot_visibility=[{"slot_id": "visual-1", "visible": False}],
    )
    assert all(node["node_id"] != "visual-1" for node in result["layout_plan"]["nodes"])
    slot = result["visual_plan"]["slots"][0]
    assert slot["status"] == "skipped"
    assert "data-node-id=\"visual-1\"" not in result["svg_markup"]


def test_apply_scene_patch_rejects_unknown_node():
    with pytest.raises(ScenePatchError) as exc:
        apply_scene_patch(
            svg_markup=DRAFT_SVG,
            content_plan=_content_plan(),
            visual_plan=_none_visual(),
            layout_plan=propose_layout_plan(_content_plan()),
            text_edits=[{"node_id": "missing-node", "text": "新文案"}],
        )
    assert exc.value.error_code == "SCENE_NODE_UNKNOWN"


def test_apply_scene_patch_rejects_overlong_label():
    with pytest.raises(ScenePatchError):
        apply_scene_patch(
            svg_markup=DRAFT_SVG,
            content_plan=_content_plan(),
            visual_plan=_none_visual(),
            layout_plan=propose_layout_plan(_content_plan()),
            text_edits=[{"node_id": "block-1-title", "text": "一二三四五六七八九十一二三四五六七八九十超出了"}],
        )


def test_apply_scene_patch_rejects_box_outside_safe_area():
    with pytest.raises(ScenePatchError):
        apply_scene_patch(
            svg_markup=DRAFT_SVG,
            content_plan=_content_plan(),
            visual_plan=_none_visual(),
            layout_plan=propose_layout_plan(_content_plan()),
            box_edits=[{"node_id": "page-title", "box": {"x": 0, "y": 0, "w": 1152, "h": 52}}],
        )


def test_patch_page_scene_creates_new_version_and_marks_design_stale(service, db_session):
    project = make_project(db_session, stage="draft")
    page = make_content_page(db_session, project, page_code="page-03", title="策划页", sort_order=1)
    draft = _seed_draft(db_session, page)
    previous_id = draft.id

    result = service.patch_page_scene(
        project.id,
        page.id,
        {
            "base_version_id": previous_id,
            "text_edits": [{"node_id": "page-title", "text": "行程安排"}],
            "box_edits": [{"node_id": "page-title", "box": {"x": 64, "y": 92, "w": 1152, "h": 52}}],
        },
    )
    db_session.refresh(page)
    assert page.current_draft_version_id != previous_id
    assert page.draft_status == "ready"
    assert page.design_status == "stale"
    assert result["draft"]["content_plan_json"]["title"] == "行程安排"
    assert result["draft"]["layout_plan_json"]["layout_replanned"] is True
    title = next(node for node in result["draft"]["layout_plan_json"]["nodes"] if node["node_id"] == "page-title")
    assert title["box"]["y"] == 92
    previous = db_session.get(DraftVersion, previous_id)
    assert previous is not None
    assert previous.content_plan_json["title"] == "行程预算"


def test_patch_page_scene_rejects_stale_base_version(service, db_session):
    project = make_project(db_session, stage="draft")
    page = make_content_page(db_session, project, page_code="page-03", title="策划页", sort_order=1)
    _seed_draft(db_session, page)
    with pytest.raises(HTTPException) as exc:
        service.patch_page_scene(
            project.id,
            page.id,
            {
                "base_version_id": "stale-version",
                "text_edits": [{"node_id": "page-title", "text": "行程安排"}],
            },
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["error_code"] == "DRAFT_VERSION_CONFLICT"


def test_patch_page_scene_hides_optional_slot_via_api(service, db_session):
    project = make_project(db_session, stage="draft")
    page = make_content_page(db_session, project, page_code="page-03", title="策划页", sort_order=1)
    extra = [
        {
            "node_id": "visual-1",
            "kind": "visual-slot",
            "role": "hero-visual",
            "visual_slot_id": "visual-1",
            "box": {"x": 720, "y": 160, "w": 400, "h": 240},
        }
    ]
    draft = _seed_draft(db_session, page, svg=SLOT_SVG, visual=_optional_visual(), extra_slots=extra)
    result = service.patch_page_scene(
        project.id,
        page.id,
        {
            "base_version_id": draft.id,
            "slot_visibility": [{"slot_id": "visual-1", "visible": False}],
        },
    )
    assert all(node["node_id"] != "visual-1" for node in result["draft"]["layout_plan_json"]["nodes"])
    assert result["draft"]["visual_plan_json"]["slots"][0]["status"] == "skipped"
    assert page.design_status == "stale"


def test_apply_scene_patch_restores_optional_slot():
    content = _content_plan()
    layout = propose_layout_plan(content)
    layout["nodes"].append(
        {
            "node_id": "visual-1",
            "kind": "visual-slot",
            "role": "hero-visual",
            "visual_slot_id": "visual-1",
            "box": {"x": 720, "y": 160, "w": 400, "h": 240},
        }
    )
    hidden = apply_scene_patch(
        svg_markup=SLOT_SVG,
        content_plan=content,
        visual_plan=_optional_visual(),
        layout_plan=layout,
        slot_visibility=[{"slot_id": "visual-1", "visible": False}],
    )
    restored = apply_scene_patch(
        svg_markup=hidden["svg_markup"],
        content_plan=hidden["content_plan"],
        visual_plan=hidden["visual_plan"],
        layout_plan=hidden["layout_plan"],
        slot_visibility=[{"slot_id": "visual-1", "visible": True}],
    )
    assert any(node["node_id"] == "visual-1" for node in restored["layout_plan"]["nodes"])
    assert restored["visual_plan"]["slots"][0]["status"] == "bound"
    assert 'data-node-id="visual-1"' in restored["svg_markup"]


def test_apply_scene_patch_moves_group_children():
    content = _content_plan()
    layout = propose_layout_plan(content)
    group = next(node for node in layout["nodes"] if node["node_id"] == "block-1")
    title = next(node for node in layout["nodes"] if node["node_id"] == "block-1-title")
    old_x = title["box"]["x"]
    result = apply_scene_patch(
        svg_markup=DRAFT_SVG,
        content_plan=content,
        visual_plan=_none_visual(),
        layout_plan=layout,
        box_edits=[{"node_id": "block-1", "box": {**group["box"], "x": group["box"]["x"] + 8}}],
    )
    moved = next(node for node in result["layout_plan"]["nodes"] if node["node_id"] == "block-1-title")
    assert moved["box"]["x"] == old_x + 8


def test_apply_scene_patch_rejects_empty_text():
    with pytest.raises(ScenePatchError):
        apply_scene_patch(
            svg_markup=DRAFT_SVG,
            content_plan=_content_plan(),
            visual_plan=_none_visual(),
            layout_plan=propose_layout_plan(_content_plan()),
            text_edits=[{"node_id": "page-title", "text": "   "}],
        )


def test_apply_scene_patch_infers_text_ref_when_extract_dropped_it():
    content = _content_plan()
    layout = propose_layout_plan(content)
    for node in layout["nodes"]:
        node.pop("text_ref", None)
    result = apply_scene_patch(
        svg_markup=DRAFT_SVG,
        content_plan=content,
        visual_plan=_none_visual(),
        layout_plan=layout,
        text_edits=[{"node_id": "block-1-title", "text": "预约规则"}],
    )
    assert result["content_plan"]["blocks"][0]["label"] == "预约规则"
