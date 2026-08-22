from __future__ import annotations

from app.models.entities import DesignVersion, QualityEvalJob
from app.services.quality_eval import compose_tracks, estimate_quality_eval
from app.services.quality_eval_judge import RUBRIC_DIMENSIONS, normalize_rubric
from app.services.quality_report import build_quality_report, make_check
from app.services.visual_metrics import measure_visual_metrics
from tests.helpers import PNG_1X1, drain_tasks, make_content_page, make_project

SAMPLE_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" width="1280" height="720">
  <rect x="80" y="80" width="520" height="240" fill="#1d4ed8"/>
  <text x="96" y="160" font-size="48" fill="#ffffff" class="t-page-title" data-text-role="page_title">评估测试</text>
</svg>
""".strip()


def _perfect_rubric(**_kwargs):
    return normalize_rubric(
        {
            "dimensions": {
                name: {
                    "score": 5,
                    "confidence": "high",
                    "evidence": [{"region": "title", "observation": "主结论清晰"}],
                    "hard_issue": True,
                    "revision": "不构成硬失败",
                }
                for name in RUBRIC_DIMENSIONS
            }
        }
    )


def _fake_svg_render(svg_markup, output_path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(PNG_1X1)
    return {"status": "ok", "kind": "svg", "reason": None, "path": str(output_path)}


def _fake_pptx_render(markup, output_dir):
    path = output_dir / "pptx-render.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG_1X1)
    return {"status": "ok", "kind": "pptx", "reason": None, "path": str(path)}


def _patch_eval_io(monkeypatch):
    monkeypatch.setattr("app.services.quality_eval.render_svg_screenshot", _fake_svg_render)
    monkeypatch.setattr("app.services.quality_eval._render_pptx", _fake_pptx_render)
    monkeypatch.setattr("app.services.quality_eval.judge_rubric", _perfect_rubric)
    monkeypatch.setattr(
        "app.services.quality_eval.judge_pairwise",
        lambda **_kwargs: {"status": "ok", "winner": "B", "reason": "B 更清晰", "top_issue": "", "prompt_version": "quality-eval-judge.v1", "dimensions": {}},
    )


def _set_design_svg(db_session, page, markup, *, quality_report=None):
    design = db_session.get(DesignVersion, page.current_design_version_id)
    design.design_svg_markup = markup
    if quality_report is not None:
        design.quality_report_json = quality_report
    db_session.commit()
    return design


def test_compose_tracks_never_lets_subjective_score_override_hard_fail():
    tracks = compose_tracks(hard_fail=True, subjective_score=5.0, human_calibrated=4.8, vlm_status="ok")
    assert tracks["verdict"] == "hard_fail"
    assert tracks["ready_eligible"] is False
    assert tracks["subjective_score"] is None
    assert tracks["raw_subjective_score"] == 5.0
    assert tracks["human_calibrated"] is None
    assert tracks["subjective_overrides_hard_fail"] is False


def test_quality_report_tracks_keep_hard_fail_separate_from_subjective():
    report = build_quality_report(
        [make_check("text_out_of_bounds", "layout", status="fail", violations=[{"code": "TEXT_OUT_OF_BOUNDS"}])],
        subjective_score=0.96,
    )
    assert report["hard_fail"] is True
    assert report["status"] == "fail"
    assert report["tracks"]["hard_fail"] is True
    assert report["tracks"]["subjective_score"] == 0.96


def test_vlm_hard_issue_becomes_candidate_not_hard_fail():
    rubric = _perfect_rubric()
    assert rubric["mean_score"] == 5
    assert rubric["candidate_issues"]
    assert all(item["hard_issue"] is False for item in rubric["dimensions"].values())


def test_visual_metrics_warns_on_sparse_page():
    metrics = measure_visual_metrics(SAMPLE_SVG)
    assert 0.0 <= metrics["whitespace_ratio"] <= 1.0
    assert "warnings" in metrics


def test_off_mode_is_rejected(client, db_session):
    project = make_project(db_session, stage="design")
    make_content_page(db_session, project, page_code="p1", title="页一", sort_order=1)
    response = client.post(f"/api/v1/projects/{project.id}/quality-evals", json={"mode": "off"})
    assert response.status_code == 400


def test_page_quality_shows_not_run_without_fake_score(client, db_session):
    project = make_project(db_session, stage="design")
    page = make_content_page(db_session, project, page_code="p1", title="页一", sort_order=1)
    quality = client.get(f"/api/v1/projects/{project.id}/pages/{page.id}/quality")
    assert quality.status_code == 200
    payload = quality.json()["subjective_eval"]
    assert payload["status"] == "not_run"
    assert payload["label"] == "未运行主观视觉评估"
    assert payload["subjective_score"] is None


def test_estimate_quick_caps_all_pages_to_canary(db_session):
    project = make_project(db_session, stage="design")
    for index in range(1, 5):
        make_content_page(db_session, project, page_code=f"p{index}", title=f"页{index}", sort_order=index)
    estimate = estimate_quality_eval(db_session, project, mode="quick", scope="all_pages")
    assert estimate["mode"] == "quick"
    assert estimate["scope"] == "canary"
    assert estimate["scope_capped"] is True
    assert estimate["page_count"] == 2
    assert estimate["require_confirmation"] is True
    assert estimate["vlm_required"] is True


def test_eval_job_does_not_mutate_page_status_and_cannot_override_hard_fail(client, db_session, monkeypatch):
    _patch_eval_io(monkeypatch)
    project = make_project(db_session, stage="design")
    page = make_content_page(db_session, project, page_code="p1", title="硬失败页", sort_order=1)
    failing_report = build_quality_report(
        [make_check("text_out_of_bounds", "layout", status="fail", violations=[{"code": "TEXT_OUT_OF_BOUNDS", "node_id": "title"}])]
    )
    _set_design_svg(db_session, page, SAMPLE_SVG, quality_report=failing_report)
    design_status = page.design_status
    draft_status = page.draft_status

    created = client.post(
        f"/api/v1/projects/{project.id}/quality-evals",
        json={"mode": "standard", "scope": "selected_pages", "page_ids": [page.id]},
    )
    assert created.status_code == 202, created.text
    eval_id = created.json()["eval_id"]
    drain_tasks()
    job = client.get(f"/api/v1/projects/{project.id}/quality-evals/{eval_id}").json()
    assert job["status"] == "completed"
    page_report = job["report"]["pages"][0]
    assert page_report["hard_fail"] is True
    assert page_report["rubric"]["mean_score"] == 5
    assert page_report["tracks"]["verdict"] == "hard_fail"
    assert page_report["tracks"]["ready_eligible"] is False
    assert page_report["tracks"]["subjective_score"] is None
    assert page_report["tracks"]["raw_subjective_score"] == 5
    assert page_report["tracks"]["subjective_overrides_hard_fail"] is False
    db_session.refresh(page)
    assert page.design_status == design_status == "ready"
    assert page.draft_status == draft_status == "ready"
    quality = client.get(f"/api/v1/projects/{project.id}/pages/{page.id}/quality").json()
    assert quality["design_status"] == "ready"
    assert quality["subjective_eval"]["eval_job_id"] == eval_id
    assert quality["subjective_eval"]["tracks"]["verdict"] == "hard_fail"


def test_pairwise_winner_does_not_clear_hard_fail(client, db_session, monkeypatch):
    _patch_eval_io(monkeypatch)
    project = make_project(db_session, stage="design")
    page = make_content_page(db_session, project, page_code="p1", title="对照页", sort_order=1)
    failing_report = build_quality_report(
        [make_check("unresolved_required_slot", "visual", status="fail", violations=[{"code": "UNRESOLVED_REQUIRED_SLOT"}])]
    )
    current = _set_design_svg(db_session, page, SAMPLE_SVG, quality_report=failing_report)
    current.version_no = 2
    db_session.flush()
    previous = DesignVersion(
        project_id=project.id,
        page_id=page.id,
        version_no=1,
        status="ready",
        design_svg_markup=SAMPLE_SVG.replace("评估测试", "旧版"),
    )
    db_session.add(previous)
    db_session.commit()

    created = client.post(
        f"/api/v1/projects/{project.id}/quality-evals",
        json={"mode": "deep", "scope": "selected_pages", "page_ids": [page.id]},
    )
    drain_tasks()
    job = client.get(f"/api/v1/projects/{project.id}/quality-evals/{created.json()['eval_id']}").json()
    page_report = job["report"]["pages"][0]
    assert page_report["pairwise"]["winner"] == "B"
    assert page_report["pairwise"]["cannot_override_hard_fail"] is True
    assert page_report["tracks"]["verdict"] == "hard_fail"
    db_session.refresh(page)
    assert page.design_status == "ready"


def test_eval_worker_failure_does_not_fail_design(db_session, monkeypatch):
    from app.services.quality_eval import create_quality_eval_job
    from app.services.tasks import STATUS_FAILED, _finish_task, enqueue_quality_eval_job

    project = make_project(db_session, stage="design")
    page = make_content_page(db_session, project, page_code="p1", title="worker失败", sort_order=1)
    _set_design_svg(db_session, page, SAMPLE_SVG)
    job, created = create_quality_eval_job(db_session, project, mode="quick")
    assert created is True
    job.status = "running"
    db_session.commit()
    task = enqueue_quality_eval_job(db_session, project_id=project.id, eval_id=job.id)
    _finish_task(db_session, task, STATUS_FAILED, "boom")
    db_session.commit()
    stored = db_session.get(QualityEvalJob, job.id)
    assert stored.status == "failed"
    db_session.refresh(page)
    assert page.design_status == "ready"
    assert page.draft_status == "ready"


def test_human_review_records_disagreement_without_overriding_hard_fail(client, db_session, monkeypatch):
    _patch_eval_io(monkeypatch)
    project = make_project(db_session, stage="design")
    page = make_content_page(db_session, project, page_code="p1", title="校准页", sort_order=1)
    failing_report = build_quality_report(
        [make_check("text_overlap", "layout", status="fail", violations=[{"code": "TEXT_OVERLAP"}])]
    )
    _set_design_svg(db_session, page, SAMPLE_SVG, quality_report=failing_report)
    created = client.post(
        f"/api/v1/projects/{project.id}/quality-evals",
        json={"mode": "standard", "scope": "selected_pages", "page_ids": [page.id]},
    )
    drain_tasks()
    eval_id = created.json()["eval_id"]
    first = client.post(
        f"/api/v1/projects/{project.id}/quality-evals/{eval_id}/human-reviews",
        json={"page_id": page.id, "reviewer_id": "r1", "scores": {name: 5 for name in RUBRIC_DIMENSIONS}},
    )
    second = client.post(
        f"/api/v1/projects/{project.id}/quality-evals/{eval_id}/human-reviews",
        json={"page_id": page.id, "reviewer_id": "r2", "scores": {name: 2 for name in RUBRIC_DIMENSIONS}},
    )
    assert first.status_code == 200
    payload = second.json()
    assert payload["report"]["disagreements"]
    page_report = payload["report"]["pages"][0]
    assert page_report["tracks"]["verdict"] == "hard_fail"
    assert page_report["tracks"]["human_calibrated"] is None
    assert page_report["tracks"]["raw_human_calibrated"] == 3.5


def test_dual_render_skipped_without_renderer_does_not_invent_scores(client, db_session, monkeypatch):
    monkeypatch.setattr(
        "app.services.quality_eval.render_svg_screenshot",
        lambda *_args, **_kwargs: {"status": "skipped", "kind": "svg", "reason": "playwright_unavailable", "path": None},
    )
    monkeypatch.setattr(
        "app.services.quality_eval._render_pptx",
        lambda *_args, **_kwargs: {"status": "skipped", "kind": "pptx", "reason": "libreoffice_unavailable", "path": None},
    )
    project = make_project(db_session, stage="design")
    page = make_content_page(db_session, project, page_code="p1", title="无渲染", sort_order=1)
    _set_design_svg(db_session, page, SAMPLE_SVG, quality_report=build_quality_report([]))
    created = client.post(
        f"/api/v1/projects/{project.id}/quality-evals",
        json={"mode": "quick", "scope": "selected_pages", "page_ids": [page.id]},
    )
    drain_tasks()
    job = client.get(f"/api/v1/projects/{project.id}/quality-evals/{created.json()['eval_id']}").json()
    page_report = job["report"]["pages"][0]
    assert page_report["render"]["svg"]["status"] == "skipped"
    assert page_report["render"]["pptx"]["status"] == "skipped"
    assert page_report["vlm_status"] == "skipped"
    assert page_report["tracks"]["subjective_score"] is None
    assert page_report["tracks"]["verdict"] == "subjective_skipped"
    quality = client.get(f"/api/v1/projects/{project.id}/pages/{page.id}/quality").json()
    assert quality["subjective_eval"]["subjective_score"] is None


def test_idempotency_key_returns_same_in_progress_job(client, db_session):
    project = make_project(db_session, stage="design")
    page = make_content_page(db_session, project, page_code="p1", title="幂等", sort_order=1)
    _set_design_svg(db_session, page, SAMPLE_SVG)
    first = client.post(
        f"/api/v1/projects/{project.id}/quality-evals",
        json={"mode": "quick", "idempotency_key": "eval-key-1"},
    )
    second = client.post(
        f"/api/v1/projects/{project.id}/quality-evals",
        json={"mode": "quick", "idempotency_key": "eval-key-1"},
    )
    assert first.json()["eval_id"] == second.json()["eval_id"]
    assert first.json()["status"] == "queued"


def test_quick_and_standard_do_not_share_in_progress_job(client, db_session):
    project = make_project(db_session, stage="design")
    page = make_content_page(db_session, project, page_code="p1", title="分流", sort_order=1)
    _set_design_svg(db_session, page, SAMPLE_SVG)
    quick = client.post(f"/api/v1/projects/{project.id}/quality-evals", json={"mode": "quick"})
    standard = client.post(f"/api/v1/projects/{project.id}/quality-evals", json={"mode": "standard", "scope": "all_pages"})
    assert quick.status_code == 202
    assert standard.status_code == 202
    assert quick.json()["eval_id"] != standard.json()["eval_id"]


def test_judge_does_not_invent_scores_from_file_paths(tmp_path):
    from app.services.quality_eval_judge import judge_rubric

    png = tmp_path / "svg-render.png"
    png.write_bytes(PNG_1X1)
    result = judge_rubric(context={"page_id": "p1"}, image_paths=[str(png)])
    assert result["status"] == "skipped"
    assert result["reason"] == "vision_input_unsupported"
    assert result["mean_score"] is None


def test_preexisting_human_reviews_are_applied_on_complete(client, db_session, monkeypatch):
    _patch_eval_io(monkeypatch)
    project = make_project(db_session, stage="design")
    page = make_content_page(db_session, project, page_code="p1", title="人工回填", sort_order=1)
    _set_design_svg(
        db_session,
        page,
        SAMPLE_SVG,
        quality_report=build_quality_report([make_check("layout_ok", "layout", status="pass")]),
    )
    created = client.post(
        f"/api/v1/projects/{project.id}/quality-evals",
        json={"mode": "standard", "scope": "selected_pages", "page_ids": [page.id]},
    )
    eval_id = created.json()["eval_id"]
    from app.models.entities import QualityEvalJob

    job = db_session.get(QualityEvalJob, eval_id)
    job.human_reviews_json = [
        {
            "page_id": page.id,
            "reviewer_id": "r1",
            "blind": True,
            "scores": {name: 4 for name in RUBRIC_DIMENSIONS},
            "mean_score": 4.0,
            "pairwise": None,
            "notes": "",
        }
    ]
    db_session.commit()
    drain_tasks()
    payload = client.get(f"/api/v1/projects/{project.id}/quality-evals/{eval_id}").json()
    page_report = payload["report"]["pages"][0]
    assert page_report["tracks"]["human_calibrated"] == 4.0
    assert page_report["tracks"]["verdict"] != "hard_fail"
