from __future__ import annotations

import io
import zipfile
from pathlib import Path

from app.services.export import build_pptx, rasterize_svg_to_png
from tests.helpers import make_content_page, make_project

SAMPLE_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" width="1280" height="720">
  <rect x="80" y="80" width="520" height="240" fill="#1d4ed8"/>
  <text x="96" y="160" font-size="48" fill="#ffffff">导出测试</text>
</svg>
""".strip()


def test_rasterize_svg_to_png_is_real_png():
    png = rasterize_svg_to_png(SAMPLE_SVG)
    assert png.startswith(b"\x89PNG")
    assert len(png) > 32


def test_build_pptx_default_writes_native_shapes(tmp_path: Path):
    export_path = tmp_path / "deck.pptx"
    build_pptx(
        [("page-01", SAMPLE_SVG), ("page-02", SAMPLE_SVG.replace("导出测试", "第二页"))],
        export_path,
    )
    assert export_path.is_file()
    with zipfile.ZipFile(export_path) as archive:
        slides = sorted(
            [name for name in archive.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml")]
        )
        assert len(slides) == 2
        xml = archive.read(slides[0]).decode("utf-8")
        assert "<a:t>导出测试</a:t>" in xml
        assert "svgBlip" not in xml


def test_create_export_pptx_via_api(client, db_session):
    project = make_project(db_session, stage="design")
    project.title = "企业落地大模型实践路径"
    page = make_content_page(db_session, project, page_code="p1", title="企业落地大模型实践路径", sort_order=1)
    from app.models.entities import DesignVersion

    design = db_session.get(DesignVersion, page.current_design_version_id)
    design.design_svg_markup = SAMPLE_SVG
    db_session.commit()

    response = client.post(f"/api/v1/projects/{project.id}/exports", json={"export_format": "pptx"})
    assert response.status_code == 200, response.text
    payload = response.json()
    export_id = payload["export_id"]
    assert "企业落地大模型实践路径.pptx" in payload["file_path"].replace("\\", "/")
    download = client.get(f"/api/v1/projects/{project.id}/exports/{export_id}/download")
    assert download.status_code == 200
    disposition = download.headers.get("content-disposition", "")
    assert "实践路径.pptx" in disposition or "utf-8''" in disposition
    body = download.content
    assert body[:2] == b"PK"
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        slide_xml = next(name for name in archive.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml"))
        xml = archive.read(slide_xml).decode("utf-8")
        assert "<a:t>导出测试</a:t>" in xml
        presentation = archive.read("ppt/presentation.xml").decode("utf-8")
        assert 'cx="12192000"' in presentation


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
