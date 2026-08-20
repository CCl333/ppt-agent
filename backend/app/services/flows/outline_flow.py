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
            title="生成大纲并等待确认",
            origin="system",
        )
        run.start()
        run.set_router_decision(
            self._build_system_decision(
                scope_type="project",
                target_stage="outline",
                target_page_id=None,
                action_type="outline_generate",
                reason="固定项齐备后生成大纲，确认后再进入资料阶段。",
                execution_plan=[
                    {"step_code": "O2", "step_name": "整理搜索摘要", "reason": "大纲使用首轮搜索摘要。"},
                    {"step_code": "O3", "step_name": "生成大纲", "reason": "根据需求、固定项和背景调研摘要生成章节与页面。"},
                    {"step_code": "O4", "step_name": "落库页面实体", "reason": "创建页面和首个版本。"},
                    {"step_code": "O5", "step_name": "等待确认大纲", "reason": "确认后再按页找资料。"},
                ],
            )
        )
        fixed_fields = self._build_fixed_field_values(project, requirement_form)
        page_count_target = self._coerce_page_count(fixed_fields.get("page_count_target"), project.page_count_target) or 10
        project.page_count_target = page_count_target
        project.style_preset = str(fixed_fields.get("style_preset") or project.style_preset or "")
        current_step_code = "O2"
        current_step_name = "整理搜索摘要"
        try:
            run.step_started("O2", "整理搜索摘要", "大纲使用首轮搜索摘要，不再抓取全文。")
            evidence = self.research.search_results_as_evidence(requirement_form.init_search_results_json)
            if not evidence:
                raise RuntimeError("首轮搜索结果为空，不能生成大纲")
            run.step_completed("O2", "整理搜索摘要", {"citation_count": len(evidence)})

            current_step_code = "O3"
            current_step_name = "生成大纲"
            run.step_started("O3", "生成大纲", "根据需求、固定项和背景调研摘要生成章节与页面。")
            outline_payload = self.generator.generate_outline(
                project_title=project.title,
                request_text=project.request_text,
                page_count_target=page_count_target,
                style_preset=project.style_preset or "",
                background_asset_path=project.background_asset_path,
                answers=requirement_form.answers_json or {},
                context_digest=evidence,
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
            project.current_stage = "outline"
            run.data_updated(
                {
                    "entity": "project",
                    "update_kind": "outline",
                    "page_count": len(self.list_pages(project.id)),
                }
            )
            run.step_completed("O4", "落库页面实体", {"page_count": len(self.list_pages(project.id))})

            current_step_code = "O5"
            current_step_name = "等待确认大纲"
            run.step_started("O5", "等待确认大纲", "大纲已生成，确认后再按页找资料。")
            run.status_changed({"current_stage": "outline"})
            run.step_completed("O5", "等待确认大纲", {"current_stage": "outline"})
            run.set_recommendations(
                [
                    {
                        "code": "outline_confirm_to_search",
                        "label": "确认大纲，开始按页找资料",
                        "reason": "先改标题/要点或增删页，确认后再进入资料阶段。",
                    }
                ]
            )
            self._persist_agent_message(
                run=run,
                content_md="大纲已生成。请先确认章节和页面结构，确认后再开始按页找资料。",
                result_snapshot={"current_stage": "outline"},
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

