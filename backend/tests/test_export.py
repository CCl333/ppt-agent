from __future__ import annotations

import io
import zipfile
from pathlib import Path

from app.services.export import build_pptx, rasterize_svg_to_png
from tests.helpers import drain_tasks, make_content_page, make_project

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
    assert response.status_code == 202, response.text
    payload = response.json()
    export_id = payload["export_id"]
    assert payload["status"] == "queued"
    drain_tasks()
    completed = client.get(f"/api/v1/projects/{project.id}/exports/{export_id}")
    assert completed.status_code == 200
    payload = completed.json()
    assert payload["status"] == "completed"
    assert "企业落地大模型实践路径" in payload["file_path"].replace("\\", "/")
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
    assert response.status_code == 202, response.text
    export_id = response.json()["export_id"]
    drain_tasks()
    status = client.get(f"/api/v1/projects/{project.id}/exports/{export_id}")
    assert status.status_code == 200
    detail = status.json()
    assert detail["status"] == "failed"
    assert detail.get("error_code")
    download = client.get(f"/api/v1/projects/{project.id}/exports/{export_id}/download")
    assert download.status_code == 409


def test_export_unknown_element_creates_failed_job_without_image_fallback(client, db_session):
    from app.models.entities import DesignVersion, ExportJob

    project = make_project(db_session, stage="design")
    page = make_content_page(db_session, project, page_code="p1", title="坏页", sort_order=1)
    design = db_session.get(DesignVersion, page.current_design_version_id)
    design.design_svg_markup = '<svg viewBox="0 0 1280 720"><foo/></svg>'
    db_session.commit()

    response = client.post(f"/api/v1/projects/{project.id}/exports", json={"export_format": "pptx"})
    assert response.status_code == 202, response.text
    export_id = response.json()["export_id"]
    drain_tasks()
    db_session.expire_all()
    job = db_session.get(ExportJob, export_id)
    assert job is not None
    assert job.status == "failed"
    assert job.export_format == "pptx"
    assert job.render_mode == "shapes"
    assert job.error_code in {"EXPORT_SHAPES_FAILED", "EXPORT_SHAPES_PREFLIGHT", "UNCLASSIFIED"}
    assert "image" not in str(job.error_detail_json.get("message") or "").lower() or "pptx-image" not in str(job.error_detail_json).lower()
    download = client.get(f"/api/v1/projects/{project.id}/exports/{export_id}/download")
    assert download.status_code == 409


def test_page_quality_endpoint_returns_reports(client, db_session):
    project = make_project(db_session, stage="design")
    page = make_content_page(db_session, project, page_code="p1", title="质量页", sort_order=1)
    response = client.get(f"/api/v1/projects/{project.id}/pages/{page.id}/quality")
    assert response.status_code == 200
    payload = response.json()
    assert payload["page_id"] == page.id
    assert "draft" in payload
    assert "design" in payload
