from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from app.models.base import new_id
from app.models.entities import (
    DesignVersion,
    DraftVersion,
    OutlineVersion,
    PageBriefVersion,
    Project,
    ProjectMessage,
    ProjectPage,
    RequirementForm,
    ResearchSession,
)
from app.services.page_images import materialize_page_images
from app.services.page_quality import apply_report_to_version, assert_quality_ready, evaluate_page_quality
from app.services.page_scene import extract_page_scene, propose_layout_plan
from app.services.visual_plan import propose_visual_plan
from app.services.search_plan import merge_search_results, next_search_round, search_coverage
from app.services.search_quality import MIN_DIGEST_CHARS
from app.services.style_cards import get_library_card
from app.services.tasks import enqueue_batch_action, wake_scheduler


class PageFlowMixin:
    def _mark_page_stage_failed(self, page: ProjectPage, action_type: str) -> None:
        if action_type in {"page_search_run", "page_search_refresh"}:
            page.search_status = "failed"
        elif action_type == "page_summary_generate":
            page.summary_status = "failed"
        elif action_type == "page_draft_generate":
            page.draft_status = "failed"
        elif action_type == "page_design_generate":
            page.design_status = "failed"

    def _run_page_query_generation(
        self,
        *,
        project: Project,
        page: ProjectPage,
        latest_instruction: str,
    ) -> list[dict[str, str]]:
        if page.page_role != "content":
            raise RuntimeError("固定页不需要生成搜索词")
        brief = self._get_current_brief(page)
        if not brief:
            raise RuntimeError("页面结构不存在")
        queries = self.generator.generate_page_search_queries(
            project_title=project.title,
            project_request=project.request_text,
            page_id=page.id,
            page_title=brief.title,
            page_bullets=brief.content_outline_json,
            page_section_title=page.part_title,
            outline_full_snapshot=self._build_outline_snapshot(project.id),
            latest_instruction=latest_instruction,
        )
        page.page_search_queries_json = queries
        return queries

    def _run_page_search(
        self,
        *,
        project: Project,
        page: ProjectPage,
        latest_instruction: str,
        replace_existing: bool,
        run: AgentRunRecorder | None = None,
    ) -> dict[str, Any]:
        if page.page_role != "content":
            raise RuntimeError("固定页不需要页级搜索")
        queries = page.page_search_queries_json
        if not queries:
            if run is not None:
                run.step_started("page_search_prepare_queries", "补齐页面搜索词", "当前页还没有搜索词，先自动补齐。")
            queries = self._run_page_query_generation(
                project=project,
                page=page,
                latest_instruction=latest_instruction,
            )
            if run is not None:
                run.data_updated(
                    {
                        "entity": "page",
                        "page_id": page.id,
                        "update_kind": "search_queries",
                        "query_count": len(queries),
                    }
                )
                run.step_completed("page_search_prepare_queries", "补齐页面搜索词", {"query_count": len(queries)})
        existing_results = [] if replace_existing else list(page.page_search_results_json or [])
        search_round = 1 if replace_existing else next_search_round(page.page_search_results_json)
        page.search_status = "running"
        if run is not None:
            run.data_updated(
                {
                    "entity": "page",
                    "page_id": page.id,
                    "update_kind": "search_started",
                    "query_count": len(queries),
                }
            )

        from app.services.search_settings import snapshot_search_runtime

        collection = self.research.get_or_create_page_collection(project, page)
        if snapshot_search_runtime().mode == "llm":
            candidate_sources, pending_chunk_records = self._collect_page_llm_digest(
                page=page,
                queries=queries,
                collection=collection,
                replace_existing=replace_existing,
                search_round=search_round,
                run=run,
            )
        else:
            candidate_sources, pending_chunk_records = self._collect_page_bocha_sources(
                page=page,
                queries=queries,
                collection=collection,
                replace_existing=replace_existing,
                search_round=search_round,
                existing_results=existing_results,
                run=run,
            )
        if not replace_existing:
            candidate_sources = merge_search_results(existing_results, candidate_sources)

        if run is not None:
            run.step_started("page_search_chunk", "切块入库资料池", "把正文切块，供后续确定性选证据使用。")

        def on_chunk_progress(payload: dict[str, Any]) -> None:
            if run is None:
                return
            run.step_progress(
                "page_search_chunk",
                "切块入库资料池",
                progress={
                    "current": payload["completed_chunks"],
                    "total": payload["total_chunks"],
                    "label": f"已写入 {payload['completed_chunks']}/{payload['total_chunks']} 个 chunk",
                },
                result={"document_count": payload["document_count"]},
            )

        chunk_stats = self.research.store_chunks(
            pending_chunk_records,
            on_chunk_progress=on_chunk_progress if run is not None else None,
        )
        candidate_sources = self.research.refresh_search_result_cards(candidate_sources)
        digest = self.research.build_collection_digest(collection.id)
        session = self.research.create_session(
            project_id=project.id,
            page_id=page.id,
            scope_type="page",
            session_role="page_search",
            research_goal=f"为页面《{self._get_current_brief(page).title if self._get_current_brief(page) else page.page_code}》建立独立资料池。",
            query_plan=queries,
            context_snapshot={"latest_instruction": latest_instruction},
        )
        session.candidate_sources_json = candidate_sources
        session.status = "completed" if digest.get("document_count") else "failed"
        page.current_research_session_id = session.id
        page.page_search_results_json = candidate_sources
        page.page_corpus_digest_json = digest
        page.page_images_json = self._refresh_page_images(project, page)
        page.search_status = "ready" if digest.get("document_count") else "failed"
        page.summary_status = "stale" if page.page_summary_md else "empty"
        page.draft_status = "stale" if page.current_draft_version_id else "empty"
        page.design_status = "stale" if page.current_design_version_id else "empty"
        self._update_artifact_staleness(page)
        if run is not None:
            run.data_updated(
                {
                    "entity": "page",
                    "page_id": page.id,
                    "update_kind": "search_chunked",
                    "document_count": digest.get("document_count", 0),
                    "chunk_count": digest.get("chunk_count", 0),
                }
            )
            run.step_completed(
                "page_search_chunk",
                "切块入库资料池",
                {
                    "document_count": digest.get("document_count", 0),
                    "chunk_count": digest.get("chunk_count", 0),
                    **chunk_stats,
                },
            )
        self.session.flush()
        coverage = search_coverage(queries, candidate_sources)
        return {
            "query_count": len(queries),
            "result_count": len(candidate_sources),
            "search_round": search_round,
            "dimension_count": coverage["dimension_count"],
            "latest_round_hits": coverage["latest_round_hits"],
            **digest,
        }

    def _build_page_search_intent(
        self,
        page: ProjectPage,
        queries: list[dict[str, str]],
    ) -> str:
        brief = self._get_current_brief(page)
        title = brief.title if brief else page.page_code
        outline = "；".join(brief.content_outline_json or []) if brief else ""
        query_lines = "\n".join(
            f"- {item.get('query_text')}" for item in queries if item.get("query_text")
        )
        return (
            f"请为这一页PPT整理资料。\n"
            f"页面标题：{title}\n"
            f"所属章节：{page.part_title or ''}\n"
            f"内容要点：{outline}\n"
            f"检索问题：\n{query_lines}"
        )

    def _collect_page_llm_digest(
        self,
        *,
        page: ProjectPage,
        queries: list[dict[str, str]],
        collection,
        replace_existing: bool,
        search_round: int,
        run,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if run is not None:
            run.step_started(
                "page_search_digest",
                "让搜索模型整理该页资料",
                "一次调用搜索模型，把整理稿写入本页资料池，不再重新抓取网页。",
            )
        from app.services.search_quality import require_reachable_sources

        intent = self._build_page_search_intent(page, queries)
        # search_web_digest 已完成整理稿质量校验与一次重试，这里拿到的必定是合格正文。
        digest = self.research.mcp.search_web_digest(intent, limit=8)
        answer = digest.answer
        live_sources = require_reachable_sources(digest.items, raw_excerpt=answer)
        brief = self._get_current_brief(page)
        candidate_sources, pending_chunk_records = self.research.ingest_llm_answer(
            collection=collection,
            page=page,
            answer=answer,
            sources=live_sources,
            query_text=intent,
            page_title=brief.title if brief else page.page_code,
            replace=replace_existing,
            search_round=search_round,
        )
        candidate_sources = self.research.refresh_search_result_cards(candidate_sources)
        page.page_search_results_json = candidate_sources
        if run is not None:
            run.data_updated(
                {
                    "entity": "page",
                    "page_id": page.id,
                    "update_kind": "search_results",
                    "result_count": len(candidate_sources),
                }
            )
            run.step_completed(
                "page_search_digest",
                "让搜索模型整理该页资料",
                {"result_count": len(candidate_sources), "digest_chars": len(answer)},
            )
        return candidate_sources, pending_chunk_records

    def _collect_page_bocha_sources(
        self,
        *,
        page: ProjectPage,
        queries: list[dict[str, str]],
        collection,
        replace_existing: bool,
        search_round: int,
        existing_results: list[dict[str, Any]],
        run,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if run is not None:
            run.step_started("page_search_bocha", "执行 Bocha 搜索", "先获取搜索摘要结果，再决定后续抓取。")

        def on_query_completed(payload: dict[str, Any]) -> None:
            page.page_search_results_json = merge_search_results(existing_results, payload["items"])
            if run is not None:
                run.data_updated(
                    {
                        "entity": "page",
                        "page_id": page.id,
                        "update_kind": "search_results",
                        "query_index": payload["query_index"],
                        "query_total": payload["query_total"],
                        "result_count": payload["result_count"],
                        "search_round": search_round,
                    }
                )
                run.step_progress(
                    "page_search_bocha",
                    "执行 Bocha 搜索",
                    progress={
                        "current": payload["query_index"],
                        "total": payload["query_total"],
                        "label": f"已完成 {payload['query_index']}/{payload['query_total']} 条查询",
                    },
                    result={"result_count": payload["result_count"]},
                )

        search_results = self.research.search_query_summaries(
            queries,
            limit_per_query=4,
            search_round=search_round,
            on_query_completed=on_query_completed if run is not None else None,
        )
        if run is not None:
            run.step_completed("page_search_bocha", "执行 Bocha 搜索", {"result_count": len(search_results)})
            run.step_started("page_search_read", "抓取全文并写入资料池", "把搜索结果扩展成可引用正文。")

        def on_read_progress(payload: dict[str, Any]) -> None:
            if run is None:
                return
            run.step_progress(
                "page_search_read",
                "抓取全文并写入资料池",
                progress={
                    "current": payload["completed"],
                    "total": payload["total"],
                    "label": f"已处理 {payload['completed']}/{payload['total']} 条来源",
                },
                result={
                    "ingested_count": payload["ingested_count"],
                    "failed_count": payload["failed_count"],
                },
            )

        candidate_sources, pending_chunk_records, _read_summary = self.research.hydrate_search_results(
            collection=collection,
            search_results=search_results,
            replace=replace_existing,
            on_read_progress=on_read_progress if run is not None else None,
        )
        candidate_sources = self.research.refresh_search_result_cards(candidate_sources)
        page.page_search_results_json = candidate_sources
        read_ready = sum(1 for item in candidate_sources if item.get("read_status") in {"ready", "reused"})
        read_failed = sum(1 for item in candidate_sources if item.get("read_status") == "failed")
        if run is not None:
            run.data_updated(
                {
                    "entity": "page",
                    "page_id": page.id,
                    "update_kind": "search_read",
                    "result_count": len(candidate_sources),
                    "read_ready": read_ready,
                    "read_failed": read_failed,
                }
            )
            run.step_completed(
                "page_search_read",
                "抓取全文并写入资料池",
                {
                    "result_count": len(candidate_sources),
                    "read_ready": read_ready,
                    "read_failed": read_failed,
                },
            )
        return candidate_sources, pending_chunk_records

    def _run_page_summary(
        self,
        *,
        project: Project,
        page: ProjectPage,
        latest_instruction: str,
    ) -> dict[str, Any]:
        if page.page_role != "content":
            raise RuntimeError("固定页不需要页级 summary 生成")
        brief = self._get_current_brief(page)
        if not brief:
            raise RuntimeError("页面结构不存在")
        digest = page.page_corpus_digest_json or {}
        if not digest.get("document_count"):
            raise RuntimeError("当前页资料池为空，不能生成 summary")
        if "content_chars" not in digest:
            # 本字段是后加的，老资料池没有。惰性补算而不是一律拦死——
            # 否则资料池本身完好的历史页面也会被迫全量重搜。
            collection = self.research.get_or_create_page_collection(project, page)
            digest = self.research.build_collection_digest(collection.id)
            page.page_corpus_digest_json = digest
        corpus_chars = int(digest.get("content_chars") or 0)
        if corpus_chars < MIN_DIGEST_CHARS:
            raise RuntimeError(
                f"当前页资料池正文仅 {corpus_chars} 字（下限 {MIN_DIGEST_CHARS}），"
                "不足以生成 summary，请先重跑检索"
            )
        page.summary_status = "running"
        # 摘要 LLM 调用耗时长，状态位必须先落库：否则 worker 中途死亡会回滚成 empty，
        # 而 tasks.py 的两条恢复路径都以 == "running" 为触发条件，永远兜不住。
        self.session.commit()
        query_plan = page.page_search_queries_json or [
            {
                "query_text": f"{brief.title} {' '.join(brief.content_outline_json)}".strip(),
                "query_purpose": "当前页核心事实和证据",
            }
        ]
        collection = self.research.get_or_create_page_collection(project, page)
        session = self.research.create_session(
            project_id=project.id,
            page_id=page.id,
            scope_type="page",
            session_role="page_summary",
            research_goal=f"从当前页资料池生成页面《{brief.title}》的详实摘要。",
            query_plan=query_plan,
            context_snapshot={"latest_instruction": latest_instruction},
        )
        selected = self.research.retrieve_for_collection(
            project=project,
            collection=collection,
            research_session=session,
            query_plan=query_plan,
            limit=30,
            excerpt_limit=1200,
        )
        summary_package = self.generator.summarize_selected_sources(
            scope_type="page",
            research_goal=session.research_goal or "",
            selected_sources=selected,
        )
        session.summary_md = summary_package["summary_md"]
        session.status = "completed" if selected else "failed"
        page.current_research_session_id = session.id
        page.page_summary_md = summary_package["summary_md"]
        page.page_summary_citations_json = selected
        page.summary_status = "ready" if page.page_summary_md else "failed"
        page.draft_status = "stale" if page.current_draft_version_id else "empty"
        page.design_status = "stale" if page.current_design_version_id else "empty"
        self._update_artifact_staleness(page)
        return {"citation_count": len(selected), "summary_length": len(page.page_summary_md)}

    def _run_page_draft(
        self,
        *,
        project: Project,
        page: ProjectPage,
        latest_instruction: str,
    ) -> dict[str, Any]:
        brief = self._get_current_brief(page)
        if not brief:
            raise RuntimeError("页面结构不存在")
        summary_md = page.page_summary_md.strip()
        summary_source = "page_summary"
        if not summary_md and page.page_role != "content":
            summary_md = self._build_fixed_page_summary(brief.title, brief.content_outline_json)
            summary_source = "outline_brief"
        if not summary_md:
            raise RuntimeError("当前页 summary 为空，不能生成策划稿")
        self._ensure_page_images(project, page)
        self._set_project_stage_at_least(project, "draft")
        page_context = {
            "page": {
                "page_id": page.id,
                "page_code": page.page_code,
                "page_role": page.page_role,
                "page_brief_version_id": brief.id,
                "title": brief.title,
                "content_outline": brief.content_outline_json,
                "content_summary": brief.content_summary,
            },
            "summary": {
                "summary_md": summary_md,
                "selected_sources": page.page_summary_citations_json,
            },
            "page_images": page.page_images_json or [],
            "latest_instruction": latest_instruction,
        }
        plan = self.generator.generate_content_plan(page_context=page_context)
        if page.page_role == "section":
            visual_intent = str((self._section_meta_from_outline(page) or {}).get("visual_intent") or "").strip()
            if visual_intent:
                plan["visual_intent"] = visual_intent
        visual_plan = propose_visual_plan(plan, page_images=page.page_images_json or [], page_role=page.page_role)
        plan = {**plan, "visual_slots": visual_plan.get("slots") or []}
        layout_plan = propose_layout_plan(plan, page_role=page.page_role)
        svg = self.generator.generate_draft_svg(
            page_context=page_context,
            content_plan=plan,
            layout_plan=layout_plan,
            visual_plan=visual_plan,
        )
        extracted = extract_page_scene(svg, content_plan=plan, base_scene=layout_plan)
        report = evaluate_page_quality(
            svg,
            stage="draft",
            content_plan=plan,
            layout_plan=layout_plan,
            visual_plan=visual_plan,
            run_export_preflight=False,
        )
        version_no = (self.session.scalar(select(func.count(DraftVersion.id)).where(DraftVersion.page_id == page.id)) or 0) + 1
        draft = DraftVersion(
            project_id=project.id,
            page_id=page.id,
            version_no=version_no,
            status="ready",
            page_brief_version_id=brief.id,
            research_session_id=page.current_research_session_id,
            draft_svg_markup=svg,
            content_plan_json=plan,
            visual_plan_json=visual_plan,
            layout_plan_json=extracted,
        )
        apply_report_to_version(draft, report, svg_markup=svg)
        self.session.add(draft)
        self.session.flush()
        page.current_draft_version_id = draft.id
        if report.get("hard_fail"):
            page.draft_status = "failed"
            self._update_artifact_staleness(page)
            assert_quality_ready(report, stage="draft")
        page.draft_status = "ready"
        page.design_status = "stale" if page.current_design_version_id else "empty"
        self._update_artifact_staleness(page)
        return {"draft_version_id": draft.id, "summary_source": summary_source, "quality_report": report}

    def _run_page_design(
        self,
        *,
        project: Project,
        page: ProjectPage,
    ) -> dict[str, Any]:
        draft = self._get_current_draft(page)
        if not draft:
            raise RuntimeError("当前页策划稿为空，不能生成设计稿")
        if not project.style_preset:
            raise RuntimeError("style_preset 未设置，不能生成设计稿")
        self._set_project_stage_at_least(project, "design")
        page_count = self.session.scalar(select(func.count(ProjectPage.id)).where(ProjectPage.project_id == project.id)) or 1
        brief = self._get_current_brief(page)
        plan = draft.content_plan_json if isinstance(draft.content_plan_json, dict) else {}
        base_scene = draft.layout_plan_json if isinstance(draft.layout_plan_json, dict) else {}
        visual_plan = draft.visual_plan_json if isinstance(draft.visual_plan_json, dict) else propose_visual_plan(plan, page_images=page.page_images_json or [], page_role=page.page_role)
        svg = self.generator.generate_design_svg(
            draft_svg=draft.draft_svg_markup,
            style_pack_id=project.style_preset,
            background_asset_path=project.background_asset_path,
            content_plan=plan,
            layout_plan=base_scene,
            visual_plan=visual_plan,
            page_images=page.page_images_json or [],
            frozen_card=self._frozen_style_card(project) or get_library_card(self.session, str(project.style_preset or "")),
            chrome={
                "page_title": str(plan.get("title") or (brief.title if brief else page.page_code)),
                "page_index": page.sort_order,
                "page_count": int(page_count),
                "page_role": page.page_role,
            },
        )
        version_no = (self.session.scalar(select(func.count(DesignVersion.id)).where(DesignVersion.page_id == page.id)) or 0) + 1
        extracted = extract_page_scene(svg, content_plan=plan, base_scene=base_scene)
        report = evaluate_page_quality(
            svg,
            stage="design",
            content_plan=plan,
            layout_plan=base_scene if base_scene.get("nodes") else extracted,
            visual_plan=visual_plan,
            run_export_preflight=True,
        )
        design = DesignVersion(
            project_id=project.id,
            page_id=page.id,
            version_no=version_no,
            status="ready",
            draft_version_id=draft.id,
            style_pack_id=project.style_preset,
            background_asset_path=project.background_asset_path,
            design_svg_markup=svg,
            visual_plan_json=visual_plan,
            layout_plan_json=extracted,
        )
        apply_report_to_version(design, report, svg_markup=svg)
        self.session.add(design)
        self.session.flush()
        page.current_design_version_id = design.id
        if report.get("hard_fail"):
            page.design_status = "failed"
            self._update_artifact_staleness(page)
            assert_quality_ready(report, stage="design")
        page.design_status = "ready"
        self._update_artifact_staleness(page)
        return {"design_version_id": design.id, "quality_report": report}

    def run_page_action_flow(
        self,
        project_id: str,
        page_id: str,
        action_type: str,
        agent_run_id: str,
        replace_existing: bool,
    ) -> None:
        from app.services.orchestrator import AgentRunRecorder
        project = self._require_project(project_id)
        page = self._require_page(project_id, page_id)
        latest_instruction = ""
        run = AgentRunRecorder(
            service=self,
            project=project,
            stage=project.current_stage,
            scope_type="page",
            target_page_id=page.id,
            title=f"执行页面动作：{action_type}",
            origin="button",
            message_id=None,
            agent_run_id=agent_run_id,
        )
        run.start()
        run.set_router_decision(
            self._build_system_decision(
                scope_type="page",
                target_stage=run.stage,
                target_page_id=page.id,
                action_type=action_type,
                reason="用户通过按钮直接触发页面动作。",
                execution_plan=self._page_action_execution_plan(action_type),
            )
        )
        result_snapshot: dict[str, Any] = {"page_id": page.id}
        step_name = action_type
        try:
            if action_type == "page_generate_search_queries":
                step_name = "生成页面搜索词"
                run.step_started(action_type, step_name, "把当前页结构化需求翻译成搜索词集合。")
                result_snapshot = {"queries": self._run_page_query_generation(project=project, page=page, latest_instruction=latest_instruction)}
                run.data_updated(
                    {
                        "entity": "page",
                        "page_id": page.id,
                        "update_kind": "search_queries",
                        "query_count": len(page.page_search_queries_json),
                    }
                )
                run.step_completed(action_type, step_name, {"query_count": len(page.page_search_queries_json)})
                coverage = search_coverage(page.page_search_queries_json or [], [])
                message = f"已为当前页生成 {len(page.page_search_queries_json)} 条搜索词，{coverage['dimension_count']} 组维度。下一步可以直接执行正式搜索。"
            elif action_type in {"page_search_run", "page_search_refresh"}:
                step_name = "执行页面搜索"
                result_snapshot = self._run_page_search(
                    project=project,
                    page=page,
                    latest_instruction=latest_instruction,
                    replace_existing=replace_existing or action_type == "page_search_refresh",
                    run=run,
                )
                message = (
                    f"第 {result_snapshot.get('search_round', 1)} 轮检索完成，"
                    f"本轮 {result_snapshot.get('latest_round_hits', result_snapshot.get('result_count', 0))} 条，"
                    f"累计 {result_snapshot.get('result_count', 0)} 条，"
                    f"{result_snapshot.get('dimension_count', 0)} 组维度。"
                    "系统没有自动生成 summary，你可以继续手动生成 summary。"
                )
            elif action_type == "page_summary_generate":
                step_name = "生成页面 summary"
                run.step_started(action_type, step_name, "只从当前页资料池内召回并生成摘要。")
                result_snapshot = self._run_page_summary(project=project, page=page, latest_instruction=latest_instruction)
                run.data_updated(
                    {
                        "entity": "page",
                        "page_id": page.id,
                        "update_kind": "summary",
                        "summary_length": result_snapshot.get("summary_length", 0),
                    }
                )
                run.step_completed(action_type, step_name, result_snapshot)
                message = "当前页 summary 已生成。系统没有自动继续出策划稿。"
            elif action_type == "page_draft_generate":
                step_name = "生成页面策划稿"
                run.step_started(action_type, step_name, "基于当前页 summary 生成 draft。")
                result_snapshot = self._run_page_draft(project=project, page=page, latest_instruction=latest_instruction)
                run.data_updated(
                    {
                        "entity": "page",
                        "page_id": page.id,
                        "update_kind": "draft",
                        "draft_version_id": result_snapshot.get("draft_version_id"),
                    }
                )
                run.step_completed(action_type, step_name, result_snapshot)
                message = "当前页策划稿已生成。"
            elif action_type == "page_design_generate":
                step_name = "生成页面设计稿"
                run.step_started(action_type, step_name, "基于当前页 draft 和 style_preset 生成 design。")
                result_snapshot = self._run_page_design(project=project, page=page)
                run.data_updated(
                    {
                        "entity": "page",
                        "page_id": page.id,
                        "update_kind": "design",
                        "design_version_id": result_snapshot.get("design_version_id"),
                    }
                )
                run.step_completed(action_type, step_name, result_snapshot)
                message = "当前页设计稿已生成。"
            else:
                raise RuntimeError(f"不支持的页面动作: {action_type}")
        except Exception as exc:
            self._mark_page_stage_failed(page, action_type)
            self._update_artifact_staleness(page)
            self.session.commit()
            self._finalize_run_failure(
                run=run,
                step_code=action_type,
                step_name=step_name,
                exc=exc,
                content_md=f"{step_name}失败。错误已经保留在当前动作卡片中。",
                result_snapshot={"page_id": page.id},
            )
            raise

        run.set_recommendations(self._default_recommendations_for_action(action_type))
        self._persist_agent_message(run=run, content_md=message, result_snapshot=result_snapshot)
        run.complete()

    def _refresh_page_images(self, project: Project, page: ProjectPage) -> list[dict[str, Any]]:
        extra = []
        if project.requirement_form:
            extra = project.requirement_form.init_search_results_json or []
        return materialize_page_images(
            project_id=project.id,
            page_id=page.id,
            search_results=page.page_search_results_json or [],
            extra_results=extra,
        )

    def _ensure_page_images(self, project: Project, page: ProjectPage) -> None:
        if page.page_images_json:
            return
        page.page_images_json = self._refresh_page_images(project, page)

