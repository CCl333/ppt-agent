from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.base import now_utc
from app.models.entities import DesignVersion, DraftVersion, Project, ProjectPage, QualityEvalJob
from app.services.error_policy import classify_exception
from app.services.events import append_event
from app.services.export import build_pptx
from app.services.page_quality import evaluate_page_quality
from app.services.quality_eval_judge import (
    JUDGE_PROMPT_VERSION,
    RUBRIC_DIMENSIONS,
    judge_pairwise,
    judge_rubric,
)
from app.services.quality_eval_render import render_pptx_screenshot, render_svg_screenshot
from app.services.quality_report import hash_svg
from app.services.runtime import QUALITY_EVAL_VERSION, build_fingerprint
from app.services.visual_metrics import measure_visual_metrics
from app.services.visual_plan import collect_visual_slots

EVAL_MODES = frozenset({"off", "quick", "standard", "deep"})
EVAL_SCOPES = frozenset({"canary", "selected_pages", "all_pages"})
ACTIVE_EVAL_STATUSES = ("queued", "running")
CANARY_LIMIT = 2
TOKENS_PER_RUBRIC = 2500
TOKENS_PER_PAIRWISE = 2000
SECONDS_PER_RUBRIC = 20
SECONDS_PER_PAIRWISE = 15
NOT_RUN_LABEL = "未运行主观视觉评估"

JudgeRubricFn = Callable[..., dict[str, Any]]
JudgePairwiseFn = Callable[..., dict[str, Any]]


def canonical_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def not_run_subjective_eval() -> dict[str, Any]:
    return {
        "status": "not_run",
        "label": NOT_RUN_LABEL,
        "eval_job_id": None,
        "mode": "off",
        "subjective_score": None,
        "tracks": {
            "hard_fail": None,
            "measured_warning": [],
            "subjective_score": None,
            "human_calibrated": None,
        },
        "subjective_overrides_hard_fail": False,
    }


def compose_tracks(
    *,
    hard_fail: bool,
    measured_warnings: list[dict[str, Any]] | None = None,
    subjective_score: float | None = None,
    human_calibrated: float | None = None,
    vlm_status: str = "not_run",
) -> dict[str, Any]:
    warnings = list(measured_warnings or [])
    if hard_fail:
        verdict = "hard_fail"
    elif vlm_status == "skipped":
        verdict = "subjective_skipped"
    elif subjective_score is None:
        verdict = "not_evaluated"
    else:
        verdict = "subjective_only"
    return {
        "hard_fail": hard_fail,
        "measured_warning": warnings,
        "subjective_score": None if hard_fail else subjective_score,
        "raw_subjective_score": subjective_score,
        "human_calibrated": None if hard_fail else human_calibrated,
        "raw_human_calibrated": human_calibrated,
        "verdict": verdict,
        "subjective_overrides_hard_fail": False,
        "ready_eligible": not hard_fail,
    }


def estimate_quality_eval(
    session: Session,
    project: Project,
    *,
    mode: str,
    scope: str = "canary",
    page_ids: list[str] | None = None,
    pairwise_candidates: int = 2,
) -> dict[str, Any]:
    resolved_mode, resolved_scope, pages, capped = _resolve_pages(
        session, project, mode=mode, scope=scope, page_ids=page_ids
    )
    pairwise_pairs = 0
    if resolved_mode == "deep":
        pairwise_pairs = sum(1 for page in pages if _previous_design(session, page) is not None)
        if pairwise_candidates > 2:
            pairwise_pairs += max(0, pairwise_candidates - 2) * len(pages)
    model_multiplier = 2 if resolved_mode == "deep" else 1
    rubric_calls = len(pages) * model_multiplier
    pairwise_calls = pairwise_pairs * model_multiplier if resolved_mode == "deep" else 0
    estimated_tokens = rubric_calls * TOKENS_PER_RUBRIC + pairwise_calls * TOKENS_PER_PAIRWISE
    return {
        "mode": resolved_mode,
        "scope": resolved_scope,
        "scope_capped": capped,
        "page_count": len(pages),
        "page_ids": [page.id for page in pages],
        "estimated_calls": rubric_calls + pairwise_calls,
        "estimated_tokens": estimated_tokens,
        "estimated_cost": None,
        "estimated_duration_s": rubric_calls * SECONDS_PER_RUBRIC + pairwise_calls * SECONDS_PER_PAIRWISE,
        "pairwise_pairs": pairwise_pairs,
        "require_confirmation": True,
        "vlm_required": resolved_mode != "off",
        "build_fingerprint": build_fingerprint(),
        "judge_prompt_version": JUDGE_PROMPT_VERSION,
    }


def create_quality_eval_job(
    session: Session,
    project: Project,
    *,
    mode: str,
    scope: str = "canary",
    page_ids: list[str] | None = None,
    pairwise_candidates: int = 2,
    requested_models: list[str] | None = None,
    idempotency_key: str | None = None,
) -> tuple[QualityEvalJob, bool]:
    estimate = estimate_quality_eval(
        session,
        project,
        mode=mode,
        scope=scope,
        page_ids=page_ids,
        pairwise_candidates=pairwise_candidates,
    )
    if estimate["mode"] == "off":
        raise HTTPException(status_code=400, detail="主观评估未开启。请选择 quick / standard / deep")
    if estimate["page_count"] <= 0:
        raise HTTPException(status_code=422, detail="没有可评估的设计稿页面")
    snapshot = _build_snapshot(
        session,
        project,
        estimate["page_ids"],
        mode=str(estimate["mode"]),
        scope=str(estimate["scope"]),
    )
    existing = find_active_eval(
        session,
        project_id=project.id,
        input_hash=str(snapshot.get("input_hash") or ""),
        idempotency_key=idempotency_key,
    )
    if existing:
        return existing, False
    job = QualityEvalJob(
        project_id=project.id,
        mode=estimate["mode"],
        scope=estimate["scope"],
        status="queued",
        phase="snapshot",
        page_ids_json=list(estimate["page_ids"]),
        requested_models_json=list(requested_models or ["configured-vlm"]),
        estimated_tokens=int(estimate["estimated_tokens"]),
        sample_count=int(estimate["page_count"]),
        completed_count=0,
        progress_json={"current": 0, "total": estimate["page_count"], "phase": "snapshot"},
        input_snapshot_json=snapshot,
        input_hash=str(snapshot.get("input_hash") or ""),
        report_json={"estimate": estimate},
        human_reviews_json=[],
        build_fingerprint_json=build_fingerprint(),
        judge_prompt_version=JUDGE_PROMPT_VERSION,
        idempotency_key=idempotency_key,
        attempt=0,
    )
    session.add(job)
    session.flush()
    _emit(session, job, "quality_eval.queued")
    return job, True


def find_active_eval(
    session: Session,
    *,
    project_id: str,
    input_hash: str,
    idempotency_key: str | None,
) -> QualityEvalJob | None:
    if idempotency_key:
        existing = session.scalars(
            select(QualityEvalJob)
            .where(
                QualityEvalJob.project_id == project_id,
                QualityEvalJob.idempotency_key == idempotency_key,
                QualityEvalJob.status.in_(ACTIVE_EVAL_STATUSES),
            )
            .order_by(QualityEvalJob.created_at.desc())
        ).first()
        if existing:
            return existing
    return session.scalars(
        select(QualityEvalJob)
        .where(
            QualityEvalJob.project_id == project_id,
            QualityEvalJob.input_hash == input_hash,
            QualityEvalJob.status.in_(ACTIVE_EVAL_STATUSES),
        )
        .order_by(QualityEvalJob.created_at.desc())
    ).first()


def execute_quality_eval_job(
    session: Session,
    job: QualityEvalJob,
    *,
    eval_root: Path | None = None,
    rubric_fn: JudgeRubricFn | None = None,
    pairwise_fn: JudgePairwiseFn | None = None,
) -> None:
    if not _claim_running(session, job):
        return
    _emit(session, job, "quality_eval.updated", extra={"phase": job.phase})
    rubric_fn = rubric_fn or judge_rubric
    pairwise_fn = pairwise_fn or judge_pairwise
    try:
        pages, skipped_pages = _load_snapshot_pages(session, job)
        job.phase = "render"
        session.flush()
        page_results: list[dict[str, Any]] = []
        input_tokens = 0
        output_tokens = 0
        root = Path(eval_root or get_settings().quality_eval_path) / job.id
        root.mkdir(parents=True, exist_ok=True)
        total = max(len(pages) + len(skipped_pages), 1)
        for index, entry in enumerate(pages, start=1):
            page_results.append(
                _evaluate_page(
                    session,
                    job,
                    entry,
                    output_dir=root / str(entry["page_id"]),
                    rubric_fn=rubric_fn,
                    pairwise_fn=pairwise_fn,
                )
            )
            input_tokens += int(page_results[-1].get("input_tokens") or 0)
            output_tokens += int(page_results[-1].get("output_tokens") or 0)
            job.completed_count = index
            job.progress_json = {"current": index, "total": total, "phase": job.phase}
            session.flush()
            _emit(session, job, "quality_eval.updated", extra={"completed_count": index})
        job.phase = "report"
        job.actual_input_tokens = input_tokens
        job.actual_output_tokens = output_tokens
        report = {
            "schema_version": QUALITY_EVAL_VERSION,
            "judge_prompt_version": job.judge_prompt_version,
            "mode": job.mode,
            "scope": job.scope,
            "tracks_policy": "subjective_never_overrides_hard_fail",
            "pages": page_results,
            "skipped_pages": skipped_pages,
            "human_reviews": list(job.human_reviews_json or []),
            "disagreements": _disagreements(job.human_reviews_json or []),
        }
        _apply_human_to_pages(report, job.human_reviews_json or [])
        job.report_json = report
        job.status = "completed"
        job.finished_at = now_utc()
        job.error_code = None
        job.error_detail_json = {}
        session.flush()
        _emit(session, job, "quality_eval.completed")
    except Exception as exc:
        detail = classify_exception(exc)
        fail_quality_eval_job(session, job, detail if isinstance(detail, dict) else {"message": str(exc)})


def fail_quality_eval_job(
    session: Session,
    job: QualityEvalJob,
    detail: dict[str, Any],
    *,
    status_value: str = "failed",
) -> None:
    if job.status in {"completed", "failed", "canceled"}:
        return
    job.status = status_value
    job.finished_at = now_utc()
    job.error_code = str(detail.get("error_code") or "QUALITY_EVAL_FAILED")
    job.error_detail_json = dict(detail)
    job.updated_at = now_utc()
    session.flush()
    event_type = "quality_eval.canceled" if status_value == "canceled" else "quality_eval.failed"
    _emit(session, job, event_type, extra={"error_code": job.error_code})


def add_human_review(session: Session, job: QualityEvalJob, review: dict[str, Any]) -> dict[str, Any]:
    page_id = str(review.get("page_id") or "").strip()
    reviewer_id = str(review.get("reviewer_id") or "").strip()
    if not page_id or not reviewer_id:
        raise HTTPException(status_code=400, detail="人工评审需要 page_id 和 reviewer_id")
    if page_id not in set(job.page_ids_json or []):
        raise HTTPException(status_code=422, detail="该页不在本次评估范围内")
    scores = review.get("scores") if isinstance(review.get("scores"), dict) else {}
    normalized_scores: dict[str, int] = {}
    for name in RUBRIC_DIMENSIONS:
        try:
            normalized_scores[name] = max(0, min(5, int(round(float(scores.get(name, 0))))))
        except (TypeError, ValueError):
            normalized_scores[name] = 0
    record = {
        "page_id": page_id,
        "reviewer_id": reviewer_id,
        "blind": bool(review.get("blind", True)),
        "scores": normalized_scores,
        "mean_score": round(sum(normalized_scores.values()) / len(RUBRIC_DIMENSIONS), 3),
        "pairwise": review.get("pairwise") if isinstance(review.get("pairwise"), dict) else None,
        "notes": str(review.get("notes") or ""),
    }
    reviews = list(job.human_reviews_json or [])
    reviews.append(record)
    job.human_reviews_json = reviews
    report = dict(job.report_json or {})
    report["human_reviews"] = reviews
    report["disagreements"] = _disagreements(reviews)
    _apply_human_to_pages(report, reviews)
    job.report_json = report
    session.flush()
    return serialize_quality_eval_job(session, job)


def latest_eval_for_version(session: Session, *, project_id: str, design_version_id: str | None) -> QualityEvalJob | None:
    if not design_version_id:
        return None
    jobs = session.scalars(
        select(QualityEvalJob)
        .where(QualityEvalJob.project_id == project_id, QualityEvalJob.status == "completed")
        .order_by(QualityEvalJob.finished_at.desc(), QualityEvalJob.created_at.desc())
    )
    for job in jobs:
        for page in (job.report_json or {}).get("pages") or []:
            if page.get("design_version_id") == design_version_id:
                return job
    return None


def subjective_eval_for_page(
    session: Session,
    *,
    project_id: str,
    page_id: str,
    design_version_id: str | None,
) -> dict[str, Any]:
    payload = not_run_subjective_eval()
    job = latest_eval_for_version(session, project_id=project_id, design_version_id=design_version_id)
    if job is None:
        return payload
    page_report = next(
        (item for item in (job.report_json or {}).get("pages") or [] if item.get("page_id") == page_id),
        None,
    )
    if page_report is None:
        return payload
    tracks = page_report.get("tracks") or compose_tracks(hard_fail=bool(page_report.get("hard_fail")))
    return {
        "status": "completed",
        "label": "已完成主观视觉评估" if tracks.get("subjective_score") is not None else "主观评估未给出分数",
        "eval_job_id": job.id,
        "mode": job.mode,
        "subjective_score": tracks.get("subjective_score"),
        "tracks": tracks,
        "subjective_overrides_hard_fail": False,
        "vlm_status": page_report.get("vlm_status"),
        "render": page_report.get("render"),
    }


def serialize_quality_eval_job(session: Session, job: QualityEvalJob, *, project: Project | None = None) -> dict[str, Any]:
    project = project or session.get(Project, job.project_id)
    stale = False
    if project:
        current = _build_snapshot(
            session,
            project,
            list(job.page_ids_json or []),
            mode=job.mode,
            scope=job.scope,
        )
        stale = str(current.get("input_hash") or "") != str(job.input_hash or "")
    return {
        "eval_id": job.id,
        "project_id": job.project_id,
        "mode": job.mode,
        "scope": job.scope,
        "status": job.status,
        "phase": job.phase,
        "page_ids": list(job.page_ids_json or []),
        "requested_models": list(job.requested_models_json or []),
        "estimated_tokens": job.estimated_tokens,
        "actual_input_tokens": job.actual_input_tokens,
        "actual_output_tokens": job.actual_output_tokens,
        "estimated_cost": job.estimated_cost,
        "actual_cost": job.actual_cost,
        "sample_count": job.sample_count,
        "completed_count": job.completed_count,
        "progress": job.progress_json or {},
        "input_hash": job.input_hash,
        "input_snapshot": job.input_snapshot_json or {},
        "input_stale": stale,
        "report": job.report_json or {},
        "human_reviews": list(job.human_reviews_json or []),
        "error_code": job.error_code,
        "error_detail": job.error_detail_json or {},
        "build_fingerprint": job.build_fingerprint_json or {},
        "judge_prompt_version": job.judge_prompt_version,
        "attempt": job.attempt,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


def list_quality_eval_jobs(session: Session, project_id: str, *, limit: int = 20) -> list[QualityEvalJob]:
    return list(
        session.scalars(
            select(QualityEvalJob)
            .where(QualityEvalJob.project_id == project_id)
            .order_by(QualityEvalJob.created_at.desc())
            .limit(max(1, limit))
        )
    )


def _resolve_pages(
    session: Session,
    project: Project,
    *,
    mode: str,
    scope: str,
    page_ids: list[str] | None,
) -> tuple[str, str, list[ProjectPage], bool]:
    resolved_mode = str(mode or "off").strip().lower() or "off"
    resolved_scope = str(scope or "canary").strip().lower() or "canary"
    if resolved_mode not in EVAL_MODES:
        raise HTTPException(status_code=400, detail="评估模式必须是 off / quick / standard / deep")
    if resolved_scope not in EVAL_SCOPES:
        raise HTTPException(status_code=400, detail="评估范围必须是 canary / selected_pages / all_pages")
    if resolved_mode == "off":
        return resolved_mode, resolved_scope, [], False
    pages = list(
        session.scalars(
            select(ProjectPage).where(ProjectPage.project_id == project.id).order_by(ProjectPage.sort_order.asc())
        )
    )
    ready = [page for page in pages if page.design_status == "ready" and page.current_design_version_id]
    capped = False
    if resolved_scope == "selected_pages":
        wanted = {str(item) for item in page_ids or []}
        if not wanted:
            raise HTTPException(status_code=400, detail="selected_pages 需要提供 page_ids")
        selected = [page for page in ready if page.id in wanted]
    elif resolved_mode == "quick":
        selected = _pick_canary(session, ready)
        capped = resolved_scope == "all_pages"
        resolved_scope = "canary"
    elif resolved_scope == "canary":
        selected = _pick_canary(session, ready)
    else:
        selected = ready
    return resolved_mode, resolved_scope, selected, capped


def _pick_canary(session: Session, pages: list[ProjectPage]) -> list[ProjectPage]:
    scored: list[tuple[int, int, ProjectPage]] = []
    for page in pages:
        score = {"cover": 100, "toc": 70, "content": 50, "section": 20, "ending": 40}.get(page.page_role or "content", 10)
        design = session.get(DesignVersion, page.current_design_version_id) if page.current_design_version_id else None
        slots = collect_visual_slots(design.visual_plan_json if design else None)
        if any(str(slot.get("kind") or "") in {"photo", "chart", "generated_image", "diagram"} for slot in slots):
            score += 20
        scored.append((score, page.sort_order, page))
    scored.sort(key=lambda item: (-item[0], item[1]))
    picked: list[ProjectPage] = []
    for _score, _order, page in scored:
        if page not in picked:
            picked.append(page)
        if len(picked) >= CANARY_LIMIT:
            break
    return picked


def _build_snapshot(
    session: Session,
    project: Project,
    page_ids: list[str],
    *,
    mode: str,
    scope: str,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for page_id in page_ids:
        page = session.get(ProjectPage, page_id)
        if page is None or page.project_id != project.id:
            continue
        design = session.get(DesignVersion, page.current_design_version_id) if page.current_design_version_id else None
        draft = session.get(DraftVersion, page.current_draft_version_id) if page.current_draft_version_id else None
        markup = design.design_svg_markup if design else ""
        entries.append(
            {
                "page_id": page.id,
                "page_code": page.page_code,
                "sort_order": page.sort_order,
                "page_role": page.page_role,
                "design_status": page.design_status,
                "draft_status": page.draft_status,
                "design_version_id": page.current_design_version_id,
                "draft_version_id": page.current_draft_version_id,
                "svg_hash": (design.svg_hash if design else None) or (hash_svg(markup) if design else None),
                "quality_report": dict(design.quality_report_json or {}) if design else {},
                "visual_plan": (design.visual_plan_json if design else None) or (draft.visual_plan_json if draft else {}),
                "layout_plan": (design.layout_plan_json if design else None) or (draft.layout_plan_json if draft else {}),
            }
        )
    snapshot = {
        "project_id": project.id,
        "mode": mode,
        "scope": scope,
        "pages": entries,
        "quality_eval_version": QUALITY_EVAL_VERSION,
        "build_fingerprint": build_fingerprint(),
    }
    snapshot["input_hash"] = canonical_hash(
        {
            "project_id": project.id,
            "mode": mode,
            "scope": scope,
            "pages": [
                {
                    "page_id": item["page_id"],
                    "design_version_id": item["design_version_id"],
                    "svg_hash": item["svg_hash"],
                }
                for item in entries
            ],
        }
    )
    return snapshot


def _load_snapshot_pages(session: Session, job: QualityEvalJob) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pages: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for entry in (job.input_snapshot_json or {}).get("pages") or []:
        design = session.get(DesignVersion, entry.get("design_version_id")) if entry.get("design_version_id") else None
        if design is None:
            skipped.append({**entry, "skip_reason": "design_missing"})
            continue
        markup = design.design_svg_markup or ""
        actual_hash = design.svg_hash or hash_svg(markup)
        drift = bool(entry.get("svg_hash") and actual_hash != entry.get("svg_hash"))
        pages.append({**entry, "design": design, "svg_markup": markup, "snapshot_drift": drift})
    if not pages:
        raise RuntimeError("评估快照中没有可用设计稿")
    return pages, skipped


def _evaluate_page(
    session: Session,
    job: QualityEvalJob,
    entry: dict[str, Any],
    *,
    output_dir: Path,
    rubric_fn: JudgeRubricFn,
    pairwise_fn: JudgePairwiseFn,
) -> dict[str, Any]:
    design: DesignVersion = entry["design"]
    markup = entry.get("svg_markup") or ""
    output_dir.mkdir(parents=True, exist_ok=True)
    stored = entry.get("quality_report") if isinstance(entry.get("quality_report"), dict) else {}
    if not (stored.get("checks") or stored.get("hard_fail") is True):
        live = design.quality_report_json if isinstance(design.quality_report_json, dict) else {}
        stored = live if (live.get("checks") or live.get("hard_fail") is True) else {}
    if stored.get("checks") or stored.get("hard_fail") is True:
        quality = stored
    else:
        quality = evaluate_page_quality(
            markup,
            stage="design",
            content_plan=None,
            layout_plan=entry.get("layout_plan") if isinstance(entry.get("layout_plan"), dict) else design.layout_plan_json,
            visual_plan=entry.get("visual_plan") if isinstance(entry.get("visual_plan"), dict) else design.visual_plan_json,
        )
    hard_fail = bool(quality.get("hard_fail"))
    metrics = measure_visual_metrics(markup, layout_plan=design.layout_plan_json)
    measured_warnings = list(quality.get("warnings") or []) + list(metrics.get("warnings") or [])
    job.phase = "render"
    svg_render = render_svg_screenshot(markup, output_dir / "svg-render.png")
    pptx_render = _render_pptx(markup, output_dir)
    image_paths = [path for path in (svg_render.get("path"), pptx_render.get("path")) if path]
    vlm_status = "skipped"
    rubric: dict[str, Any] = {"status": "skipped", "reason": "render_unavailable", "mean_score": None}
    pairwise: dict[str, Any] | None = None
    input_tokens = 0
    output_tokens = 0
    if job.mode in {"quick", "standard", "deep"}:
        job.phase = "vlm"
        try:
            rubric = rubric_fn(
                context={
                    "page_id": entry.get("page_id"),
                    "page_code": entry.get("page_code"),
                    "page_role": entry.get("page_role"),
                    "hard_fail": hard_fail,
                    "quality_status_reason": quality.get("status_reason"),
                    "visual_plan": entry.get("visual_plan") or {},
                    "metrics": metrics,
                },
                image_paths=image_paths,
            )
            vlm_status = str(rubric.get("status") or "ok")
            if vlm_status == "ok":
                input_tokens += TOKENS_PER_RUBRIC
                output_tokens += 400
        except Exception as exc:
            vlm_status = "failed"
            rubric = {"status": "failed", "reason": str(exc)[:300], "mean_score": None, "dimensions": {}}
        if job.mode == "deep":
            job.phase = "pairwise"
            previous = _previous_design(session, session.get(ProjectPage, entry["page_id"]))
            if previous is not None:
                prev_dir = output_dir / "previous"
                prev_svg = render_svg_screenshot(previous.design_svg_markup or "", prev_dir / "svg-render.png")
                try:
                    pairwise = pairwise_fn(
                        context={
                            "page_id": entry.get("page_id"),
                            "left_version_id": previous.id,
                            "right_version_id": design.id,
                        },
                        image_a=prev_svg.get("path"),
                        image_b=svg_render.get("path"),
                    )
                    if pairwise.get("status") == "ok":
                        input_tokens += TOKENS_PER_PAIRWISE
                        output_tokens += 200
                except Exception as exc:
                    pairwise = {"status": "failed", "reason": str(exc)[:300], "winner": "tie"}
    subjective_score = rubric.get("mean_score") if vlm_status == "ok" else None
    tracks = compose_tracks(
        hard_fail=hard_fail,
        measured_warnings=measured_warnings,
        subjective_score=subjective_score,
        vlm_status=vlm_status,
    )
    if pairwise and pairwise.get("winner") in {"A", "B"} and hard_fail:
        pairwise = {**pairwise, "cannot_override_hard_fail": True}
    return {
        "page_id": entry.get("page_id"),
        "page_code": entry.get("page_code"),
        "design_version_id": design.id,
        "draft_status": entry.get("draft_status"),
        "design_status": entry.get("design_status"),
        "hard_fail": hard_fail,
        "quality_status": quality.get("status"),
        "quality_status_reason": quality.get("status_reason"),
        "snapshot_drift": bool(entry.get("snapshot_drift")),
        "metrics": metrics,
        "render": {"svg": svg_render, "pptx": pptx_render},
        "vlm_status": vlm_status,
        "rubric": rubric,
        "pairwise": pairwise,
        "tracks": tracks,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def _render_pptx(markup: str, output_dir: Path) -> dict[str, Any]:
    pptx_path = output_dir / "page.pptx"
    try:
        build_pptx([("eval", markup)], pptx_path, mode="shapes")
    except Exception as exc:
        return {
            "status": "skipped",
            "kind": "pptx",
            "reason": "pptx_build_failed",
            "detail": str(exc)[:300],
            "path": None,
        }
    return render_pptx_screenshot(pptx_path, output_dir / "pptx-render.png")


def _previous_design(session: Session, page: ProjectPage | None) -> DesignVersion | None:
    if page is None or not page.current_design_version_id:
        return None
    current = session.get(DesignVersion, page.current_design_version_id)
    if current is None:
        return None
    return session.scalars(
        select(DesignVersion)
        .where(
            DesignVersion.page_id == page.id,
            DesignVersion.id != current.id,
            DesignVersion.version_no < current.version_no,
        )
        .order_by(DesignVersion.version_no.desc())
    ).first()


def _claim_running(session: Session, job: QualityEvalJob) -> bool:
    if job.status in {"completed", "failed", "canceled"}:
        return False
    now = now_utc()
    result = session.execute(
        update(QualityEvalJob)
        .where(QualityEvalJob.id == job.id, QualityEvalJob.status == "queued")
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


def _emit(session: Session, job: QualityEvalJob, event_type: str, extra: dict[str, Any] | None = None) -> None:
    payload = {"eval_id": job.id, "status": job.status, "phase": job.phase, "mode": job.mode}
    if extra:
        payload.update(extra)
    append_event(
        session,
        project_id=job.project_id,
        event_type=event_type,
        stage="design",
        scope_type="project",
        payload=payload,
    )


def _disagreements(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_page: dict[str, list[dict[str, Any]]] = {}
    for review in reviews:
        by_page.setdefault(str(review.get("page_id")), []).append(review)
    found: list[dict[str, Any]] = []
    for page_id, items in by_page.items():
        if len(items) < 2:
            continue
        for name in RUBRIC_DIMENSIONS:
            scores = [int((item.get("scores") or {}).get(name) or 0) for item in items]
            if max(scores) - min(scores) >= 2:
                found.append(
                    {
                        "page_id": page_id,
                        "dimension": name,
                        "scores": scores,
                        "reviewer_ids": [item.get("reviewer_id") for item in items],
                    }
                )
        winners = [
            str((item.get("pairwise") or {}).get("winner") or "")
            for item in items
            if isinstance(item.get("pairwise"), dict) and item.get("pairwise")
        ]
        unique = {item for item in winners if item}
        if len(unique) > 1:
            found.append({"page_id": page_id, "dimension": "pairwise", "winners": sorted(unique)})
    return found


def _apply_human_to_pages(report: dict[str, Any], reviews: list[dict[str, Any]]) -> None:
    by_page: dict[str, list[dict[str, Any]]] = {}
    for review in reviews:
        by_page.setdefault(str(review.get("page_id")), []).append(review)
    for page in report.get("pages") or []:
        items = by_page.get(str(page.get("page_id")), [])
        if not items:
            continue
        human_mean = round(sum(float(item.get("mean_score") or 0) for item in items) / len(items), 3)
        tracks = dict(page.get("tracks") or {})
        page["tracks"] = compose_tracks(
            hard_fail=bool(page.get("hard_fail") or tracks.get("hard_fail")),
            measured_warnings=list(tracks.get("measured_warning") or []),
            subjective_score=tracks.get("subjective_score"),
            human_calibrated=human_mean,
            vlm_status=str(page.get("vlm_status") or "not_run"),
        )
        page["human_calibrated"] = None if page["tracks"]["hard_fail"] else human_mean
