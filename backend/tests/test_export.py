from __future__ import annotations

import io
import zipfile
from pathlib import Path

from app.services.export import build_pptx, rasterize_svg_to_png
from tests.helpers import make_content_page, make_project

SAMPLE_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" width="1280" height="720">
  <rect width="1280" height="720" fill="#1d4ed8"/>
  <text x="96" y="160" font-size="48" fill="#ffffff">导出测试</text>
</svg>
""".strip()


def test_rasterize_svg_to_png_is_real_png():
    png = rasterize_svg_to_png(SAMPLE_SVG)
    assert png.startswith(b"\x89PNG")
    assert len(png) > 32


def test_build_pptx_writes_png_blip_and_svg_blip(tmp_path: Path):
    export_path = tmp_path / "deck.pptx"
    build_pptx(
        [("page-01", SAMPLE_SVG), ("page-02", SAMPLE_SVG.replace("导出测试", "第二页"))],
        export_path,
    )
    assert export_path.is_file()
    with zipfile.ZipFile(export_path) as archive:
        names = archive.namelist()
        pngs = [name for name in names if name.startswith("ppt/media/") and name.endswith(".png")]
        svgs = [name for name in names if name.startswith("ppt/media/") and name.endswith(".svg")]
        slides = sorted(
            [name for name in names if name.startswith("ppt/slides/slide") and name.endswith(".xml")]
        )
        content_types = archive.read("[Content_Types].xml").decode("utf-8")
        assert len(pngs) == 2
        assert len(svgs) == 2
        assert len(slides) == 2
        assert 'Extension="svg"' in content_types
        svg_blip_count = 0
        png_blip_count = 0
        for slide_name in slides:
            xml = archive.read(slide_name).decode("utf-8")
            svg_blip_count += xml.count("svgBlip")
            png_blip_count += xml.count("blip")
        assert svg_blip_count == 2
        assert png_blip_count >= 2


def test_create_export_pptx_via_api(client, db_session):
    project = make_project(db_session, stage="design")
    page = make_content_page(db_session, project, page_code="p1", title="封面", sort_order=1)
    from app.models.entities import DesignVersion

    design = db_session.get(DesignVersion, page.current_design_version_id)
    design.design_svg_markup = SAMPLE_SVG
    db_session.commit()

    response = client.post(f"/api/v1/projects/{project.id}/exports", json={"export_format": "pptx"})
    assert response.status_code == 200, response.text
    export_id = response.json()["export_id"]
    download = client.get(f"/api/v1/projects/{project.id}/exports/{export_id}/download")
    assert download.status_code == 200
    payload = download.content
    assert payload[:2] == b"PK"
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        slide_xml = next(name for name in archive.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml"))
        xml = archive.read(slide_xml).decode("utf-8")
        assert "svgBlip" in xml
        assert any(name.endswith(".png") for name in archive.namelist())
        assert any(name.endswith(".svg") for name in archive.namelist())


def test_export_rejects_incomplete_design(client, db_session):
    project = make_project(db_session, stage="design")
    make_content_page(
        db_session,
        project,
        page_code="p1",
        title="未完成",
        sort_order=1,
        with_design=False,
    )
    response = client.post(f"/api/v1/projects/{project.id}/exports", json={"export_format": "pptx"})
    assert response.status_code == 422
