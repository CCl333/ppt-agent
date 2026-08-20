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


class InitFlowMixin:
    def run_bootstrap_flow(self, project_id: str) -> None:
        from app.services.orchestrator import AgentRunRecorder
        project = self._require_project(project_id)
        requirement_form = self._require_requirement_form(project)
        run = AgentRunRecorder(
            service=self,
            project=project,
            stage="init",
            scope_type="project",
            target_page_id=None,
            title="初始化资料准备",
            origin="system",
        )
        run.start()
        run.set_router_decision(
            self._build_system_decision(
                scope_type="project",
                target_stage="init",
                target_page_id=None,
                action_type="init_refresh_search",
                reason="项目创建后自动执行初始化搜索、建库和问题生成。",
                execution_plan=[
                    {"step_code": "I2", "step_name": "生成初始化搜索词", "reason": "把原始需求翻译成项目级查询。"},
                    {"step_code": "I3", "step_name": "执行 Bocha 搜索", "reason": "获取首轮搜索摘要。"},
                    {"step_code": "I4", "step_name": "生成页数推荐和问题", "reason": "基于搜索摘要快速产出固定项与问题。"},
                ],
            )
        )
        current_step_code = "I2"
        current_step_name = "生成初始化搜索词"
        try:
            run.step_started("I2", "生成初始化搜索词", "把原始需求翻译成项目级查询。")
            query_plan = self.research.build_query_plan(
                scope_type="project",
                session_role="init_discovery",
                request_text=project.request_text,
                project_stage="init",
                project_title=project.title,
                fixed_fields=self._build_fixed_field_values(project, requirement_form),
                answers=requirement_form.answers_json or {},
                latest_instruction=requirement_form.latest_instruction or "",
            )
            requirement_form.init_search_queries_json = query_plan
            run.data_updated(
                {
                    "entity": "requirement_form",
                    "update_kind": "init_queries",
                    "query_count": len(query_plan),
                }
            )
            run.step_completed("I2", "生成初始化搜索词", {"query_count": len(query_plan)})

            current_step_code = "I3"
            current_step_name = "执行 Bocha 搜索"
            run.step_started("I3", "执行 Bocha 搜索", "获取首轮搜索摘要。")

            def on_init_query_completed(payload: dict[str, Any]) -> None:
                requirement_form.init_search_results_json = payload["items"]
                run.data_updated(
                    {
                        "entity": "requirement_form",
                        "update_kind": "init_search_results",
                        "query_index": payload["query_index"],
                        "query_total": payload["query_total"],
                        "result_count": payload["result_count"],
                    }
                )
                run.step_progress(
                    "I3",
                    "执行 Bocha 搜索",
                    progress={
                        "current": payload["query_index"],
                        "total": payload["query_total"],
                        "label": f"已完成 {payload['query_index']}/{payload['query_total']} 条查询",
                    },
                    result={"result_count": payload["result_count"]},
                )

            search_results = self.research.search_query_summaries(
                query_plan,
                limit_per_query=4,
                on_query_completed=on_init_query_completed,
            )
            requirement_form.init_search_results_json = self.research.build_search_result_cards(search_results)
            run.step_completed("I3", "执行 Bocha 搜索", {"result_count": len(search_results)})

            current_step_code = "I4"
            current_step_name = "生成页数推荐和问题"
            run.step_started("I4", "生成页数推荐和问题", "基于搜索摘要快速产出固定项与问题。")
            package = self.generator.generate_init_fast_questions(
                project_title=project.title,
                request_text=project.request_text,
                init_search_results=search_results,
            )
            requirement_form.page_count_options_json = package["page_count_options"]
            requirement_form.ai_questions_json = package["ai_questions"]
            requirement_form.status = "ready"
            requirement_form.suggested_actions_json = [
                {
                    "code": "fill_required_fields",
                    "label": "补全固定项和问题答案",
                    "reason": "先完成页数、风格和问题答案，再进入大纲。",
                },
                {
                    "code": "refresh_init_search",
                    "label": "补充约束后重跑项目级搜索",
                    "reason": "如果首轮资料跑偏，可以要求重新搜索。",
                },
            ]
            run.data_updated(
                {
                    "entity": "requirement_form",
                    "update_kind": "init_questions",
                    "question_count": len(requirement_form.ai_questions_json),
                }
            )
            run.step_completed("I4", "生成页数推荐和问题", {"question_count": len(requirement_form.ai_questions_json)})
            run.set_recommendations(requirement_form.suggested_actions_json)
            self._persist_agent_message(
                run=run,
                content_md="初始化搜索摘要已准备完成。现在可以填写页数、风格和补充问题答案；如果资料方向不对，也可以直接要求重跑项目级搜索。",
                result_snapshot={"requirement_form_status": requirement_form.status, "result_count": len(search_results)},
            )
            run.complete()
        except Exception as exc:
            requirement_form.status = "failed"
            requirement_form.suggested_actions_json = [
                {
                    "code": "refresh_init_search",
                    "label": "重试初始化搜索",
                    "reason": "初始化资料准备失败后，可直接重跑搜索与建库。已成功的检索结果会尽量复用缓存。",
                }
            ]
            run.set_recommendations(requirement_form.suggested_actions_json)
            self._finalize_run_failure(
                run=run,
                step_code=current_step_code,
                step_name=current_step_name,
                exc=exc,
                content_md="初始化资料准备失败。可以点击「重试初始化搜索」继续；已成功的检索结果会尽量复用缓存。",
                result_snapshot={"requirement_form_status": requirement_form.status},
            )
            raise

