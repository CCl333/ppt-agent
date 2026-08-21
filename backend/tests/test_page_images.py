from __future__ import annotations

from pathlib import Path

import pytest

from app.services.page_images import (
    assert_svg_uses_images,
    extract_image_candidates,
    materialize_page_images,
    resolve_image_refs,
)
from app.services.svg_contract import SvgContractError, validate_svg_contract
from tests.helpers import PNG_1X1, make_content_page, make_project


def test_extract_image_candidates_from_search_fields():
    candidates = extract_image_candidates(
        [
            {
                "title": "故宫",
                "url": "https://example.com/gugong",
                "image_url": "https://cdn.example.com/gugong.jpg",
            },
            {
                "title": "直链图片",
                "url": "https://cdn.example.com/photo.png",
            },
            {
                "title": "重复",
                "url": "https://example.com/other",
                "extra_images": ["https://cdn.example.com/gugong.jpg"],
            },
        ]
    )
    urls = [item["url"] for item in candidates]
    assert urls == ["https://cdn.example.com/gugong.jpg", "https://cdn.example.com/photo.png"]


def test_materialize_page_images_writes_files():
    def fake_fetch(url: str):
        assert url == "https://cdn.example.com/gugong.jpg"
        return "image/png", PNG_1X1

    catalog = materialize_page_images(
        project_id="proj",
        page_id="page",
        search_results=[{"title": "故宫", "image_url": "https://cdn.example.com/gugong.jpg"}],
        fetch=fake_fetch,
    )
    assert catalog[0]["image_id"] == "IMG-1"
    assert Path(catalog[0]["storage_path"]).read_bytes() == PNG_1X1


def test_resolve_and_assert_image_refs():
    catalog = materialize_page_images(
        project_id="proj",
        page_id="page",
        search_results=[{"title": "故宫", "image_url": "https://cdn.example.com/gugong.jpg"}],
        fetch=lambda _url: ("image/png", PNG_1X1),
    )
    svg = '<svg viewBox="0 0 1280 720"><image data-image-id="IMG-1" x="40" y="80" width="400" height="240"/></svg>'
    validate_svg_contract(svg, stage="draft")
    resolved = resolve_image_refs(svg, catalog)
    assert "data:image/png;base64," in resolved
    assert_svg_uses_images(resolved, catalog, [{"image_id": "IMG-1"}])


def test_draft_does_not_require_images_when_catalog_exists():
    catalog = [{"image_id": "IMG-1"}]
    svg = """
    <svg viewBox="0 0 1280 720">
      <g data-image-slot-id="IMG-1">
        <rect x="40" y="80" width="400" height="240" fill="none" stroke="#999" stroke-dasharray="6 4"/>
        <text x="50" y="200">景点现场示意图</text>
      </g>
    </svg>
    """
    assert_svg_uses_images(svg, catalog, [{"image_id": "IMG-1"}], stage="draft")


def test_design_requires_slotted_images():
    catalog = [{"image_id": "IMG-1"}]
    empty = '<svg viewBox="0 0 1280 720"><rect width="10" height="10"/></svg>'
    with pytest.raises(RuntimeError, match="未出现"):
        assert_svg_uses_images(empty, catalog, [{"image_id": "IMG-1"}], stage="design")
    assert_svg_uses_images(empty, catalog, [], stage="design")


def test_unknown_image_id_is_hard_fail():
    svg = '<svg viewBox="0 0 1280 720"><image data-image-id="IMG-9" x="0" y="0" width="10" height="10"/></svg>'
    with pytest.raises(RuntimeError, match="不存在的配图"):
        resolve_image_refs(svg, [])


def test_http_image_href_fails_contract():
    svg = '<svg viewBox="0 0 1280 720"><image href="https://cdn.example.com/a.jpg" x="0" y="0" width="10" height="10"/></svg>'
    with pytest.raises(SvgContractError, match="data-image-id"):
        validate_svg_contract(svg, stage="draft")


def test_list_events_returns_stream(service, db_session):
    project = make_project(db_session, stage="init")
    from app.services.events import append_event

    append_event(
        db_session,
        project_id=project.id,
        event_type="agent.run.started",
        stage="init",
        scope_type="project",
        payload={"title": "初始化"},
        agent_run_id="run-1",
    )
    db_session.commit()
    payload = service.list_events(project.id)
    assert payload["items"][0]["event_type"] == "agent.run.started"
    assert payload["items"][0]["payload"]["title"] == "初始化"


def test_serialize_page_includes_image_catalog(service, db_session):
    project = make_project(db_session, stage="search")
    page = make_content_page(db_session, project, page_code="page-03", title="故宫", sort_order=1)
    page.page_images_json = materialize_page_images(
        project_id=project.id,
        page_id=page.id,
        search_results=[{"title": "故宫", "image_url": "https://cdn.example.com/gugong.jpg"}],
        fetch=lambda _url: ("image/png", PNG_1X1),
    )
    db_session.commit()
    payload = service.serialize_page(page)
    assert payload["page_images"][0]["image_id"] == "IMG-1"
    assert payload["page_images"][0]["available"] == "true"
    assert "storage_path" not in payload["page_images"][0]
