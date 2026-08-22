from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.base import now_utc
from app.models.entities import DesignVersion, ExportJob, Project, ProjectPage
from app.services.error_policy import classify_exception
from app.services.events import append_event
from app.services.export import build_pptx, verify_pptx_package
from app.services.export_name import resolve_export_stem
from app.services.export_preflight import preflight_shapes_slide
from app.services.font_policy import build_font_report
from app.services.quality_report import hash_svg
from app.services.runtime import EXPORTER_VERSION, SVG_CONTRACT_VERSION, build_fingerprint

ACTIVE_EXPORT_STATUSES = ("queued", "running")
RENDER_MODE_BY_FORMAT = {
    "pptx": "shapes",
    "pptx-image": "image",
    "zip": "svg",
}


def resolve_render_mode(export_format: str, render_mode: str | None = None) -> str:
    if export_format not in RENDER_MODE_BY_FORMAT:
        raise HTTPException(status_code=400, detail="当前仅支持 zip、pptx 或 pptx-image 导出")
    expected = RENDER_MODE_BY_FORMAT[export_format]
    if render_mode and render_mode != expected:
        raise HTTPException(status_code=400, detail=f"{export_format} 的 render_mode 必须是 {expected}")
    return expected


def canonical_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def build_export_snapshot(session: Session, project: Project) -> dict[str, Any]:
    pages = list(
        session.scalars(select(ProjectPage).where(ProjectPage.project_id == project.id).order_by(ProjectPage.sort_order.asc()))
    )
    page_entries: list[dict[str, Any]] = []
    for page in pages:
        design = session.get(DesignVersion, page.current_design_version_id) if page.current_design_version_id else None
        markup = design.design_svg_markup if design else ""
        page_entries.append(
            {
                "page_id": page.id,
                "page_code": page.page_code,
                "sort_order": page.sort_order,
                "page_role": page.page_role,
                "design_status": page.design_status,
                "design_version_id": page.current_design_version_id,
                "draft_version_id": page.current_draft_version_id,
                "style_pack_id": design.style_pack_id if design else project.style_preset,
                "svg_hash": design.svg_hash or hash_svg(markup) if design else None,
            }
        )
    snapshot = {
        "project_id": project.id,
        "project_stage": project.current_stage,
        "pages": page_entries,
        "contract_version": SVG_CONTRACT_VERSION,
        "exporter_version": EXPORTER_VERSION,
        "build_fingerprint": build_fingerprint(),
    }
    snapshot["input_hash"] = canonical_hash(
        {
            "project_id": project.id,
            "pages": [
                {
                    "page_id": item["page_id"],
                    "design_version_id": item["design_version_id"],
                    "svg_hash": item["svg_hash"],
                    "sort_order": item["sort_order"],
                }
                for item in page_entries
            ],
        }
    )
    return snapshot


def find_active_export(
    session: Session,
    *,
    project_id: str,
    export_format: str,
    input_hash: str,
    idempotency_key: str | None,
) -> ExportJob | None:
    if idempotency_key:
        existing = session.scalars(
            select(ExportJob)
            .where(
                ExportJob.project_id == project_id,
                ExportJob.idempotency_key == idempotency_key,
                ExportJob.status.in_(ACTIVE_EXPORT_STATUSES),
            )
            .order_by(ExportJob.created_at.desc())
        ).first()
        if existing:
            return existing
    return session.scalars(
        select(ExportJob)
        .where(
            ExportJob.project_id == project_id,
            ExportJob.export_format == export_format,
            ExportJob.input_hash == input_hash,
            ExportJob.status.in_(ACTIVE_EXPORT_STATUSES),
        )
        .order_by(ExportJob.created_at.desc())
    ).first()


def create_export_job(
    session: Session,
    project: Project,
    *,
    export_format: str,
    render_mode: str,
    snapshot: dict[str, Any],
    idempotency_key: str | None = None,
) -> ExportJob:
    job = ExportJob(
        project_id=project.id,
        export_format=export_format,
        render_mode=render_mode,
        status="queued",
        phase="snapshot",
        progress_json={"current": 0, "total": len(snapshot.get("pages") or [])},
        input_snapshot_json=snapshot,
        input_hash=str(snapshot.get("input_hash") or ""),
        build_fingerprint_json=build_fingerprint(),
        idempotency_key=idempotency_key,
        attempt=0,
    )
    session.add(job)
    session.flush()
    append_event(
        session,
        project_id=project.id,
        event_type="export.queued",
        stage="export",
        scope_type="project",
        payload={"export_id": job.id, "export_format": export_format, "input_hash": job.input_hash},
    )
    return job


def execute_export_job(session: Session, job: ExportJob, *, export_root: Path, outline_json: dict[str, Any] | None, first_page_title: str | None, project_title: str) -> None:
    if not _claim_export_running(session, job):
        return
    _emit(session, job, "export.updated", extra={"phase": job.phase})
    try:
        pages = _validate_snapshot(session, job)
        if job.export_format in {"pptx", "pptx-image"}:
            _run_preflight(session, job, pages)
        if not _export_still_active(session, job):
            _cleanup_temp(job)
            return
        output_path = _build_and_publish(
            session,
            job,
            pages,
            export_root=export_root,
            outline_json=outline_json,
            first_page_title=first_page_title,
            project_title=project_title,
        )
        if not _export_still_active(session, job):
            _cleanup_temp(job)
            if output_path.exists() and str(output_path) != (job.file_path or ""):
                output_path.unlink(missing_ok=True)
            return
        job.status = "completed"
        job.phase = "publish"
        job.file_path = str(output_path)
        job.finished_at = now_utc()
        job.error_code = None
        _set_progress(job, current=len(pages), total=len(pages))
        session.flush()
        _emit(session, job, "export.completed", extra={"file_sha256": job.file_sha256, "slide_count": job.slide_count})
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else classify_exception(RuntimeError(str(exc.detail)))
        if isinstance(detail, str):
            detail = classify_exception(RuntimeError(detail))
        fail_export_job(session, job, detail)
    except Exception as exc:
        fail_export_job(session, job, classify_exception(exc))


def fail_export_job(session: Session, job: ExportJob, detail: dict[str, Any], *, status: str = "failed") -> None:
    if job.status in {"completed", "failed", "canceled"}:
        return
    payload = dict(detail)
    payload["export_id"] = job.id
    _cleanup_temp(job)
    job.status = status
    job.error_code = str(payload.get("error_code") or "EXPORT_FAILED")
    job.error_detail_json = payload
    job.finished_at = now_utc()
    job.file_path = job.file_path or ""
    session.flush()
    _emit(session, job, "export.failed" if status == "failed" else "export.updated", extra={"error_code": job.error_code})


def current_input_is_stale(session: Session, project: Project, job: ExportJob) -> bool:
    current = build_export_snapshot(session, project)
    return str(current.get("input_hash") or "") != str(job.input_hash or "")


def serialize_export_job(session: Session, job: ExportJob, *, project: Project | None = None) -> dict[str, Any]:
    project = project or session.get(Project, job.project_id)
    stale = current_input_is_stale(session, project, job) if project else False
    download_ready = job.status == "completed" and bool(job.file_path) and Path(job.file_path).is_file()
    return {
        "export_id": job.id,
        "project_id": job.project_id,
        "export_format": job.export_format,
        "render_mode": job.render_mode,
        "status": job.status,
        "phase": job.phase,
        "progress": job.progress_json or {},
        "input_hash": job.input_hash,
        "input_snapshot": job.input_snapshot_json or {},
        "input_stale": stale,
        "file_path": job.file_path,
        "file_sha256": job.file_sha256,
        "file_size": job.file_size,
        "slide_count": job.slide_count,
        "manifest": job.manifest_json or {},
        "font_report": job.font_report_json or {},
        "error_code": job.error_code,
        "error_detail": job.error_detail_json or {},
        "build_fingerprint": job.build_fingerprint_json or {},
        "download_ready": download_ready,
        "attempt": job.attempt,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


def resolve_download_path(job: ExportJob) -> str:
    if job.status != "completed" or not job.file_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": job.error_code or "EXPORT_NOT_READY",
                "category": "quality_violation",
                "message": "导出尚未完成或已失败，不能下载",
                "retryability": "after_relayout",
                "export_id": job.id,
                "violations": (job.error_detail_json or {}).get("violations") or [],
            },
        )
    path = Path(job.file_path)
    if not path.is_file():
        job.status = "failed"
        job.error_code = "ARTIFACT_MISSING"
        job.error_detail_json = {
            "error_code": "ARTIFACT_MISSING",
            "category": "transient_infra",
            "message": "导出文件已丢失，不能返回空文件",
            "retryability": "retry_now",
            "export_id": job.id,
        }
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=job.error_detail_json)
    if job.file_sha256:
        actual = file_sha256(path)
        if actual != job.file_sha256:
            job.status = "failed"
            job.error_code = "ARTIFACT_CHECKSUM_MISMATCH"
            job.error_detail_json = {
                "error_code": "ARTIFACT_CHECKSUM_MISMATCH",
                "category": "transient_infra",
                "message": "导出文件校验失败",
                "retryability": "retry_now",
                "export_id": job.id,
            }
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=job.error_detail_json)
    return str(path)


def _claim_export_running(session: Session, job: ExportJob) -> bool:
    if job.status in {"completed", "failed", "canceled"}:
        return False
    now = now_utc()
    result = session.execute(
        update(ExportJob)
        .where(ExportJob.id == job.id, ExportJob.status == "queued")
        .values(status="running", started_at=job.started_at or now, attempt=int(job.attempt or 0) + 1, updated_at=now)
    )
    if result.rowcount == 1:
        session.refresh(job)
        return True
    session.refresh(job)
    if job.status != "running":
        return False
    job.attempt = int(job.attempt or 0) + 1
    job.updated_at = now
    session.flush()
    return True


def _export_still_active(session: Session, job: ExportJob) -> bool:
    session.flush()
    current = session.scalar(select(ExportJob.status).where(ExportJob.id == job.id))
    return current in {"queued", "running"}


def _validate_snapshot(session: Session, job: ExportJob) -> list[dict[str, Any]]:
    job.phase = "snapshot"
    session.flush()
    pages = list((job.input_snapshot_json or {}).get("pages") or [])
    if not pages:
        raise RuntimeError("导出快照为空")
    missing: list[str] = []
    resolved: list[dict[str, Any]] = []
    for index, entry in enumerate(pages, start=1):
        design = session.get(DesignVersion, entry.get("design_version_id")) if entry.get("design_version_id") else None
        markup = (design.design_svg_markup if design else "") or ""
        if entry.get("design_status") != "ready" or design is None or not markup.strip():
            missing.append(f"{index}. {entry.get('page_code') or entry.get('page_id')}")
            continue
        actual_hash = design.svg_hash or hash_svg(markup)
        if entry.get("svg_hash") and actual_hash != entry.get("svg_hash"):
            raise RuntimeError(f"页面 {entry.get('page_code')} 的快照 hash 与设计版本不一致")
        resolved.append({**entry, "svg_markup": markup, "design": design})
        _set_progress(job, current=index, total=len(pages))
    if missing:
        detail = classify_exception(RuntimeError("以下页面尚未完成设计稿，无法导出：" + "；".join(missing)))
        detail["error_code"] = "EXPORT_INPUT_INCOMPLETE"
        detail["violations"] = [{"code": "EXPORT_INPUT_INCOMPLETE", "detail": item} for item in missing]
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)
    return resolved


def _run_preflight(session: Session, job: ExportJob, pages: list[dict[str, Any]]) -> None:
    job.phase = "preflight"
    session.flush()
    if job.render_mode != "shapes":
        return
    for index, entry in enumerate(pages, start=1):
        result = preflight_shapes_slide(entry["svg_markup"])
        entry["preflight"] = result
        _set_progress(job, current=index, total=len(pages))
        if result.get("status") != "pass":
            detail = {
                "error_code": "EXPORT_SHAPES_PREFLIGHT",
                "category": "contract_violation",
                "message": f"页面 {entry.get('page_code')} native shapes 预检失败: {result.get('detail')}",
                "retryability": "not_retryable",
                "page_id": entry.get("page_id"),
                "violations": result.get("violations") or [],
            }
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _build_and_publish(
    session: Session,
    job: ExportJob,
    pages: list[dict[str, Any]],
    *,
    export_root: Path,
    outline_json: dict[str, Any] | None,
    first_page_title: str | None,
    project_title: str,
) -> Path:
    job.phase = "build"
    session.flush()
    stem = resolve_export_stem(outline_json=outline_json, first_page_title=first_page_title, project_title=project_title)
    suffix = {"zip": ".zip", "pptx": ".pptx", "pptx-image": ".pptx"}[job.export_format]
    export_dir = export_root / job.project_id
    export_dir.mkdir(parents=True, exist_ok=True)
    temp_path = export_dir / f".{job.id}.tmp{suffix}"
    final_path = export_dir / f"{stem}-{job.id[:8]}{suffix}"
    job.temp_path = str(temp_path)
    session.flush()
    slides = [(str(item["page_code"]), str(item["svg_markup"])) for item in pages]
    if job.export_format == "zip":
        _write_zip(temp_path, pages)
    else:
        mode = "image" if job.render_mode == "image" else "shapes"
        build_pptx(slides, temp_path, mode=mode)
    job.phase = "verify"
    session.flush()
    if job.export_format == "zip":
        verify_zip_package(temp_path, expected_count=len(pages))
    else:
        verify_pptx_package(temp_path, render_mode=str(job.render_mode or "shapes"), expected_slide_count=len(pages))
    checksum = file_sha256(temp_path)
    size = temp_path.stat().st_size
    manifest = {
        "export_id": job.id,
        "export_format": job.export_format,
        "render_mode": job.render_mode,
        "input_hash": job.input_hash,
        "file_sha256": checksum,
        "slide_count": len(pages),
        "download_name": f"{stem}{suffix}",
        "editable": job.render_mode == "shapes",
        "warning": None if job.render_mode != "image" else "整页图不可逐字编辑",
        "build_fingerprint": job.build_fingerprint_json or build_fingerprint(),
        "pages": [
            {
                "page_id": item["page_id"],
                "page_code": item["page_code"],
                "sort_order": item["sort_order"],
                "design_version_id": item["design_version_id"],
                "svg_hash": item["svg_hash"],
                "preflight": item.get("preflight") or {},
            }
            for item in pages
        ],
    }
    os.replace(temp_path, final_path)
    job.temp_path = None
    job.file_sha256 = checksum
    job.file_size = size
    job.slide_count = len(pages)
    job.manifest_json = manifest
    job.font_report_json = build_font_report([item["svg_markup"] for item in pages])
    return final_path


def _write_zip(path: Path, pages: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        manifest = []
        for item in pages:
            filename = f"{item['page_code']}.svg"
            archive.writestr(filename, item["svg_markup"])
            manifest.append({"page_id": item["page_id"], "page_code": item["page_code"], "file": filename, "svg_hash": item.get("svg_hash")})
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))


def verify_zip_package(path: Path, *, expected_count: int) -> None:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if "manifest.json" not in names:
            raise RuntimeError("ZIP 缺少 manifest.json")
        svgs = [name for name in names if name.endswith(".svg")]
        if len(svgs) != expected_count:
            raise RuntimeError(f"ZIP 页数 {len(svgs)} 与快照 {expected_count} 不一致")


def _set_progress(job: ExportJob, *, current: int, total: int) -> None:
    job.progress_json = {"current": current, "total": total, "phase": job.phase}


def _cleanup_temp(job: ExportJob) -> None:
    if not job.temp_path:
        return
    path = Path(job.temp_path)
    path.unlink(missing_ok=True)
    job.temp_path = None


def _emit(session: Session, job: ExportJob, event_type: str, extra: dict[str, Any] | None = None) -> None:
    payload = {"export_id": job.id, "status": job.status, "phase": job.phase}
    if extra:
        payload.update(extra)
    append_event(
        session,
        project_id=job.project_id,
        event_type=event_type,
        stage="export",
        scope_type="project",
        payload=payload,
    )
