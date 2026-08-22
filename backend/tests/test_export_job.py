from __future__ import annotations

import io
import zipfile
from pathlib import Path

from app.models.entities import DesignVersion, ExportJob
from tests.helpers import drain_tasks, make_content_page, make_project

SAMPLE_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" width="1280" height="720">
  <rect x="80" y="80" width="520" height="240" fill="#1d4ed8"/>
  <text x="96" y="160" font-size="48" fill="#ffffff">导出测试</text>
</svg>
""".strip()

CHANGED_SVG = SAMPLE_SVG.replace("导出测试", "新版本")


def test_export_job_is_async_and_writes_checksum_manifest(client, db_session):
    project = make_project(db_session, stage="design")
    page = make_content_page(db_session, project, page_code="p1", title="企业落地大模型实践路径", sort_order=1)
    design = db_session.get(DesignVersion, page.current_design_version_id)
    design.design_svg_markup = SAMPLE_SVG
    db_session.commit()

    response = client.post(f"/api/v1/projects/{project.id}/exports", json={"export_format": "pptx"})
    assert response.status_code == 202, response.text
    payload = response.json()
    export_id = payload["export_id"]
    assert payload["status"] == "queued"
    assert payload["phase"] == "snapshot"
    assert payload["input_hash"]
    assert payload["download_ready"] is False

    drain_tasks()
    status = client.get(f"/api/v1/projects/{project.id}/exports/{export_id}")
    assert status.status_code == 200
    job = status.json()
    assert job["status"] == "completed"
    assert job["phase"] == "publish"
    assert job["file_sha256"]
    assert job["file_size"] > 0
    assert job["slide_count"] == 1
    assert job["manifest"]["pages"][0]["design_version_id"] == page.current_design_version_id
    assert job["download_ready"] is True
    download = client.get(f"/api/v1/projects/{project.id}/exports/{export_id}/download")
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert "ppt/presentation.xml" in archive.namelist()
        xml = archive.read("ppt/slides/slide1.xml").decode("utf-8")
        assert "<a:t>导出测试</a:t>" in xml
        assert "svgBlip" not in xml


def test_export_snapshot_freezes_input_and_flags_stale(client, db_session):
    project = make_project(db_session, stage="design")
    page = make_content_page(db_session, project, page_code="p1", title="冻结页", sort_order=1)
    design = db_session.get(DesignVersion, page.current_design_version_id)
    design.design_svg_markup = SAMPLE_SVG
    db_session.commit()

    response = client.post(f"/api/v1/projects/{project.id}/exports", json={"export_format": "pptx"})
    export_id = response.json()["export_id"]
    frozen_hash = response.json()["input_hash"]
    frozen_design_id = page.current_design_version_id
    newer = DesignVersion(
        project_id=project.id,
        page_id=page.id,
        version_no=2,
        status="ready",
        design_svg_markup=CHANGED_SVG,
    )
    db_session.add(newer)
    db_session.flush()
    page.current_design_version_id = newer.id
    db_session.commit()

    drain_tasks()
    job = client.get(f"/api/v1/projects/{project.id}/exports/{export_id}").json()
    assert job["status"] == "completed"
    assert job["input_hash"] == frozen_hash
    assert job["input_stale"] is True
    assert job["input_snapshot"]["pages"][0]["design_version_id"] == frozen_design_id
    download = client.get(f"/api/v1/projects/{project.id}/exports/{export_id}/download")
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        xml = archive.read("ppt/slides/slide1.xml").decode("utf-8")
        assert "<a:t>导出测试</a:t>" in xml
        assert "新版本" not in xml


def test_failed_export_does_not_overwrite_completed_file(client, db_session, tmp_path: Path):
    project = make_project(db_session, stage="design")
    page = make_content_page(db_session, project, page_code="p1", title="覆盖保护", sort_order=1)
    design = db_session.get(DesignVersion, page.current_design_version_id)
    design.design_svg_markup = SAMPLE_SVG
    db_session.commit()

    first = client.post(f"/api/v1/projects/{project.id}/exports", json={"export_format": "pptx"}).json()
    drain_tasks()
    first_job = client.get(f"/api/v1/projects/{project.id}/exports/{first['export_id']}").json()
    first_path = Path(first_job["file_path"])
    assert first_path.is_file()
    first_bytes = first_path.read_bytes()

    design.design_svg_markup = '<svg viewBox="0 0 1280 720"><foo/></svg>'
    db_session.commit()
    second = client.post(f"/api/v1/projects/{project.id}/exports", json={"export_format": "pptx"}).json()
    drain_tasks()
    second_job = client.get(f"/api/v1/projects/{project.id}/exports/{second['export_id']}").json()
    assert second_job["status"] == "failed"
    assert second_job["error_code"] in {"EXPORT_SHAPES_PREFLIGHT", "EXPORT_SHAPES_FAILED", "UNCLASSIFIED"}
    assert first_path.is_file()
    assert first_path.read_bytes() == first_bytes
    assert not second_job["file_path"] or second_job["file_path"] != first_job["file_path"]
    download = client.get(f"/api/v1/projects/{project.id}/exports/{first['export_id']}/download")
    assert download.status_code == 200


def test_idempotency_key_returns_same_in_progress_job(client, db_session):
    project = make_project(db_session, stage="design")
    page = make_content_page(db_session, project, page_code="p1", title="幂等", sort_order=1)
    design = db_session.get(DesignVersion, page.current_design_version_id)
    design.design_svg_markup = SAMPLE_SVG
    db_session.commit()

    first = client.post(
        f"/api/v1/projects/{project.id}/exports",
        json={"export_format": "pptx", "idempotency_key": "client-key-1"},
    )
    second = client.post(
        f"/api/v1/projects/{project.id}/exports",
        json={"export_format": "pptx", "idempotency_key": "client-key-1"},
    )
    assert first.json()["export_id"] == second.json()["export_id"]
    drain_tasks()


def test_failed_export_task_marks_running_job_failed(db_session):
    from app.services.export_job import build_export_snapshot, create_export_job
    from app.services.tasks import STATUS_FAILED, _finish_task, enqueue_export_job

    project = make_project(db_session, stage="design")
    page = make_content_page(db_session, project, page_code="p1", title="worker失败", sort_order=1)
    design = db_session.get(DesignVersion, page.current_design_version_id)
    design.design_svg_markup = SAMPLE_SVG
    db_session.commit()
    snapshot = build_export_snapshot(db_session, project)
    job = create_export_job(db_session, project, export_format="pptx", render_mode="shapes", snapshot=snapshot)
    job.status = "running"
    task = enqueue_export_job(db_session, project_id=project.id, export_id=job.id)
    _finish_task(db_session, task, STATUS_FAILED, "lease expired, retries exhausted")
    db_session.flush()
    assert job.status == "failed"
    assert job.error_code == "EXPORT_WORKER_FAILED"


def test_temp_file_cleaned_on_failure(client, db_session):
    project = make_project(db_session, stage="design")
    page = make_content_page(db_session, project, page_code="p1", title="临时文件", sort_order=1)
    design = db_session.get(DesignVersion, page.current_design_version_id)
    design.design_svg_markup = '<svg viewBox="0 0 1280 720"><foo/></svg>'
    db_session.commit()
    payload = client.post(f"/api/v1/projects/{project.id}/exports", json={"export_format": "pptx"}).json()
    drain_tasks()
    db_session.expire_all()
    job = db_session.get(ExportJob, payload["export_id"])
    assert job.status == "failed"
    assert not job.temp_path
    export_dir = Path(job.file_path).parent if job.file_path else None
    if export_dir and export_dir.is_dir():
        assert not list(export_dir.glob(f".{job.id}.tmp*"))
