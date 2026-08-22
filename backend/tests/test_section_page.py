from __future__ import annotations

from sqlalchemy import select

from app.models.entities import DesignVersion, DraftVersion, OutlineVersion, ProjectPage
from app.services.storyboard import (
    build_storyboard_tree,
    content_page_budget,
    decide_section_strategy,
    enrich_outline_section_pages,
    section_pages_enabled,
    want_section_page,
)
from app.services.tasks import _batch_page_eligible
from tests.helpers import make_project

READY_SVG = '<svg viewBox="0 0 1280 720"><text x="80" y="120">ready</text></svg>'


def _outline_payload(*, parts: list[dict]) -> dict:
    toc = [item["part_title"] for item in parts]
    return {
        "ppt_outline": {
            "cover": {"title": "封面", "content": ["开场"]},
            "table_of_contents": {"title": "目录", "content": toc},
            "parts": parts,
            "end_page": {"title": "谢谢", "content": []},
        }
    }


def _two_part_outline() -> dict:
    return _outline_payload(
        parts=[
            {
                "part_title": "经典六日路线",
                "pages": [
                    {"title": "中轴皇城", "content": ["故宫"]},
                    {"title": "胡同与园林", "content": ["南锣"]},
                ],
            },
            {
                "part_title": "长城与近郊",
                "pages": [{"title": "长城专线", "content": ["八达岭"]}],
            },
        ]
    )


def _seed_outline(service, db_session, project, payload: dict) -> None:
    service._rebuild_pages_from_outline(project, payload)
    outline = OutlineVersion(
        project_id=project.id,
        version_no=1,
        status="ready",
        outline_json=payload,
    )
    db_session.add(outline)
    db_session.commit()


def _ordered_pages(db_session, project_id: str) -> list[ProjectPage]:
    return list(
        db_session.scalars(
            select(ProjectPage).where(ProjectPage.project_id == project_id).order_by(ProjectPage.sort_order.asc())
        )
    )


def _attach_ready(db_session, project, page: ProjectPage) -> None:
    draft = DraftVersion(
        project_id=project.id,
        page_id=page.id,
        version_no=1,
        status="ready",
        draft_svg_markup=READY_SVG,
    )
    design = DesignVersion(
        project_id=project.id,
        page_id=page.id,
        version_no=1,
        status="ready",
        style_pack_id="minimalism",
        design_svg_markup=READY_SVG,
    )
    db_session.add_all([draft, design])
    db_session.flush()
    page.current_draft_version_id = draft.id
    page.current_design_version_id = design.id
    page.draft_status = "ready"
    page.design_status = "ready"
    page.search_status = "ready" if page.page_role == "content" else "confirmed"
    page.summary_status = "ready" if page.page_role == "content" else "confirmed"
    if page.page_role == "content":
        page.page_search_results_json = [
            {
                "id": f"{page.page_code}-src",
                "query_text": page.page_code,
                "query_purpose": "fixture",
                "search_rank": 1,
                "title": page.page_code,
                "url": f"https://example.com/{page.page_code}",
                "bocha_summary": "fixture summary",
                "snippet": "fixture summary",
                "read_status": "ready",
                "chunk_status": "ready",
            }
        ]
        page.page_summary_md = page.page_summary_md or "摘要"


def _content_payloads(listed: list[dict], part: dict) -> list[dict]:
    by_id = {item["page_id"]: item for item in listed}
    return [
        {
            "page_id": item["page_id"],
            "title": by_id[item["page_id"]]["title"],
            "content_outline": by_id[item["page_id"]]["content_outline"],
        }
        for item in part["content_pages"]
    ]


def _section_payload(listed: list[dict], part: dict) -> dict:
    by_id = {item["page_id"]: item for item in listed}
    section_page = part["section_page"]
    if not section_page:
        return {"enabled": False}
    page = by_id[section_page["page_id"]]
    return {
        "enabled": True,
        "page_id": page["page_id"],
        "title": page["title"],
        "preview_items": page["content_outline"],
    }


def _parts_payload(listed: list[dict], tree: dict) -> list[dict]:
    return [
        {
            "part_id": part["part_id"],
            "part_title": part["part_title"],
            "section_page": _section_payload(listed, part),
            "pages": _content_payloads(listed, part),
        }
        for part in tree["sections"]
    ]


def test_compact_strategy_skips_section_pages_when_budget_is_tight():
    assert decide_section_strategy(8, 3) == "compact"
    assert section_pages_enabled(8, 3) is False
    assert content_page_budget(8, 3) == 5


def test_standard_strategy_enables_section_pages_for_typical_deck():
    assert decide_section_strategy(12, 2) == "standard"
    assert section_pages_enabled(12, 2) is True
    assert content_page_budget(12, 2) == 7


def test_enrich_outline_assigns_part_id_and_section_page():
    payload = enrich_outline_section_pages(_two_part_outline(), page_count_target=12)
    parts = payload["ppt_outline"]["parts"]
    assert parts[0]["part_id"] == "part-1"
    assert parts[0]["section_page"]["enabled"] is True
    assert "中轴皇城" in parts[0]["section_page"]["preview_items"]
    assert parts[1]["part_id"] == "part-2"


def test_rebuild_creates_section_pages_in_play_order(service, db_session):
    project = make_project(db_session, stage="outline", page_count_target=12)
    _seed_outline(service, db_session, project, _two_part_outline())
    pages = _ordered_pages(db_session, project.id)
    roles = [page.page_role for page in pages]
    assert roles == ["cover", "toc", "section", "content", "content", "section", "content", "end"]
    assert pages[2].part_id == pages[3].part_id == "part-1"
    assert pages[2].search_status == "confirmed"
    assert pages[2].summary_status == "confirmed"
    assert "中轴皇城" in pages[2].page_summary_md
    serialized = [service.serialize_page(page) for page in pages]
    tree = build_storyboard_tree(serialized)
    assert tree["play_order"] == [page.id for page in pages]
    assert tree["composition"]["label"] == "封面 1 + 目录 1 + 章节过渡 2 + 正文 3 + 收尾 1 = 8"
    assert tree["composition"]["section"] == 2
    assert tree["composition"]["content"] == 3
    assert tree["sections"][0]["section_page"]["page_id"] == pages[2].id
    assert [item["page_id"] for item in tree["sections"][0]["content_pages"]] == [pages[3].id, pages[4].id]


def test_compact_rebuild_does_not_insert_section_pages(service, db_session):
    project = make_project(db_session, stage="outline", page_count_target=8)
    payload = _outline_payload(
        parts=[
            {"part_title": "一", "pages": [{"title": "A", "content": ["a"]}]},
            {"part_title": "二", "pages": [{"title": "B", "content": ["b"]}]},
            {"part_title": "三", "pages": [{"title": "C", "content": ["c"]}]},
        ]
    )
    _seed_outline(service, db_session, project, payload)
    roles = [page.page_role for page in _ordered_pages(db_session, project.id)]
    assert "section" not in roles
    assert roles[0:2] == ["cover", "toc"]
    assert roles[-1] == "end"


def test_storyboard_reorder_keeps_section_before_content(service, db_session):
    project = make_project(db_session, stage="outline", page_count_target=12)
    _seed_outline(service, db_session, project, _two_part_outline())
    listed = service.list_pages(project.id)
    tree = build_storyboard_tree(listed)
    first = tree["sections"][0]
    second = tree["sections"][1]
    result = service.patch_storyboard(
        project.id,
        [
            {
                "part_id": second["part_id"],
                "part_title": "长城与近郊",
                "section_page": {"enabled": True, "page_id": second["section_page"]["page_id"], "title": "长城与近郊"},
                "pages": _content_payloads(listed, second),
            },
            {
                "part_id": first["part_id"],
                "part_title": "经典六日路线",
                "section_page": {"enabled": True, "page_id": first["section_page"]["page_id"], "title": "走进北京的时间轴"},
                "pages": _content_payloads(listed, first),
            },
        ],
    )
    next_pages = result["items"]
    roles = [item["page_role"] for item in next_pages]
    assert roles[:4] == ["cover", "toc", "section", "content"]
    assert next_pages[2]["part_id"] == second["part_id"]
    assert result["storyboard"]["play_order"] == [item["page_id"] for item in next_pages]


def test_section_title_change_does_not_stale_content_pages(service, db_session):
    project = make_project(db_session, stage="draft", page_count_target=12)
    _seed_outline(service, db_session, project, _two_part_outline())
    section = next(page for page in _ordered_pages(db_session, project.id) if page.page_role == "section")
    content = next(
        page for page in _ordered_pages(db_session, project.id) if page.page_role == "content" and page.part_id == section.part_id
    )
    _attach_ready(db_session, project, section)
    _attach_ready(db_session, project, content)
    db_session.commit()

    listed = service.list_pages(project.id)
    tree = build_storyboard_tree(listed)
    part = next(item for item in tree["sections"] if item["part_id"] == section.part_id)
    other = next(item for item in tree["sections"] if item["part_id"] != section.part_id)
    service.patch_storyboard(
        project.id,
        [
            {
                "part_id": part["part_id"],
                "part_title": "经典六日路线",
                "section_page": {
                    "enabled": True,
                    "page_id": part["section_page"]["page_id"],
                    "title": "走进北京的时间轴",
                    "preview_items": ["中轴皇城", "胡同与园林"],
                },
                "pages": _content_payloads(listed, part),
            },
            {
                "part_id": other["part_id"],
                "part_title": other["part_title"],
                "section_page": {
                    "enabled": True,
                    "page_id": other["section_page"]["page_id"],
                    "title": other["section_page"]["title"],
                },
                "pages": _content_payloads(listed, other),
            },
        ],
    )
    db_session.expire_all()
    section = db_session.get(ProjectPage, section.id)
    content = db_session.get(ProjectPage, content.id)
    assert section.draft_status == "stale"
    assert section.design_status == "stale"
    assert content.search_status == "ready"
    assert content.summary_status == "ready"
    assert content.draft_status == "ready"
    assert content.design_status == "ready"


def test_content_title_change_stales_section_not_siblings(service, db_session):
    project = make_project(db_session, stage="draft", page_count_target=12)
    _seed_outline(service, db_session, project, _two_part_outline())
    pages = _ordered_pages(db_session, project.id)
    section = pages[2]
    first_content = pages[3]
    second_content = pages[4]
    _attach_ready(db_session, project, section)
    _attach_ready(db_session, project, first_content)
    _attach_ready(db_session, project, second_content)
    db_session.commit()

    service.patch_page_outline(
        project.id,
        first_content.id,
        {"title": "中轴皇城改名", "content_outline": ["新要点"], "section_title": "经典六日路线"},
    )
    db_session.expire_all()
    section = db_session.get(ProjectPage, section.id)
    first_content = db_session.get(ProjectPage, first_content.id)
    second_content = db_session.get(ProjectPage, second_content.id)
    assert first_content.draft_status == "stale"
    assert second_content.draft_status == "ready"
    assert section.draft_status == "stale"
    assert section.design_status == "stale"
    assert "中轴皇城改名" in section.page_summary_md


def test_batch_search_skips_section_pages(service, db_session):
    project = make_project(db_session, stage="search", page_count_target=12)
    _seed_outline(service, db_session, project, _two_part_outline())
    section = next(page for page in _ordered_pages(db_session, project.id) if page.page_role == "section")
    assert _batch_page_eligible(section, "project_batch_search") is False
    assert _batch_page_eligible(section, "project_batch_summary") is False
    assert _batch_page_eligible(section, "project_batch_draft") is True


def test_want_section_page_treats_null_as_disabled():
    assert want_section_page(None, has_existing=True) is False
    assert want_section_page({"enabled": False}, has_existing=True) is False
    assert want_section_page({"enabled": True}, has_existing=False) is True
    assert want_section_page({"title": "概览"}, has_existing=False) is False
    assert want_section_page({"title": "概览"}, has_existing=True) is True


def test_storyboard_grouping_only_change_commits_without_new_outline(service, db_session):
    project = make_project(db_session, stage="draft", page_count_target=12)
    _seed_outline(service, db_session, project, _two_part_outline())
    listed = service.list_pages(project.id)
    tree = build_storyboard_tree(listed)
    payload = _parts_payload(listed, tree)
    service.patch_storyboard(project.id, payload)
    db_session.expire_all()

    pages = _ordered_pages(db_session, project.id)
    section = next(page for page in pages if page.page_role == "section")
    content_pages = [page for page in pages if page.page_role == "content"]
    _attach_ready(db_session, project, section)
    for page in content_pages:
        _attach_ready(db_session, project, page)
        page.part_title = "旧分组名"
    db_session.commit()

    expected_titles = {page.id: page.part_id for page in content_pages}
    expected_part_titles = {item["page_id"]: item["part_title"] for item in listed if item["page_role"] == "content"}
    outline_version = service._get_current_outline(project.id).version_no
    service.patch_storyboard(project.id, payload)
    db_session.expire_all()

    for page_id, part_title in expected_part_titles.items():
        page = db_session.get(ProjectPage, page_id)
        assert page is not None
        assert page.part_title == part_title
        assert page.part_id == expected_titles[page_id]
        assert page.draft_status == "ready"
    section = db_session.get(ProjectPage, section.id)
    assert section is not None
    assert section.draft_status == "ready"
    assert service._get_current_outline(project.id).version_no == outline_version


def test_compact_storyboard_backfill_part_id_persists(service, db_session):
    project = make_project(db_session, stage="outline", page_count_target=8)
    payload = _outline_payload(
        parts=[
            {"part_title": "一", "pages": [{"title": "A", "content": ["a"]}]},
            {"part_title": "二", "pages": [{"title": "B", "content": ["b"]}]},
            {"part_title": "三", "pages": [{"title": "C", "content": ["c"]}]},
        ]
    )
    _seed_outline(service, db_session, project, payload)
    listed = service.list_pages(project.id)
    tree = build_storyboard_tree(listed)
    patch = _parts_payload(listed, tree)
    service.patch_storyboard(project.id, patch)
    db_session.expire_all()

    content_pages = [page for page in _ordered_pages(db_session, project.id) if page.page_role == "content"]
    expected_part_ids = {page.id: page.part_id for page in content_pages}
    assert all(expected_part_ids.values())
    for page in content_pages:
        page.part_id = None
    db_session.commit()

    outline_version = service._get_current_outline(project.id).version_no
    service.patch_storyboard(project.id, patch)
    db_session.expire_all()

    for page_id, part_id in expected_part_ids.items():
        page = db_session.get(ProjectPage, page_id)
        assert page is not None
        assert page.part_id == part_id
    assert service._get_current_outline(project.id).version_no == outline_version


def test_storyboard_null_section_page_removes_existing_section(service, db_session):
    project = make_project(db_session, stage="outline", page_count_target=12)
    _seed_outline(service, db_session, project, _two_part_outline())
    listed = service.list_pages(project.id)
    tree = build_storyboard_tree(listed)
    first, second = tree["sections"]
    removed_section_id = first["section_page"]["page_id"]
    kept_content_ids = [item["page_id"] for item in first["content_pages"]]

    service.patch_storyboard(
        project.id,
        [
            {
                "part_id": first["part_id"],
                "part_title": first["part_title"],
                "section_page": None,
                "pages": _content_payloads(listed, first),
            },
            {
                "part_id": second["part_id"],
                "part_title": second["part_title"],
                "section_page": _section_payload(listed, second),
                "pages": _content_payloads(listed, second),
            },
        ],
    )
    db_session.expire_all()
    assert db_session.get(ProjectPage, removed_section_id) is None
    remaining = _ordered_pages(db_session, project.id)
    remaining_ids = {page.id for page in remaining}
    assert set(kept_content_ids) <= remaining_ids
    assert not any(page.page_role == "section" and page.part_id == first["part_id"] for page in remaining)
    assert any(page.page_role == "section" and page.part_id == second["part_id"] for page in remaining)


def test_export_order_matches_storyboard_play_order(service, db_session):
    project = make_project(db_session, stage="design", page_count_target=12)
    _seed_outline(service, db_session, project, _two_part_outline())
    pages = _ordered_pages(db_session, project.id)
    for page in pages:
        design = DesignVersion(
            project_id=project.id,
            page_id=page.id,
            version_no=1,
            status="ready",
            style_pack_id="minimalism",
            design_svg_markup=READY_SVG,
        )
        db_session.add(design)
        db_session.flush()
        page.current_design_version_id = design.id
        page.design_status = "ready"
    db_session.commit()
    exportables = service._collect_exportable_designs(project.id)
    play_order = build_storyboard_tree(service.list_pages(project.id))["play_order"]
    assert [page.id for page, _design in exportables] == play_order
    assert any(page.page_role == "section" for page, _design in exportables)
