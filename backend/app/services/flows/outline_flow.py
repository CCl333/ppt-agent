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
from app.services.tasks import enqueue_batch_action, wake_scheduler


class OutlineFlowMixin:
    def run_outline_flow(self, project_id: str) -> None:
        from app.services.orchestrator import AgentRunRecorder
        project = self._require_project(project_id)
        requirement_form = self._require_requirement_form(project)
        self._validate_requirement_form(project, requirement_form)
        run = AgentRunRecorder(
            service=self,
            project=project,
            stage="outline",
            scope_type="project",
            target_page_id=None,
            title="生成大纲并切换到搜索工作台",
            origin="system",
        )
        run.start()
        run.set_router_decision(
            self._build_system_decision(
                scope_type="project",
                target_stage="outline",
                target_page_id=None,
                action_type="outline_generate",
                reason="固定项齐备后生成大纲，并直接进入搜索工作台。",
                execution_plan=[
                    {"step_code": "O2", "step_name": "从 init_corpus 检索证据", "reason": "大纲只能使用项目级资料池。"},
                    {"step_code": "O3", "step_name": "生成大纲", "reason": "根据需求、固定项和证据生成章节与页面。"},
                    {"step_code": "O4", "step_name": "落库页面实体", "reason": "创建页面和首个版本。"},
                    {"step_code": "O5", "step_name": "切换到搜索页", "reason": "完成后直接进入搜索工作台，不自动搜索。"},
                ],
            )
        )
        fixed_fields = self._build_fixed_field_values(project, requirement_form)
        page_count_target = self._coerce_page_count(fixed_fields.get("page_count_target"), project.page_count_target) or 10
        project.page_count_target = page_count_target
        project.style_preset = str(fixed_fields.get("style_preset") or project.style_preset or "")
        current_step_code = "O2"
        current_step_name = "从 init_corpus 检索证据"
        try:
            run.step_started("O2", "从 init_corpus 检索证据", "大纲只能使用项目级资料池。")
            init_collection = self.research.get_or_create_init_collection(project)
            evidence_query_plan = self.research.build_query_plan(
                scope_type="project",
                session_role="outline_generate",
                request_text=project.request_text,
                project_stage="outline",
                project_title=project.title,
                fixed_fields=fixed_fields,
                answers=requirement_form.answers_json or {},
                latest_instruction=requirement_form.latest_instruction or "",
            )
            evidence_session = self.research.create_session(
                project_id=project.id,
                page_id=None,
                scope_type="project",
                session_role="outline_generate",
                research_goal="为大纲生成筛选项目级证据。",
                query_plan=evidence_query_plan,
                context_snapshot={"request_text": project.request_text, "fixed_fields": fixed_fields},
            )
            evidence = self.research.retrieve_for_collection(
                project=project,
                collection=init_collection,
                research_session=evidence_session,
                query_plan=evidence_query_plan,
                limit=200,
            )
            evidence_session.status = "completed" if evidence else "failed"
            run.step_completed("O2", "从 init_corpus 检索证据", {"citation_count": len(evidence)})

            current_step_code = "O3"
            current_step_name = "生成大纲"
            run.step_started("O3", "生成大纲", "根据需求、固定项和证据生成章节与页面。")
            outline_payload = self.generator.generate_outline(
                project_title=project.title,
                request_text=project.request_text,
                page_count_target=page_count_target,
                style_preset=project.style_preset or "",
                background_asset_path=project.background_asset_path,
                answers=requirement_form.answers_json or {},
                init_corpus_evidence=evidence,
            )
            run.step_completed("O3", "生成大纲", {"part_count": len(outline_payload["ppt_outline"].get("parts", []))})

            current_step_code = "O4"
            current_step_name = "落库页面实体"
            run.step_started("O4", "落库页面实体", "创建页面和首个版本。")
            self._rebuild_pages_from_outline(project, outline_payload)
            outline = OutlineVersion(
                project_id=project.id,
                version_no=(self.session.scalar(select(func.count(OutlineVersion.id)).where(OutlineVersion.project_id == project.id)) or 0) + 1,
                status="ready",
                outline_json=outline_payload,
            )
            self.session.add(outline)
            project.current_stage = "search"
            run.data_updated(
                {
                    "entity": "project",
                    "update_kind": "outline",
                    "page_count": len(self.list_pages(project.id)),
                }
            )
            run.step_completed("O4", "落库页面实体", {"page_count": len(self.list_pages(project.id))})

            current_step_code = "O5"
            current_step_name = "切换到搜索页"
            run.step_started("O5", "切换到搜索页", "完成后直接进入搜索工作台，不自动搜索。")
            run.status_changed({"current_stage": "search"})
            run.step_completed("O5", "切换到搜索页", {"current_stage": "search"})
            run.set_recommendations(
                [
                    {
                        "code": "page_generate_search_queries",
                        "label": "先为当前页生成搜索词",
                        "reason": "进入搜索页后默认不自动搜索，先看当前页职责是否正确。",
                    },
                    {
                        "code": "project_batch_search",
                        "label": "需要时再批量搜索",
                        "reason": "只有用户明确要求批量执行时才跑全项目。",
                    },
                ]
            )
            self._persist_agent_message(
                run=run,
                content_md="大纲生成完成，系统已进入搜索工作台。当前没有自动搜索任何页面，你可以先修改当前页标题和要点，再决定是否生成搜索词或执行搜索。",
                result_snapshot={"current_stage": "search"},
            )
            run.complete()
        except Exception as exc:
            self._finalize_run_failure(
                run=run,
                step_code=current_step_code,
                step_name=current_step_name,
                exc=exc,
                content_md="大纲生成失败。错误已经保留在当前动作卡片中。",
                result_snapshot={"current_stage": project.current_stage},
            )
            raise

    def _rebuild_pages_from_outline(self, project: Project, outline_payload: dict[str, Any]) -> None:
        for page in self.session.scalars(select(ProjectPage).where(ProjectPage.project_id == project.id)):
            self.session.delete(page)
        self.session.flush()

        page_defs: list[tuple[str, str | None, str, list[str]]] = []
        ppt_outline = outline_payload["ppt_outline"]
        page_defs.append(("cover", None, ppt_outline["cover"]["title"], ppt_outline["cover"].get("content", [])))
        page_defs.append(("toc", None, ppt_outline["table_of_contents"]["title"], ppt_outline["table_of_contents"].get("content", [])))
        for section in ppt_outline.get("parts", []):
            for page in section.get("pages", []):
                page_defs.append(("content", section["part_title"], page["title"], page.get("content", [])))
        page_defs.append(("end", None, ppt_outline["end_page"]["title"], ppt_outline["end_page"].get("content", [])))

        for sort_order, (role, part_title, title, content) in enumerate(page_defs, start=1):
            page = ProjectPage(
                project_id=project.id,
                page_code=f"page-{sort_order:02d}",
                page_role=role,
                part_title=part_title,
                sort_order=sort_order,
                outline_status="ready",
                search_status="confirmed" if role != "content" else "empty",
                summary_status="confirmed" if role != "content" else "empty",
                draft_status="empty",
                design_status="empty",
                page_summary_md="；".join(content[:2]) or title if role != "content" else "",
                page_summary_citations_json=[],
                page_search_queries_json=[],
                page_search_results_json=[],
                page_corpus_digest_json={},
                artifact_staleness_json={},
            )
            self.session.add(page)
            self.session.flush()
            self._new_page_brief_version(
                page=page,
                title=title,
                content_outline=content,
                section_title=part_title,
            )
            self._update_artifact_staleness(page)

