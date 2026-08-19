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


class RouterFlowMixin:
    def run_message_flow(self, message_id: str) -> None:
        from app.services.orchestrator import AgentRunRecorder, PROJECT_STAGE_ORDER, _GATED_EXECUTE_ACTIONS
        message = self.session.get(ProjectMessage, message_id)
        if not message:
            return
        project = self._require_project(message.project_id)
        page = self._require_page(project.id, message.target_page_id) if message.target_page_id else None
        ui_surface = str(message.structured_payload_json.get("ui_surface") or project.current_stage)
        run = AgentRunRecorder(
            service=self,
            project=project,
            stage=ui_surface if ui_surface in PROJECT_STAGE_ORDER else project.current_stage,
            scope_type=message.scope_type,
            target_page_id=message.target_page_id,
            title="处理聊天动作",
            origin="message",
            message_id=message.id,
        )
        run.start()
        try:
            requirement_form = self._require_requirement_form(project)
            router_payload = {
                "project_id": project.id,
                "project_stage": project.current_stage,
                "ui_surface": ui_surface,
                "latest_user_message": message.content_md,
                "recent_messages": self._recent_messages(project.id),
                "project_request": project.request_text,
                "workflow_constraints": project.workflow_constraints_json.get("items", []),
                "fixed_fields": self._build_fixed_field_values(project, requirement_form),
                "project_level_status_summary": self._build_project_level_status_summary(project),
                "outline_state_snapshot": self._build_outline_snapshot(project.id),
                "page_context": self._build_page_context_for_router(page),
                "default_scope_type": message.scope_type,
            }
            decision = self.generator.route_workspace_intent(router_payload=router_payload)
            run.set_router_decision(decision)
            run.set_recommendations(decision.get("next_recommendations", []))

            if decision["needs_clarification"] or decision["action_type"] == "reject":
                self._persist_agent_message(
                    run=run,
                    content_md=self._build_rejection_message(decision),
                    result_snapshot={"missing_data": decision.get("missing_data", [])},
                )
                run.complete("rejected")
                return

            action_type = decision["action_type"]
            if decision.get("requires_confirmation"):
                self._persist_agent_message(
                    run=run,
                    content_md=decision.get("reason") or f"动作 `{action_type}` 需要你确认后才会执行。",
                    result_snapshot={"action_type": action_type, "requires_confirmation": True},
                )
                run.complete()
                return
            if action_type in _GATED_EXECUTE_ACTIONS and not decision.get("should_execute"):
                self._persist_agent_message(
                    run=run,
                    content_md=decision.get("reason") or f"当前只给出建议，不会自动执行 `{action_type}`。",
                    result_snapshot={"action_type": action_type, "should_execute": False},
                )
                run.complete()
                return
            if action_type == "init_refresh_search":
                self.run_bootstrap_flow(project.id)
                run.complete()
                return

            if action_type == "init_confirm_to_outline":
                if ui_surface != "init" or project.current_stage != "init":
                    self._persist_agent_message(
                        run=run,
                        content_md="当前不在初始化界面，不能重新确认需求并生成大纲。",
                        result_snapshot={
                            "rejected_action": action_type,
                            "ui_surface": ui_surface,
                            "current_stage": project.current_stage,
                        },
                    )
                    run.complete("rejected")
                    return
                try:
                    self.confirm_requirements(project.id, note_md=message.content_md)
                    self._persist_agent_message(
                        run=run,
                        content_md="初始化信息已满足要求，系统开始生成大纲。大纲完成后会直接进入搜索工作台。",
                        result_snapshot={"current_stage": "outline"},
                    )
                    run.complete()
                except Exception as exc:
                    self._finalize_run_failure(
                        run=run,
                        step_code=action_type,
                        step_name="确认初始化并生成大纲",
                        exc=exc,
                        content_md="确认初始化失败。错误已经保留在当前动作卡片中。",
                        result_snapshot={"current_stage": project.current_stage},
                    )
                return

            if action_type in {"project_batch_search", "project_batch_summary", "project_batch_draft", "project_batch_design"}:
                tasks = enqueue_batch_action(
                    self.session,
                    project_id=project.id,
                    action_type=action_type,
                    agent_run_id=run.agent_run_id,
                )
                self.session.flush()
                self._persist_agent_message(
                    run=run,
                    content_md=f"已将 `{action_type}` 拆成 {len(tasks)} 个页级任务并发执行；已可用且未过期的页已跳过。",
                    result_snapshot={"queued": len(tasks), "action_type": action_type},
                )
                run.complete()
                wake_scheduler()
                return

            if action_type == "outline_generate":
                self.run_outline_flow(project.id)
                self._persist_agent_message(
                    run=run,
                    content_md="已按你的要求生成大纲并进入搜索工作台。",
                    result_snapshot={"current_stage": "search", "action_type": action_type},
                )
                run.complete()
                return

            if action_type in {"init_add_question", "init_update_question", "init_delete_question", "init_update_answer"}:
                result_snapshot: dict[str, Any]
                try:
                    if action_type == "init_update_answer":
                        run.step_started(action_type, "更新初始化答案", "根据聊天消息更新结构化答案，不自动重搜。")
                        answer_patch = decision["data_updates"].get("answer_patch")
                        if not isinstance(answer_patch, dict):
                            raise RuntimeError("router 没有返回可执行的 answer_patch")
                        result_snapshot = self._apply_init_answer_patch(project, requirement_form, answer_patch)
                        run.data_updated(
                            {
                                "entity": "requirement_form",
                                "update_kind": "init_answers",
                            }
                        )
                        run.step_completed(action_type, "更新初始化答案", result_snapshot)
                        content_md = "初始化答案已更新。系统没有自动重跑搜索，你可以继续修改，或明确要求重跑项目级搜索。"
                    else:
                        run.step_started("init_retrieval", "检索 init_corpus 证据", "问题增改前先从 init_corpus 做检索。")
                        init_collection = self.research.get_or_create_init_collection(project)
                        retrieval_query_plan = self.research.build_query_plan(
                            scope_type="project",
                            session_role="init_question_refine",
                            request_text=project.request_text,
                            project_stage="init",
                            project_title=project.title,
                            fixed_fields=self._build_fixed_field_values(project, requirement_form),
                            answers=requirement_form.answers_json or {},
                            latest_instruction=message.content_md,
                        )
                        refine_session = self.research.create_session(
                            project_id=project.id,
                            page_id=None,
                            scope_type="project",
                            session_role="init_question_refine",
                            research_goal="为初始化问题增删改提供项目级证据。",
                            query_plan=retrieval_query_plan,
                            context_snapshot={"latest_instruction": message.content_md},
                        )
                        evidence = self.research.retrieve_for_collection(
                            project=project,
                            collection=init_collection,
                            research_session=refine_session,
                            query_plan=retrieval_query_plan,
                            limit=200,
                        )
                        refine_session.status = "completed" if evidence else "failed"
                        run.step_completed("init_retrieval", "检索 init_corpus 证据", {"citation_count": len(evidence)})
                        if not evidence:
                            raise RuntimeError("init_corpus 没有可用证据，不能修订问题")
                        package = self.generator.refine_init_questions_with_retrieval(
                            project_title=project.title,
                            request_text=project.request_text,
                            latest_instruction=message.content_md,
                            current_questions=requirement_form.ai_questions_json or [],
                            current_page_count_options=requirement_form.page_count_options_json or [],
                            init_corpus_evidence=evidence,
                            question_patch=decision["data_updates"].get("question_patch")
                            if isinstance(decision["data_updates"].get("question_patch"), dict)
                            else None,
                        )
                        requirement_form.ai_questions_json = package["ai_questions"]
                        if package.get("page_count_options"):
                            requirement_form.page_count_options_json = package["page_count_options"]
                        result_snapshot = {
                            "question_count": len(requirement_form.ai_questions_json),
                            "citation_count": len(evidence),
                        }
                        run.data_updated(
                            {
                                "entity": "requirement_form",
                                "update_kind": "init_questions",
                            }
                        )
                        run.step_completed(action_type, "按证据修订初始化问题", result_snapshot)
                        content_md = "已结合 init_corpus 证据更新问题集合。当前不会自动重跑搜索；如果要基于新问题重新看资料，请明确要求重跑项目级搜索。"
                    self._persist_agent_message(run=run, content_md=content_md, result_snapshot=result_snapshot)
                    run.complete()
                except Exception as exc:
                    self._finalize_run_failure(
                        run=run,
                        step_code=action_type,
                        step_name="更新初始化需求",
                        exc=exc,
                        content_md="初始化需求更新失败。错误已经保留在当前动作卡片中。",
                    )
                return

            if page is None:
                self._persist_agent_message(
                    run=run,
                    content_md="当前动作需要明确页面上下文，但这条消息没有绑定目标页。",
                    result_snapshot={"missing_data": ["target_page_id"]},
                )
                run.complete("rejected")
                return

            result_snapshot: dict[str, Any] = {"page_id": page.id}
            content_md = ""
            step_name = action_type
            try:
                if action_type == "page_update_outline_in_search":
                    step_name = "更新页面结构"
                    run.step_started(action_type, step_name, "修改标题、要点和章节归属，并只标记下游 stale。")
                    page_patch = self.generator.generate_page_outline_patch(
                        latest_user_message=message.content_md,
                        page_id=page.id,
                        page_title=self._get_current_brief(page).title if self._get_current_brief(page) else "",
                        page_bullets=self._get_current_brief(page).content_outline_json if self._get_current_brief(page) else [],
                        page_section_title=page.part_title,
                        outline_full_snapshot=self._build_outline_snapshot(project.id),
                    )
                    decision["data_updates"]["page_patch"] = page_patch
                    self.patch_page_outline(project.id, page.id, page_patch)
                    run.data_updated(
                        {
                            "entity": "page",
                            "page_id": page.id,
                            "update_kind": "outline",
                        }
                    )
                    run.step_completed(action_type, step_name, page_patch)
                    result_snapshot = page_patch
                    content_md = f"当前页结构已更新：{page_patch.get('change_summary') or '标题和要点已写回数据库'}。系统没有自动重搜，相关下游产物已标记为 stale。"
                elif action_type == "page_generate_search_queries":
                    step_name = "生成页面搜索词"
                    run.step_started(action_type, step_name, "只重算当前页搜索词集合。")
                    result_snapshot = {"queries": self._run_page_query_generation(project=project, page=page, latest_instruction=message.content_md)}
                    run.data_updated(
                        {
                            "entity": "page",
                            "page_id": page.id,
                            "update_kind": "search_queries",
                            "query_count": len(page.page_search_queries_json),
                        }
                    )
                    run.step_completed(action_type, step_name, {"query_count": len(page.page_search_queries_json)})
                    content_md = f"已为当前页生成 {len(page.page_search_queries_json)} 条搜索词。"
                elif action_type in {"page_search_run", "page_search_refresh"}:
                    step_name = "执行页面搜索"
                    result_snapshot = self._run_page_search(
                        project=project,
                        page=page,
                        latest_instruction=message.content_md,
                        replace_existing=action_type == "page_search_refresh",
                        run=run,
                    )
                    content_md = "当前页资料池已更新。系统没有自动继续生成 summary。"
                elif action_type == "page_summary_generate":
                    step_name = "生成页面 summary"
                    run.step_started(action_type, step_name, "只从当前页资料池生成摘要。")
                    result_snapshot = self._run_page_summary(project=project, page=page, latest_instruction=message.content_md)
                    run.data_updated(
                        {
                            "entity": "page",
                            "page_id": page.id,
                            "update_kind": "summary",
                            "summary_length": result_snapshot.get("summary_length", 0),
                        }
                    )
                    run.step_completed(action_type, step_name, result_snapshot)
                    content_md = "当前页 summary 已生成。"
                elif action_type == "page_summary_edit":
                    step_name = "编辑页面 summary"
                    run.step_started(action_type, step_name, "根据用户要求改写当前页 summary，并标记 draft/design stale。")
                    patch = self.generator.generate_summary_patch(
                        latest_user_message=message.content_md,
                        page_title=self._get_current_brief(page).title if self._get_current_brief(page) else "",
                        page_bullets=self._get_current_brief(page).content_outline_json if self._get_current_brief(page) else [],
                        current_summary_md=page.page_summary_md,
                    )
                    decision["data_updates"]["summary_patch"] = patch
                    self.patch_page_summary(project.id, page.id, patch["summary_md"])
                    run.data_updated(
                        {
                            "entity": "page",
                            "page_id": page.id,
                            "update_kind": "summary",
                            "summary_length": len(patch["summary_md"]),
                        }
                    )
                    run.step_completed(action_type, step_name, {"summary_length": len(patch["summary_md"])})
                    result_snapshot = patch
                    content_md = "当前页 summary 已按你的要求改写，draft/design 已标记为 stale。"
                elif action_type == "page_draft_generate":
                    step_name = "生成页面初稿"
                    run.step_started(action_type, step_name, "基于当前页 summary 生成 draft。")
                    result_snapshot = self._run_page_draft(project=project, page=page, latest_instruction=message.content_md)
                    run.data_updated(
                        {
                            "entity": "page",
                            "page_id": page.id,
                            "update_kind": "draft",
                            "draft_version_id": result_snapshot.get("draft_version_id"),
                        }
                    )
                    run.step_completed(action_type, step_name, result_snapshot)
                    content_md = "当前页初稿已生成。"
                elif action_type == "page_design_generate":
                    step_name = "生成页面设计稿"
                    run.step_started(action_type, step_name, "基于当前页 draft 生成 design。")
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
                    content_md = "当前页设计稿已生成。"
                else:
                    self._persist_agent_message(
                        run=run,
                        content_md="这条消息已经被识别到动作类型，但当前后端还没有对应执行器。",
                        result_snapshot={"action_type": action_type},
                    )
                    run.complete("rejected")
                    return
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
                return

            self._persist_agent_message(run=run, content_md=content_md, result_snapshot=result_snapshot)
            run.complete()
        except Exception as exc:
            self._finalize_run_failure(
                run=run,
                step_code="route_workspace_intent",
                step_name="判断用户意图",
                exc=exc,
                content_md="处理聊天动作失败。错误已经保留在当前动作卡片中。",
                result_snapshot={
                    "message_id": message.id,
                    "page_id": page.id if page else None,
                    "ui_surface": ui_surface,
                },
            )

