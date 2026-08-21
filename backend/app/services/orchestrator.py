from __future__ import annotations

import json
import re
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import is_sqlite_locked, session_scope
from app.models.base import new_id
from app.models.entities import (
    DesignVersion,
    DraftVersion,
    ExportJob,
    OutlineVersion,
    PageBriefVersion,
    Project,
    ProjectEvent,
    ProjectMessage,
    ProjectPage,
    RequirementForm,
    ResearchSession,
)
from app.services.clarification import CLARIFICATION_CODES, project_title_from_answers
from app.services.content_plan import assert_svg_matches_plan, has_content_plan
from app.services.events import append_event, serialize_event
from app.services.export_name import cover_title_from_outline, resolve_export_stem
from app.services.font_policy import build_font_report
from app.services.page_images import public_catalog
from app.services.search_plan import search_coverage
from app.services.style_cards import (
    get_library_card,
    list_library_cards,
    pack_from_card,
    save_card_to_library,
    serialize_card,
)
from app.services.tasks import (
    cancel_tasks as request_task_cancel,
    enqueue_batch_action,
    enqueue_page_action,
    enqueue_project_task,
    wake_scheduler,
)
from app.services.generation import GenerationService
from app.services.svg import extract_and_validate_svg
from app.services.research import ResearchService
from app.services.flows.batch_flow import BatchFlowMixin
from app.services.flows.init_flow import InitFlowMixin
from app.services.flows.outline_flow import OutlineFlowMixin
from app.services.flows.page_flow import PageFlowMixin
from app.services.flows.router_flow import RouterFlowMixin

PAGE_STATUS_VALUES = {"empty", "ready", "running", "confirmed", "stale", "failed"}
PROJECT_STAGE_ORDER = {
    "init": 0,
    "outline": 1,
    "search": 2,
    "draft": 3,
    "design": 4,
    "export": 5,
}
_GATED_EXECUTE_ACTIONS = {
    "init_refresh_search",
    "outline_generate",
    "page_search_run",
    "page_search_refresh",
    "page_summary_generate",
    "page_draft_generate",
    "page_design_generate",
    "project_batch_search",
    "project_batch_summary",
    "project_batch_draft",
    "project_batch_design",
    "outline_confirm_to_search",
}
WORKFLOW_CONSTRAINTS = [
    {
        "code": "use_env_test_data",
        "label": "使用 .env 测试环境",
        "detail": "当前项目可以直接使用 .env 里的测试环境数据。",
    },
    {
        "code": "db_reset_allowed",
        "label": "允许清库换 schema",
        "detail": "当前没有有价值历史数据，数据库结构变化不需要兼容老版本。",
    },
    {
        "code": "no_meaningless_fallback",
        "label": "禁止无意义 fallback",
        "detail": "不能返回看似成功、实际错误的兜底结果。",
    },
    {
        "code": "remove_dead_code",
        "label": "移除无用代码",
        "detail": "不保留已经失效的旧逻辑和旧分支。",
    },
    {
        "code": "static_ui_reference",
        "label": "参考 static agent UI",
        "detail": "agent 消息展示可参考 static 下已确认的占位样式。",
    },
    {
        "code": "constraints_survive_compression",
        "label": "约束不能被压缩丢失",
        "detail": "多轮消息压缩后也必须保留这些硬约束。",
    },
]

BACKGROUND_MAX_BYTES = 8 * 1024 * 1024
_STORAGE_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass
class AgentRunRecorder:
    service: "PptAgentService"
    project: Project
    stage: str
    scope_type: str
    target_page_id: str | None
    title: str
    origin: str
    message_id: str | None = None
    agent_run_id: str = field(default_factory=new_id)
    router_decision: dict[str, Any] | None = None
    step_results: list[dict[str, Any]] = field(default_factory=list)
    next_recommendations: list[dict[str, Any]] = field(default_factory=list)

    def _commit(self) -> None:
        self.service.session.commit()

    def start(self) -> None:
        append_event(
            self.service.session,
            project_id=self.project.id,
            event_type="agent.run.started",
            stage=self.stage,
            scope_type=self.scope_type,
            target_page_id=self.target_page_id,
            agent_run_id=self.agent_run_id,
            payload={
                "title": self.title,
                "origin": self.origin,
                "message_id": self.message_id,
            },
        )
        self._commit()

    def set_router_decision(self, decision: dict[str, Any]) -> None:
        self.router_decision = decision
        append_event(
            self.service.session,
            project_id=self.project.id,
            event_type="router.decision",
            stage=self.stage,
            scope_type=self.scope_type,
            target_page_id=self.target_page_id,
            agent_run_id=self.agent_run_id,
            payload=decision,
        )
        self._commit()

    def step_started(self, step_code: str, step_name: str, reason: str) -> None:
        append_event(
            self.service.session,
            project_id=self.project.id,
            event_type="action.step.started",
            stage=self.stage,
            scope_type=self.scope_type,
            target_page_id=self.target_page_id,
            agent_run_id=self.agent_run_id,
            payload={"step_code": step_code, "step_name": step_name, "reason": reason},
        )
        self._commit()

    def step_progress(
        self,
        step_code: str,
        step_name: str,
        *,
        progress: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        append_event(
            self.service.session,
            project_id=self.project.id,
            event_type="action.step.progress",
            stage=self.stage,
            scope_type=self.scope_type,
            target_page_id=self.target_page_id,
            agent_run_id=self.agent_run_id,
            payload={
                "step_code": step_code,
                "step_name": step_name,
                "status": "running",
                "progress": progress or {},
                "result": result or {},
            },
        )
        self._commit()

    def step_completed(self, step_code: str, step_name: str, result: dict[str, Any] | None = None) -> None:
        step_result = {
            "step_code": step_code,
            "step_name": step_name,
            "status": "completed",
            "result": result or {},
        }
        self.step_results.append(step_result)
        append_event(
            self.service.session,
            project_id=self.project.id,
            event_type="action.step.completed",
            stage=self.stage,
            scope_type=self.scope_type,
            target_page_id=self.target_page_id,
            agent_run_id=self.agent_run_id,
            payload=step_result,
        )
        self._commit()

    def step_failed(self, step_code: str, step_name: str, error_message: str) -> None:
        step_result = {
            "step_code": step_code,
            "step_name": step_name,
            "status": "failed",
            "error_message": error_message,
        }
        self.step_results.append(step_result)
        append_event(
            self.service.session,
            project_id=self.project.id,
            event_type="action.step.failed",
            stage=self.stage,
            scope_type=self.scope_type,
            target_page_id=self.target_page_id,
            agent_run_id=self.agent_run_id,
            payload=step_result,
        )
        self._commit()

    def status_changed(self, payload: dict[str, Any]) -> None:
        append_event(
            self.service.session,
            project_id=self.project.id,
            event_type="status.changed",
            stage=self.stage,
            scope_type=self.scope_type,
            target_page_id=self.target_page_id,
            agent_run_id=self.agent_run_id,
            payload=payload,
        )
        self._commit()

    def data_updated(self, payload: dict[str, Any]) -> None:
        append_event(
            self.service.session,
            project_id=self.project.id,
            event_type="workspace.data.updated",
            stage=self.stage,
            scope_type=self.scope_type,
            target_page_id=self.target_page_id,
            agent_run_id=self.agent_run_id,
            payload=payload,
        )
        self._commit()

    def set_recommendations(self, recommendations: list[dict[str, Any]]) -> None:
        self.next_recommendations = recommendations
        append_event(
            self.service.session,
            project_id=self.project.id,
            event_type="recommendations.updated",
            stage=self.stage,
            scope_type=self.scope_type,
            target_page_id=self.target_page_id,
            agent_run_id=self.agent_run_id,
            payload={"next_recommendations": recommendations},
        )
        self._commit()

    def emit_message(self, message_id: str) -> None:
        append_event(
            self.service.session,
            project_id=self.project.id,
            event_type="agent.message",
            stage=self.stage,
            scope_type=self.scope_type,
            target_page_id=self.target_page_id,
            agent_run_id=self.agent_run_id,
            payload={"message_id": message_id},
        )
        self._commit()

    def complete(self, status_text: str = "completed") -> None:
        append_event(
            self.service.session,
            project_id=self.project.id,
            event_type="agent.run.completed",
            stage=self.stage,
            scope_type=self.scope_type,
            target_page_id=self.target_page_id,
            agent_run_id=self.agent_run_id,
            payload={"status": status_text},
        )
        self._commit()


class PptAgentService(InitFlowMixin, OutlineFlowMixin, PageFlowMixin, BatchFlowMixin, RouterFlowMixin):
    def __init__(self, session: Session):
        self.session = session
        self.settings = get_settings()
        self.generator = GenerationService()
        self.research = ResearchService(session)

    def list_projects(self, limit: int = 20) -> list[dict[str, Any]]:
        stmt = select(Project).order_by(Project.updated_at.desc()).limit(limit)
        return [self.serialize_project(item) for item in self.session.scalars(stmt)]

    def delete_project(self, project_id: str) -> dict[str, Any]:
        project = self._require_project(project_id)
        self._purge_project_files(project)
        try:
            request_task_cancel(self.session, project_id=project.id)
            self.session.flush()
        except OperationalError as exc:
            if not is_sqlite_locked(exc):
                raise
            self.session.rollback()
        delay = 0.2
        last_error: OperationalError | None = None
        for _ in range(6):
            try:
                self.session.expunge_all()
                self.session.execute(delete(Project).where(Project.id == project_id))
                self.session.commit()
                return {"status": "deleted", "project_id": project_id}
            except OperationalError as exc:
                last_error = exc
                self.session.rollback()
                if not is_sqlite_locked(exc):
                    raise
                time.sleep(delay)
                delay = min(delay * 1.5, 1.2)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="项目正在处理中，暂时无法删除，请稍后再试。",
        ) from last_error

    def create_project(self, title: str | None, request_text: str) -> dict[str, Any]:
        project = Project(
            title=title or self.generator.generate_project_title(request_text),
            request_text=request_text,
            current_stage="init",
            workflow_constraints_json={"items": WORKFLOW_CONSTRAINTS},
        )
        self.session.add(project)
        self.session.flush()
        requirement_form = RequirementForm(
            project_id=project.id,
            status="running",
            fixed_items_json=self._build_fixed_fields(),
            answers_json={},
            ai_questions_json=[],
            page_count_options_json=[],
            suggested_actions_json=[],
        )
        self.session.add(requirement_form)
        self.session.flush()
        self._add_message(
            project_id=project.id,
            stage="init",
            scope_type="project",
            role="user",
            content_md=request_text,
        )
        append_event(
            self.session,
            project_id=project.id,
            event_type="project.created",
            stage="init",
            scope_type="project",
            payload={"title": project.title},
        )
        enqueue_project_task(self.session, project_id=project.id, task_type="bootstrap")
        self.session.commit()
        wake_scheduler()
        return self.serialize_project(project)

    def retry_bootstrap(self, project_id: str) -> dict[str, Any]:
        project = self._require_project(project_id)
        if project.current_stage != "init":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="当前不在初始化阶段，不能重试初始化搜索")
        form = project.requirement_form
        if form is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="需求单不存在")
        form.status = "running"
        enqueue_project_task(self.session, project_id=project.id, task_type="bootstrap")
        self.session.commit()
        wake_scheduler()
        return self.serialize_project(project)

    def get_project(self, project_id: str) -> dict[str, Any]:
        return self.serialize_project(self._require_project(project_id))

    def list_messages(self, project_id: str) -> list[dict[str, Any]]:
        self._require_project(project_id)
        stmt = (
            select(ProjectMessage)
            .where(ProjectMessage.project_id == project_id)
            .order_by(ProjectMessage.created_at.asc())
        )
        return [self.serialize_message(item) for item in self.session.scalars(stmt)]

    def list_events(self, project_id: str, *, after_id: int = 0, limit: int = 500) -> dict[str, Any]:
        project = self._require_project(project_id)
        stmt = (
            select(ProjectEvent)
            .where(ProjectEvent.project_id == project.id, ProjectEvent.stream_id > after_id)
            .order_by(ProjectEvent.stream_id.asc())
            .limit(min(max(limit, 1), 500))
        )
        events = list(self.session.scalars(stmt))
        return {"items": [serialize_event(item) for item in events]}

    def create_message(
        self,
        *,
        project_id: str,
        scope_type: str,
        target_page_id: str | None,
        ui_surface: str,
        content_md: str,
        attachments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        project = self._require_project(project_id)
        message = self._add_message(
            project_id=project_id,
            stage=project.current_stage,
            scope_type=scope_type,
            target_page_id=target_page_id,
            role="user",
            content_md=content_md,
            structured_payload_json={
                "ui_surface": ui_surface,
                "attachments": attachments,
            },
        )
        enqueue_project_task(
            self.session,
            project_id=project.id,
            task_type="message",
            task_context={"message_id": message.id},
        )
        self.session.commit()
        wake_scheduler()
        return self.serialize_message(message)

    def get_requirement_form(self, project_id: str) -> dict[str, Any]:
        project = self._require_project(project_id)
        if not project.requirement_form:
            raise HTTPException(status_code=404, detail="需求单尚未生成")
        return self.serialize_requirement_form(project.requirement_form)

    def submit_requirement_answers(self, project_id: str, answers: list[dict[str, Any]]) -> dict[str, Any]:
        project = self._require_project(project_id)
        requirement_form = self._require_requirement_form(project)
        merged = dict(requirement_form.answers_json or {})
        for item in answers:
            merged[item["question_code"]] = item["value"]
        requirement_form.answers_json = merged
        project.page_count_target = self._coerce_page_count(merged.get("page_count_target"), project.page_count_target)
        if merged.get("style_preset"):
            project.style_preset = str(merged["style_preset"])
        self._apply_working_title(project, merged)
        append_event(
            self.session,
            project_id=project.id,
            event_type="requirements.answers_updated",
            stage="init",
            scope_type="project",
            payload={"answers": merged},
        )
        self.session.commit()
        return self.serialize_requirement_form(requirement_form)

    def patch_requirement_answer(self, project_id: str, question_code: str, value: Any) -> dict[str, Any]:
        return self.submit_requirement_answers(
            project_id,
            [{"question_code": question_code, "value": value}],
        )

    def retry_requirement_source(self, project_id: str, source_id: str) -> dict[str, Any]:
        project = self._require_project(project_id)
        requirement_form = self._require_requirement_form(project)
        search_results = self.research.build_search_result_cards(requirement_form.init_search_results_json or [])
        source = next((item for item in search_results if item.get("id") == source_id), None)
        if source is None:
            raise HTTPException(status_code=404, detail="指定资料不存在")
        query_text = str(source.get("query_text") or "").strip()
        if not query_text:
            raise HTTPException(status_code=422, detail="该条搜索结果没有对应查询词，请重跑项目级搜索")

        refreshed_items = self.research.search_query_summaries(
            [
                {
                    "query_text": query_text,
                    "query_purpose": str(source.get("query_purpose") or ""),
                    "dimension": str(source.get("dimension") or source.get("query_purpose") or ""),
                    "dimension_id": str(source.get("dimension_id") or ""),
                }
            ],
            limit_per_query=3,
            search_round=int(source.get("round") or 1),
        )
        refreshed_cards = self.research.build_search_result_cards(refreshed_items)
        kept = [
            item
            for item in search_results
            if str(item.get("query_text") or "").strip() != query_text
        ]
        requirement_form.init_search_results_json = kept + refreshed_cards
        append_event(
            self.session,
            project_id=project.id,
            event_type="workspace.data.updated",
            stage="init",
            scope_type="project",
            payload={
                "entity": "requirement_form",
                "update_kind": "init_source_retry",
                "source_id": source_id,
                "query_text": query_text,
                "result_count": len(requirement_form.init_search_results_json),
            },
        )
        self.session.commit()
        return self.serialize_requirement_form(requirement_form)

    def retry_page_search_result(self, project_id: str, page_id: str, source_id: str) -> dict[str, Any]:
        project = self._require_project(project_id)
        page = self._require_page(project_id, page_id)
        search_results = self.research.refresh_search_result_cards(page.page_search_results_json or [])
        source = next((item for item in search_results if item.get("id") == source_id), None)
        if source is None:
            raise HTTPException(status_code=404, detail="指定资料不存在")

        collection = self.research.get_or_create_page_collection(project, page)
        refreshed_source = self.research.retry_search_result_card(
            collection=collection,
            search_result=source,
        )
        self.session.refresh(page)
        latest_search_results = self.research.refresh_search_result_cards(page.page_search_results_json or [])
        page.page_search_results_json = [
            refreshed_source if item.get("id") == source_id else item
            for item in latest_search_results
        ]
        page.page_search_results_json = self.research.refresh_search_result_cards(page.page_search_results_json)
        page.page_corpus_digest_json = self.research.build_collection_digest(collection.id)
        page.search_status = "ready" if page.page_corpus_digest_json.get("document_count") else "failed"
        if refreshed_source.get("read_status") == "ready":
            page.summary_status = "stale" if page.page_summary_md else "empty"
            page.draft_status = "stale" if page.current_draft_version_id else "empty"
            page.design_status = "stale" if page.current_design_version_id else "empty"
        if page.current_research_session_id:
            research_session = self.session.get(ResearchSession, page.current_research_session_id)
            if research_session and research_session.page_id == page.id:
                research_session.candidate_sources_json = page.page_search_results_json
                research_session.status = "completed" if page.page_corpus_digest_json.get("document_count") else "failed"
        self._update_artifact_staleness(page)
        append_event(
            self.session,
            project_id=project.id,
            event_type="workspace.data.updated",
            stage=project.current_stage,
            scope_type="page",
            target_page_id=page.id,
            payload={
                "entity": "page",
                "page_id": page.id,
                "update_kind": "search_result_retry",
                "source_id": source_id,
                "read_status": refreshed_source.get("read_status"),
                "chunk_status": refreshed_source.get("chunk_status"),
                "document_count": page.page_corpus_digest_json.get("document_count", 0),
                "chunk_count": page.page_corpus_digest_json.get("chunk_count", 0),
            },
        )
        self.session.commit()
        return self.serialize_page(page, include_versions=True)

    def create_requirement_question(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        requirement_form = self._require_requirement_form(self._require_project(project_id))
        questions = [item for item in requirement_form.ai_questions_json if item.get("question_code") != payload["question_code"]]
        questions.append(payload)
        requirement_form.ai_questions_json = questions
        self.session.commit()
        return self.serialize_requirement_form(requirement_form)

    def update_requirement_question(self, project_id: str, question_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        requirement_form = self._require_requirement_form(self._require_project(project_id))
        questions: list[dict[str, Any]] = []
        for item in requirement_form.ai_questions_json:
            if item.get("question_code") != question_code:
                questions.append(item)
                continue
            next_item = dict(item)
            for key, value in payload.items():
                if value is not None:
                    next_item[key] = value
            questions.append(next_item)
        requirement_form.ai_questions_json = questions
        self.session.commit()
        return self.serialize_requirement_form(requirement_form)

    def delete_requirement_question(self, project_id: str, question_code: str) -> dict[str, Any]:
        if question_code in CLARIFICATION_CODES:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="不能删除需求澄清问题")
        requirement_form = self._require_requirement_form(self._require_project(project_id))
        requirement_form.ai_questions_json = [
            item for item in requirement_form.ai_questions_json if item.get("question_code") != question_code
        ]
        answers = dict(requirement_form.answers_json or {})
        answers.pop(question_code, None)
        requirement_form.answers_json = answers
        self.session.commit()
        return self.serialize_requirement_form(requirement_form)

    def confirm_requirements(self, project_id: str, note_md: str | None = None) -> dict[str, Any]:
        project = self._require_project(project_id)
        if project.current_stage != "init":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="项目已进入后续阶段，不能重新确认需求",
            )
        requirement_form = self._require_requirement_form(project)
        self._validate_requirement_form(project, requirement_form)
        self._apply_working_title(project, requirement_form.answers_json or {})
        if note_md:
            requirement_form.latest_instruction = note_md
        cas = self.session.execute(
            update(Project)
            .where(Project.id == project.id, Project.current_stage == "init")
            .values(current_stage="outline")
        )
        if cas.rowcount != 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="项目已进入后续阶段，不能重新确认需求",
            )
        project.current_stage = "outline"
        append_event(
            self.session,
            project_id=project.id,
            event_type="outline.queued",
            stage="outline",
            scope_type="project",
            payload={"project_id": project.id},
        )
        enqueue_project_task(self.session, project_id=project.id, task_type="outline")
        self.session.commit()
        wake_scheduler()
        return self.serialize_project(project)

    def retry_outline(self, project_id: str) -> dict[str, Any]:
        project = self._require_project(project_id)
        if project.current_stage != "outline":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="当前不在大纲阶段，不能重试生成大纲")
        if self._get_current_outline(project.id):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="大纲已生成，不能从失败态重试")
        requirement_form = self._require_requirement_form(project)
        self._validate_requirement_form(project, requirement_form)
        append_event(
            self.session,
            project_id=project.id,
            event_type="outline.queued",
            stage="outline",
            scope_type="project",
            payload={"project_id": project.id, "retry": True},
        )
        enqueue_project_task(self.session, project_id=project.id, task_type="outline")
        self.session.commit()
        wake_scheduler()
        return self.serialize_project(project)

    def _advance_outline_to_search(self, project: Project) -> None:
        if project.current_stage != "outline":
            raise RuntimeError("当前不在大纲确认阶段")
        if not self._get_current_outline(project.id):
            raise RuntimeError("大纲尚未生成")
        cas = self.session.execute(
            update(Project)
            .where(Project.id == project.id, Project.current_stage == "outline")
            .values(current_stage="search")
        )
        if cas.rowcount != 1:
            raise RuntimeError("当前不在大纲确认阶段")
        project.current_stage = "search"
        append_event(
            self.session,
            project_id=project.id,
            event_type="status.changed",
            stage="search",
            scope_type="project",
            payload={"current_stage": "search"},
        )

    def confirm_outline(self, project_id: str) -> dict[str, Any]:
        project = self._require_project(project_id)
        try:
            self._advance_outline_to_search(project)
        except RuntimeError as exc:
            detail = str(exc)
            status_code = (
                status.HTTP_422_UNPROCESSABLE_ENTITY
                if "尚未生成" in detail
                else status.HTTP_409_CONFLICT
            )
            raise HTTPException(status_code=status_code, detail=detail) from exc
        self.session.commit()
        return self.serialize_project(project)

    def get_style_cards(self, project_id: str) -> dict[str, Any]:
        project = self._require_project(project_id)
        return self._serialize_style_cards(project)

    def generate_style_cards(self, project_id: str) -> dict[str, Any]:
        project = self._require_project(project_id)
        if PROJECT_STAGE_ORDER.get(project.current_stage, 0) < PROJECT_STAGE_ORDER["search"]:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="请先确认大纲后再生成风格卡")
        outline = self._get_current_outline(project.id)
        cover_title = cover_title_from_outline(outline.outline_json if outline else None) or ""
        page_titles: list[str] = []
        pages = list(
            self.session.scalars(
                select(ProjectPage).where(ProjectPage.project_id == project.id).order_by(ProjectPage.sort_order.asc())
            )
        )
        for page in pages:
            brief = self._get_current_brief(page)
            if brief and brief.title.strip():
                page_titles.append(brief.title.strip())
        cards = self.generator.generate_style_cards(
            title=project.title,
            request_text=project.request_text,
            style_hint=str(project.style_preset or ""),
            outline_cover_title=cover_title,
            page_titles=page_titles[:20],
        )
        project.style_candidates_json = cards
        self.session.commit()
        return self._serialize_style_cards(project)

    def confirm_style_card(self, project_id: str, style_id: str, *, source: str = "candidates") -> dict[str, Any]:
        project = self._require_project(project_id)
        card = self._find_style_card(project, style_id, source=source)
        if not card:
            raise HTTPException(status_code=404, detail="风格卡不存在")
        project.style_card_json = card
        project.style_preset = card["style_id"]
        self._mark_project_designs_stale(project)
        self.session.commit()
        return self._serialize_style_cards(project)

    def save_style_card_to_library(self, project_id: str, style_id: str | None = None) -> dict[str, Any]:
        project = self._require_project(project_id)
        card = None
        if style_id:
            card = self._find_style_card(project, style_id, source="any")
        elif isinstance(project.style_card_json, dict) and project.style_card_json.get("style_id"):
            card = dict(project.style_card_json)
        if not card:
            raise HTTPException(status_code=404, detail="没有可入库的风格卡")
        saved = save_card_to_library(self.session, card)
        self.session.commit()
        payload = self._serialize_style_cards(project)
        payload["saved"] = saved
        return payload

    def _serialize_style_cards(self, project: Project) -> dict[str, Any]:
        frozen = serialize_card(project.style_card_json if isinstance(project.style_card_json, dict) else None)
        candidates = [
            serialize_card(item)
            for item in (project.style_candidates_json or [])
            if isinstance(item, dict)
        ]
        return {
            "project_id": project.id,
            "frozen": frozen,
            "candidates": [item for item in candidates if item],
            "library": list_library_cards(self.session),
        }

    def _find_style_card(self, project: Project, style_id: str, *, source: str) -> dict[str, Any] | None:
        wanted = str(style_id or "").strip()
        if not wanted:
            return None
        if source in {"candidates", "any"}:
            for item in project.style_candidates_json or []:
                if isinstance(item, dict) and item.get("style_id") == wanted:
                    return dict(item)
        if source in {"frozen", "any"}:
            frozen = project.style_card_json if isinstance(project.style_card_json, dict) else {}
            if frozen.get("style_id") == wanted:
                return dict(frozen)
        if source in {"library", "any"}:
            library = get_library_card(self.session, wanted)
            if library:
                return dict(library)
        return None

    def _frozen_style_card(self, project: Project) -> dict[str, Any] | None:
        card = project.style_card_json if isinstance(project.style_card_json, dict) else None
        if card and card.get("style_id"):
            return card
        return None

    def _resolve_style_pack(self, project: Project, style_id: str | None) -> dict[str, Any]:
        frozen = self._frozen_style_card(project)
        if frozen:
            return pack_from_card(frozen)
        if style_id:
            library = get_library_card(self.session, style_id)
            if library:
                return pack_from_card(library)
        return self.generator.get_style_pack(style_id)

    def _style_library_options(self) -> list[dict[str, Any]]:
        options: list[dict[str, Any]] = []
        for card in list_library_cards(self.session):
            if not card:
                continue
            options.append(
                {
                    "style_id": card["style_id"],
                    "style_name": card["name"],
                    "description": card.get("rationale") or "",
                    "palette": card.get("palette") or {},
                }
            )
        return options or self.generator.list_style_options()

    def _mark_project_designs_stale(self, project: Project) -> None:
        pages = list(self.session.scalars(select(ProjectPage).where(ProjectPage.project_id == project.id)))
        for page in pages:
            if page.current_design_version_id:
                page.design_status = "stale"
                self._update_artifact_staleness(page)

    def upload_background(self, project_id: str, file: UploadFile) -> dict[str, Any]:
        project = self._require_project(project_id)
        payload = _read_upload_limited(file, BACKGROUND_MAX_BYTES)
        suffix = _detect_background_suffix(payload)
        if suffix is None:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="仅支持 png/jpeg/webp/gif 背景图",
            )
        asset_id = _safe_storage_id(project.id)
        target_dir = self.settings.background_path
        target_dir.mkdir(parents=True, exist_ok=True)
        if project.background_asset_path:
            Path(project.background_asset_path).unlink(missing_ok=True)
        for stale in target_dir.glob(f"{asset_id}.*"):
            stale.unlink(missing_ok=True)
        target = target_dir / f"{asset_id}{suffix}"
        target.write_bytes(payload)
        project.background_asset_path = str(target)
        requirement_form = self._require_requirement_form(project)
        answers = dict(requirement_form.answers_json or {})
        answers["background_asset"] = str(target)
        requirement_form.answers_json = answers
        self.session.commit()
        return {"project_id": project.id, "background_asset_path": str(target)}

    def get_outline(self, project_id: str) -> dict[str, Any]:
        outline = self._get_current_outline(project_id)
        if not outline:
            raise HTTPException(status_code=404, detail="大纲尚未生成")
        return self.serialize_outline(outline)

    def list_pages(self, project_id: str) -> list[dict[str, Any]]:
        self._require_project(project_id)
        stmt = select(ProjectPage).where(ProjectPage.project_id == project_id).order_by(ProjectPage.sort_order.asc())
        return [self.serialize_page(item) for item in self.session.scalars(stmt)]

    def get_page(self, project_id: str, page_id: str) -> dict[str, Any]:
        return self.serialize_page(self._require_page(project_id, page_id), include_versions=True)

    def patch_storyboard(self, project_id: str, parts_payload: list[dict[str, Any]]) -> dict[str, Any]:
        project = self._require_project(project_id)
        current_outline = self._get_current_outline(project_id)
        if not current_outline:
            raise HTTPException(status_code=404, detail="当前项目大纲不存在")
        pages = list(
            self.session.scalars(
                select(ProjectPage).where(ProjectPage.project_id == project_id).order_by(ProjectPage.sort_order.asc())
            )
        )
        outline_payload = json.loads(json.dumps(current_outline.outline_json or {}))
        ppt_outline = outline_payload.get("ppt_outline") or {}
        content_pages = [page for page in pages if page.page_role == "content"]
        existing_page_by_id = {page.id: page for page in content_pages}
        requested_existing_ids: list[str] = []
        rebuilt_parts: list[dict[str, Any]] = []
        ordered_content_pages: list[ProjectPage] = []

        used_page_ids: set[str] = set()
        next_page_code_no = 1
        for page in pages:
            match = re.fullmatch(r"page-(\d+)", page.page_code or "")
            if match:
                next_page_code_no = max(next_page_code_no, int(match.group(1)) + 1)

        def allocate_page_code() -> str:
            nonlocal next_page_code_no
            code = f"page-{next_page_code_no:02d}"
            next_page_code_no += 1
            return code

        for part_payload in parts_payload:
            part_title = str(part_payload.get("part_title") or "").strip() or "未命名章节"
            rebuilt_pages: list[dict[str, Any]] = []
            for page_payload in part_payload.get("pages", []):
                page_id = page_payload.get("page_id")
                title = str(page_payload.get("title") or "").strip() or "新内容页"
                content_outline = [str(item).strip() for item in page_payload.get("content_outline", []) if str(item).strip()]

                if page_id:
                    if page_id in used_page_ids:
                        raise HTTPException(status_code=422, detail="storyboard 页面重复，无法重排")
                    page = existing_page_by_id.get(page_id)
                    if not page:
                        raise HTTPException(status_code=422, detail="storyboard 页面不存在，无法重排")
                    used_page_ids.add(page_id)
                    requested_existing_ids.append(page_id)
                    brief = self._get_current_brief(page)
                    current_title = brief.title if brief else ""
                    current_content_outline = brief.content_outline_json if brief else []
                    if current_title != title or current_content_outline != content_outline or page.part_title != part_title:
                        self._new_page_brief_version(
                            page=page,
                            title=title,
                            content_outline=content_outline,
                            section_title=part_title,
                        )
                        self._mark_page_structure_changed(page)
                else:
                    page = ProjectPage(
                        project_id=project.id,
                        page_code=allocate_page_code(),
                        page_role="content",
                        part_title=part_title,
                        sort_order=0,
                        outline_status="ready",
                        search_status="empty",
                        summary_status="empty",
                        draft_status="empty",
                        design_status="empty",
                        page_summary_md="",
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
                        content_outline=content_outline,
                        section_title=part_title,
                    )
                    self._mark_page_structure_changed(page)

                ordered_content_pages.append(page)
                rebuilt_pages.append(
                    {
                        "title": title,
                        "content": content_outline,
                    }
                )
            rebuilt_parts.append(
                {
                    "part_title": part_title,
                    "pages": rebuilt_pages,
                }
            )

        requested_existing_id_set = set(requested_existing_ids)
        deleted_content_pages = [page for page in content_pages if page.id not in requested_existing_id_set]
        for page in deleted_content_pages:
            self.session.delete(page)

        ppt_outline["parts"] = rebuilt_parts

        prefix_pages: list[ProjectPage] = []
        suffix_pages: list[ProjectPage] = []
        seen_content = False
        for page in pages:
            if page.page_role == "content":
                seen_content = True
                continue
            if not seen_content:
                prefix_pages.append(page)
            else:
                suffix_pages.append(page)

        ordered_pages = prefix_pages + ordered_content_pages + suffix_pages
        for sort_order, page in enumerate(ordered_pages, start=1):
            page.sort_order = sort_order

        current_outline_changed = current_outline.outline_json != outline_payload
        current_order_ids = [page.id for page in content_pages]
        next_order_ids = [page.id for page in ordered_content_pages]
        if not current_outline_changed and current_order_ids == next_order_ids:
            return {
                "items": [self.serialize_page(page) for page in ordered_pages],
                "outline": self.serialize_outline(current_outline),
            }

        next_outline = OutlineVersion(
            project_id=project.id,
            version_no=current_outline.version_no + 1,
            status="ready",
            outline_json=outline_payload,
        )
        self.session.add(next_outline)
        self.session.commit()
        return {
            "items": [self.serialize_page(page) for page in ordered_pages],
            "outline": self.serialize_outline(next_outline),
        }

    def patch_page_outline(self, project_id: str, page_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        page = self._require_page(project_id, page_id)
        brief = self._new_page_brief_version(
            page=page,
            title=payload["title"],
            content_outline=payload["content_outline"],
            section_title=payload.get("section_title"),
        )
        self._mark_page_structure_changed(page)
        self.session.commit()
        return self.serialize_page(page, include_versions=True)

    def patch_page_summary(self, project_id: str, page_id: str, summary_md: str) -> dict[str, Any]:
        page = self._require_page(project_id, page_id)
        page.page_summary_md = summary_md
        page.summary_status = "ready"
        page.draft_status = "stale" if page.current_draft_version_id else "empty"
        page.design_status = "stale" if page.current_design_version_id else "empty"
        self._update_artifact_staleness(page)
        self.session.commit()
        return self.serialize_page(page, include_versions=True)

    def patch_page_draft(self, project_id: str, page_id: str, svg_markup: str) -> dict[str, Any]:
        page = self._require_page(project_id, page_id)
        try:
            markup = extract_and_validate_svg(svg_markup)
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        current = self._get_current_draft(page)
        plan = dict(current.content_plan_json) if current and isinstance(current.content_plan_json, dict) else {}
        if has_content_plan(plan):
            try:
                assert_svg_matches_plan(markup, plan)
            except RuntimeError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        version_no = (
            (self.session.scalar(select(func.count(DraftVersion.id)).where(DraftVersion.page_id == page.id)) or 0) + 1
        )
        draft = DraftVersion(
            project_id=page.project_id,
            page_id=page.id,
            version_no=version_no,
            status="ready",
            page_brief_version_id=current.page_brief_version_id if current else page.current_brief_version_id,
            research_session_id=current.research_session_id if current else page.current_research_session_id,
            draft_svg_markup=markup,
            content_plan_json=plan,
        )
        self.session.add(draft)
        self.session.flush()
        page.current_draft_version_id = draft.id
        page.draft_status = "ready"
        page.design_status = "stale" if page.current_design_version_id else "empty"
        self._update_artifact_staleness(page)
        self.session.commit()
        return self.serialize_page(page, include_versions=True)

    def queue_page_action(
        self,
        project_id: str,
        page_id: str,
        action_type: str,
        *,
        replace_existing: bool = True,
    ) -> dict[str, Any]:
        project = self._require_project(project_id)
        self._require_page(project_id, page_id)
        if action_type in {"page_search_run", "page_search_refresh", "page_generate_search_queries"} and (
            PROJECT_STAGE_ORDER.get(project.current_stage, 0) < PROJECT_STAGE_ORDER["search"]
        ):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="请先确认大纲后再按页找资料")
        agent_run_id = new_id()
        task = enqueue_page_action(
            self.session,
            project_id=project_id,
            page_id=page_id,
            action_type=action_type,
            agent_run_id=agent_run_id,
            replace_existing=replace_existing,
            priority=100,
        )
        self.session.commit()
        wake_scheduler()
        return {"status": "queued", "agent_run_id": agent_run_id, "task_id": task.task_id}

    def queue_batch_action(self, project_id: str, action_type: str) -> dict[str, Any]:
        project = self._require_project(project_id)
        if action_type == "project_batch_search" and (
            PROJECT_STAGE_ORDER.get(project.current_stage, 0) < PROJECT_STAGE_ORDER["search"]
        ):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="请先确认大纲后再按页找资料")
        agent_run_id = new_id()
        tasks = enqueue_batch_action(
            self.session,
            project_id=project_id,
            action_type=action_type,
            agent_run_id=agent_run_id,
        )
        self.session.commit()
        wake_scheduler()
        return {"status": "queued", "agent_run_id": agent_run_id, "task_ids": [item.task_id for item in tasks]}

    def cancel_tasks(self, project_id: str, page_id: str | None = None) -> dict[str, Any]:
        self._require_project(project_id)
        if page_id:
            self._require_page(project_id, page_id)
        canceled = request_task_cancel(self.session, project_id=project_id, page_id=page_id)
        self.session.commit()
        return {"status": "canceled", "canceled": canceled}

    def get_page_draft(self, project_id: str, page_id: str) -> dict[str, Any]:
        page = self._require_page(project_id, page_id)
        draft = self._get_current_draft(page)
        if not draft:
            raise HTTPException(status_code=404, detail="当前页策划稿尚未生成")
        return self.serialize_draft(draft)

    def get_page_design(self, project_id: str, page_id: str) -> dict[str, Any]:
        page = self._require_page(project_id, page_id)
        design = self._get_current_design(page)
        if not design:
            raise HTTPException(status_code=404, detail="当前页设计稿尚未生成")
        return self.serialize_design(design)

    def create_export(self, project_id: str, export_format: str) -> dict[str, Any]:
        project = self._require_project(project_id)
        if export_format == "zip":
            export_path = self._build_export_archive(project)
        elif export_format == "pptx":
            export_path = self._build_export_pptx(project, mode="shapes")
        elif export_format == "pptx-image":
            export_path = self._build_export_pptx(project, mode="image")
        else:
            raise HTTPException(status_code=400, detail="当前仅支持 zip、pptx 或 pptx-image 导出")
        stored_format = "pptx" if export_format.startswith("pptx") else export_format
        export_job = ExportJob(
            project_id=project_id,
            export_format=stored_format,
            status="completed",
            file_path=str(export_path),
            font_report_json=build_font_report(self._design_svgs_for_export(project)),
        )
        self.session.add(export_job)
        self.session.flush()
        self.session.commit()
        return self.serialize_export(export_job)

    def get_export(self, project_id: str, export_id: str) -> dict[str, Any]:
        export = self.session.get(ExportJob, export_id)
        if not export or export.project_id != project_id:
            raise HTTPException(status_code=404, detail="导出任务不存在")
        return self.serialize_export(export)

    def get_export_file_path(self, project_id: str, export_id: str) -> str:
        export = self.session.get(ExportJob, export_id)
        if not export or export.project_id != project_id:
            raise HTTPException(status_code=404, detail="导出任务不存在")
        return export.file_path

    def get_export_download_name(self, project_id: str, export_id: str) -> str:
        export = self.session.get(ExportJob, export_id)
        if not export or export.project_id != project_id:
            raise HTTPException(status_code=404, detail="导出任务不存在")
        return Path(export.file_path).name

    def _serialize_page_preview(self, page: ProjectPage) -> dict[str, Any]:
        design = self._get_current_design(page)
        if design and design.design_svg_markup.strip():
            return {
                "preview_surface": "design",
                "preview_svg_markup": design.design_svg_markup,
            }
        draft = self._get_current_draft(page)
        if draft and draft.draft_svg_markup.strip():
            return {
                "preview_surface": "draft",
                "preview_svg_markup": draft.draft_svg_markup,
            }
        return {
            "preview_surface": "document",
            "preview_svg_markup": None,
        }

    def _serialize_project_preview(self, project: Project) -> dict[str, Any]:
        cover_page = self.session.scalars(
            select(ProjectPage)
            .where(ProjectPage.project_id == project.id, ProjectPage.page_role == "cover")
            .order_by(ProjectPage.sort_order.asc())
            .limit(1)
        ).first()
        if not cover_page:
            return {
                "preview_surface": "fallback",
                "preview_svg_markup": None,
            }

        page_preview = self._serialize_page_preview(cover_page)
        if page_preview["preview_surface"] == "design":
            return page_preview
        if page_preview["preview_surface"] == "draft":
            return page_preview
        return {
            "preview_surface": "fallback",
            "preview_svg_markup": None,
        }

    def serialize_project(self, project: Project) -> dict[str, Any]:
        page_count = self.session.scalar(select(func.count(ProjectPage.id)).where(ProjectPage.project_id == project.id)) or 0
        return {
            "project_id": project.id,
            "title": project.title,
            "request_text": project.request_text,
            "current_stage": project.current_stage,
            "page_count_target": project.page_count_target,
            "style_preset": project.style_preset,
            "style_card": serialize_card(project.style_card_json if isinstance(project.style_card_json, dict) else None),
            "background_asset_path": project.background_asset_path,
            "workflow_constraints": project.workflow_constraints_json.get("items", []),
            "page_count": page_count,
            "created_at": project.created_at.isoformat(),
            "updated_at": project.updated_at.isoformat(),
            **self._serialize_project_preview(project),
        }

    def serialize_message(self, message: ProjectMessage) -> dict[str, Any]:
        return {
            "id": message.id,
            "project_id": message.project_id,
            "stage": message.stage,
            "scope_type": message.scope_type,
            "target_page_id": message.target_page_id,
            "role": message.role,
            "content_md": message.content_md,
            "structured_payload_json": message.structured_payload_json,
            "created_at": message.created_at.isoformat(),
        }

    def serialize_requirement_form(self, form: RequirementForm) -> dict[str, Any]:
        project = self._require_project(form.project_id)
        init_search_results = self.research.refresh_search_result_cards(form.init_search_results_json or [])
        return {
            "requirement_form": {
                "project_id": form.project_id,
                "status": form.status,
                "workflow_constraints": project.workflow_constraints_json.get("items", []),
                "init_search_queries": form.init_search_queries_json,
                "init_search_results": init_search_results,
                "init_corpus_digest": form.init_corpus_digest_json,
                "page_count_options": form.page_count_options_json,
                "fixed_items": self._build_fixed_fields(),
                "ai_questions": form.ai_questions_json,
                "answers": form.answers_json,
                "suggested_actions": form.suggested_actions_json,
            }
        }

    def serialize_outline(self, outline: OutlineVersion) -> dict[str, Any]:
        return {
            "outline_version_id": outline.id,
            "project_id": outline.project_id,
            "version_no": outline.version_no,
            "status": outline.status,
            "outline": outline.outline_json,
            "created_at": outline.created_at.isoformat(),
            "updated_at": outline.updated_at.isoformat(),
        }

    def serialize_page(self, page: ProjectPage, include_versions: bool = False) -> dict[str, Any]:
        brief = self._get_current_brief(page)
        draft = self._get_current_draft(page)
        design = self._get_current_design(page)
        page_search_results = self.research.refresh_search_result_cards(page.page_search_results_json or [])
        payload = {
            "page_id": page.id,
            "project_id": page.project_id,
            "page_code": page.page_code,
            "page_role": page.page_role,
            "part_title": page.part_title,
            "sort_order": page.sort_order,
            "title": brief.title if brief else "",
            "content_outline": brief.content_outline_json if brief else [],
            "outline_status": page.outline_status,
            "search_status": page.search_status,
            "summary_status": page.summary_status,
            "draft_status": page.draft_status,
            "design_status": page.design_status,
            "page_search_queries": page.page_search_queries_json,
            "page_search_results": page_search_results,
            "page_images": public_catalog(page.page_images_json),
            "search_coverage": search_coverage(page.page_search_queries_json or [], page_search_results),
            "page_corpus_digest": page.page_corpus_digest_json,
            "page_summary_md": page.page_summary_md,
            "page_summary_citations": page.page_summary_citations_json,
            "current_artifact_staleness": page.artifact_staleness_json,
            "current_brief_version_id": page.current_brief_version_id,
            "current_draft_version_id": page.current_draft_version_id,
            "current_design_version_id": page.current_design_version_id,
            "draft_preview_svg_markup": draft.draft_svg_markup if draft and draft.draft_svg_markup.strip() else None,
            "design_preview_svg_markup": design.design_svg_markup if design and design.design_svg_markup.strip() else None,
            "created_at": page.created_at.isoformat(),
            "updated_at": page.updated_at.isoformat(),
            **self._serialize_page_preview(page),
        }
        if include_versions:
            payload["draft"] = self.serialize_draft(draft) if draft else None
            payload["design"] = self.serialize_design(design) if design else None
        return payload

    def serialize_draft(self, draft: DraftVersion) -> dict[str, Any]:
        return {
            "draft_version_id": draft.id,
            "project_id": draft.project_id,
            "page_id": draft.page_id,
            "version_no": draft.version_no,
            "status": draft.status,
            "page_brief_version_id": draft.page_brief_version_id,
            "research_session_id": draft.research_session_id,
            "draft_svg_markup": draft.draft_svg_markup,
            "content_plan_json": draft.content_plan_json or {},
            "created_at": draft.created_at.isoformat(),
            "updated_at": draft.updated_at.isoformat(),
        }

    def serialize_design(self, design: DesignVersion) -> dict[str, Any]:
        project = self._require_project(design.project_id)
        return {
            "design_version_id": design.id,
            "project_id": design.project_id,
            "page_id": design.page_id,
            "version_no": design.version_no,
            "status": design.status,
            "draft_version_id": design.draft_version_id,
            "style_pack_id": design.style_pack_id,
            "background_asset_path": design.background_asset_path,
            "design_svg_markup": design.design_svg_markup,
            "style_pack": self._resolve_style_pack(project, design.style_pack_id),
            "created_at": design.created_at.isoformat(),
            "updated_at": design.updated_at.isoformat(),
        }

    def serialize_export(self, export_job: ExportJob) -> dict[str, Any]:
        return {
            "export_id": export_job.id,
            "project_id": export_job.project_id,
            "export_format": export_job.export_format,
            "status": export_job.status,
            "file_path": export_job.file_path,
            "font_report": export_job.font_report_json or {},
            "created_at": export_job.created_at.isoformat(),
            "updated_at": export_job.updated_at.isoformat(),
        }

    def _require_project(self, project_id: str) -> Project:
        project = self.session.get(Project, project_id)
        if not project:
            raise HTTPException(status_code=404, detail="项目不存在")
        return project

    def _purge_project_files(self, project: Project) -> None:
        asset_id = project.id.lower()
        candidates: list[Path] = []
        if project.background_asset_path:
            candidates.append(Path(project.background_asset_path))
        for directory in (self.settings.background_path, self.settings.export_path, self.settings.upload_path):
            if not directory.exists():
                continue
            candidates.extend(directory.glob(f"{asset_id}.*"))
        export_dir = self.settings.export_path / project.id
        if export_dir.is_dir():
            shutil.rmtree(export_dir, ignore_errors=True)
        seen: set[Path] = set()
        for path in candidates:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            path.unlink(missing_ok=True)

    def _require_requirement_form(self, project: Project) -> RequirementForm:
        if not project.requirement_form:
            raise HTTPException(status_code=404, detail="需求单不存在")
        return project.requirement_form

    def _require_page(self, project_id: str, page_id: str) -> ProjectPage:
        page = self.session.get(ProjectPage, page_id)
        if not page or page.project_id != project_id:
            raise HTTPException(status_code=404, detail="页面不存在")
        return page

    def _add_message(
        self,
        *,
        project_id: str,
        stage: str,
        scope_type: str,
        role: str,
        content_md: str,
        target_page_id: str | None = None,
        structured_payload_json: dict[str, Any] | None = None,
    ) -> ProjectMessage:
        message = ProjectMessage(
            project_id=project_id,
            stage=stage,
            scope_type=scope_type,
            target_page_id=target_page_id,
            role=role,
            content_md=content_md,
            structured_payload_json=structured_payload_json or {},
        )
        self.session.add(message)
        self.session.flush()
        return message

    def _build_fixed_fields(self) -> dict[str, Any]:
        return {
            "page_count": {
                "question_code": "page_count_target",
                "allow_custom": True,
            },
            "style_preset": {
                "question_code": "style_preset",
                "options": self._style_library_options(),
                "allow_custom": True,
            },
            "background_asset": {
                "question_code": "background_asset",
                "allow_upload": True,
                "required": False,
            },
        }

    def _coerce_page_count(self, raw_value: Any, fallback: int | None) -> int | None:
        if raw_value is None:
            return fallback
        if isinstance(raw_value, int):
            return raw_value
        if isinstance(raw_value, str):
            stripped = raw_value.strip()
            if stripped.isdigit():
                return int(stripped)
        return fallback

    def _validate_requirement_form(self, project: Project, form: RequirementForm) -> None:
        answers = form.answers_json or {}
        page_count_target = self._coerce_page_count(answers.get("page_count_target"), project.page_count_target)
        if page_count_target is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="页数目标未填写")
        if not answers.get("style_preset") and not project.style_preset:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="风格预设未选择")
        missing = [
            item["question_code"]
            for item in form.ai_questions_json
            if not str(answers.get(item["question_code"], "")).strip()
        ]
        if missing:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"补充问题未完成: {', '.join(missing)}")
        if not form.init_search_results_json:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="首轮搜索结果为空")

    def _get_current_outline(self, project_id: str) -> OutlineVersion | None:
        stmt = (
            select(OutlineVersion)
            .where(OutlineVersion.project_id == project_id)
            .order_by(OutlineVersion.version_no.desc(), OutlineVersion.created_at.desc())
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    def _get_current_brief(self, page: ProjectPage) -> PageBriefVersion | None:
        if not page.current_brief_version_id:
            return None
        return self.session.get(PageBriefVersion, page.current_brief_version_id)

    def _get_current_draft(self, page: ProjectPage) -> DraftVersion | None:
        if not page.current_draft_version_id:
            return None
        return self.session.get(DraftVersion, page.current_draft_version_id)

    def _get_current_design(self, page: ProjectPage) -> DesignVersion | None:
        if not page.current_design_version_id:
            return None
        return self.session.get(DesignVersion, page.current_design_version_id)

    def _set_project_stage_at_least(self, project: Project, stage: str) -> None:
        if PROJECT_STAGE_ORDER.get(stage, 0) > PROJECT_STAGE_ORDER.get(project.current_stage, 0):
            project.current_stage = stage

    def _new_page_brief_version(
        self,
        *,
        page: ProjectPage,
        title: str,
        content_outline: list[str],
        section_title: str | None,
        status_text: str = "ready",
    ) -> PageBriefVersion:
        current_brief = self._get_current_brief(page)
        next_version = (current_brief.version_no if current_brief else 0) + 1
        brief = PageBriefVersion(
            project_id=page.project_id,
            page_id=page.id,
            version_no=next_version,
            status=status_text,
            section_title=section_title,
            title=title,
            content_outline_json=content_outline,
            content_summary="；".join(content_outline[:2]) or title,
        )
        self.session.add(brief)
        self.session.flush()
        page.current_brief_version_id = brief.id
        page.part_title = section_title
        page.outline_status = "ready"
        return brief

    def _mark_page_structure_changed(self, page: ProjectPage) -> None:
        if page.page_role == "content":
            if page.page_search_results_json:
                page.search_status = "stale"
            else:
                page.search_status = "empty"
            if page.page_summary_md:
                page.summary_status = "stale"
            else:
                page.summary_status = "empty"
        if page.current_draft_version_id:
            page.draft_status = "stale"
        else:
            page.draft_status = "empty"
        if page.current_design_version_id:
            page.design_status = "stale"
        else:
            page.design_status = "empty"
        self._update_artifact_staleness(page)

    def _update_artifact_staleness(self, page: ProjectPage) -> None:
        page.artifact_staleness_json = {
            "search": page.search_status == "stale",
            "summary": page.summary_status == "stale",
            "draft": page.draft_status == "stale",
            "design": page.design_status == "stale",
        }

    def _build_outline_snapshot(self, project_id: str) -> list[dict[str, Any]]:
        snapshot: list[dict[str, Any]] = []
        stmt = select(ProjectPage).where(ProjectPage.project_id == project_id).order_by(ProjectPage.sort_order.asc())
        for page in self.session.scalars(stmt):
            brief = self._get_current_brief(page)
            snapshot.append(
                {
                    "page_id": page.id,
                    "page_code": page.page_code,
                    "page_role": page.page_role,
                    "section_title": page.part_title,
                    "title": brief.title if brief else "",
                    "content_outline": brief.content_outline_json if brief else [],
                }
            )
        return snapshot

    def _build_project_level_status_summary(self, project: Project) -> dict[str, Any]:
        pages = self.list_pages(project.id)
        return {
            "project_stage": project.current_stage,
            "page_count_target": project.page_count_target,
            "style_preset": project.style_preset,
            "pages": [
                {
                    "page_id": item["page_id"],
                    "page_code": item["page_code"],
                    "page_role": item["page_role"],
                    "title": item["title"],
                    "outline_status": item["outline_status"],
                    "search_status": item["search_status"],
                    "summary_status": item["summary_status"],
                    "draft_status": item["draft_status"],
                    "design_status": item["design_status"],
                }
                for item in pages
            ],
        }

    def _build_page_context_for_router(self, page: ProjectPage | None) -> dict[str, Any]:
        if page is None:
            return {}
        brief = self._get_current_brief(page)
        return {
            "page_id": page.id,
            "page_title": brief.title if brief else "",
            "page_bullets": brief.content_outline_json if brief else [],
            "page_section_title": page.part_title,
            "page_outline_status": page.outline_status,
            "page_search_status": page.search_status,
            "page_summary_status": page.summary_status,
            "page_draft_status": page.draft_status,
            "page_design_status": page.design_status,
            "page_search_queries": page.page_search_queries_json,
            "page_corpus_digest": page.page_corpus_digest_json,
            "page_summary_digest": {
                "summary_md": page.page_summary_md,
                "citation_count": len(page.page_summary_citations_json or []),
            },
            "current_artifact_staleness": page.artifact_staleness_json,
            "outline_full_snapshot": self._build_outline_snapshot(page.project_id),
        }

    def _build_export_archive(self, project: Project) -> Path:
        pages = list(self.session.scalars(select(ProjectPage).where(ProjectPage.project_id == project.id).order_by(ProjectPage.sort_order.asc())))
        stem = self._resolve_export_stem(project)
        export_path = self.settings.export_path / project.id / f"{stem}.zip"
        export_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(export_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            manifest: list[dict[str, Any]] = []
            for page in pages:
                design = self._get_current_design(page)
                if not design:
                    continue
                filename = f"{page.page_code}.svg"
                archive.writestr(filename, design.design_svg_markup)
                manifest.append({"page_id": page.id, "page_code": page.page_code, "file": filename})
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        return export_path

    def _design_svgs_for_export(self, project: Project) -> list[str]:
        pages = list(
            self.session.scalars(
                select(ProjectPage).where(ProjectPage.project_id == project.id).order_by(ProjectPage.sort_order.asc())
            )
        )
        markups: list[str] = []
        for page in pages:
            design = self._get_current_design(page)
            if design and design.design_svg_markup:
                markups.append(design.design_svg_markup)
        return markups

    def _build_export_pptx(self, project: Project, *, mode: str = "shapes") -> Path:
        exportables = self._collect_exportable_designs(project.id)
        slides = [(page.page_code, design.design_svg_markup) for page, design in exportables]
        stem = self._resolve_export_stem(project)
        export_path = self.settings.export_path / project.id / f"{stem}.pptx"
        from app.services.export import build_pptx

        return build_pptx(slides, export_path, mode=mode)

    def _resolve_export_stem(self, project: Project) -> str:
        outline = self._get_current_outline(project.id)
        first_page = self.session.scalars(
            select(ProjectPage)
            .where(ProjectPage.project_id == project.id)
            .order_by(ProjectPage.sort_order.asc())
            .limit(1)
        ).first()
        first_title = None
        if first_page:
            brief = self._get_current_brief(first_page)
            first_title = brief.title if brief else None
        return resolve_export_stem(
            outline_json=outline.outline_json if outline else None,
            first_page_title=first_title,
            project_title=project.title,
        )

    def _collect_exportable_designs(self, project_id: str) -> list[tuple[ProjectPage, DesignVersion]]:
        pages = list(
            self.session.scalars(
                select(ProjectPage).where(ProjectPage.project_id == project_id).order_by(ProjectPage.sort_order.asc())
            )
        )
        exportables: list[tuple[ProjectPage, DesignVersion]] = []
        missing_pages: list[str] = []
        for page in pages:
            design = self._get_current_design(page)
            if page.design_status != "ready" or not design or not design.design_svg_markup.strip():
                missing_pages.append(f"{page.sort_order}. {self._page_export_title(page)}")
                continue
            exportables.append((page, design))
        if missing_pages:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"以下页面还没有完成设计稿，无法导出 PPTX: {'；'.join(missing_pages)}",
            )
        if not exportables:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="当前项目没有可导出的设计稿页面",
            )
        return exportables

    def _page_export_title(self, page: ProjectPage) -> str:
        brief = self._get_current_brief(page)
        return brief.title if brief and brief.title.strip() else page.page_code

    def _apply_working_title(self, project: Project, answers: dict[str, Any]) -> None:
        title = project_title_from_answers(answers)
        if title:
            project.title = title

    def _build_fixed_field_values(self, project: Project, requirement_form: RequirementForm) -> dict[str, Any]:
        answers = requirement_form.answers_json or {}
        return {
            "page_count_target": self._coerce_page_count(answers.get("page_count_target"), project.page_count_target),
            "style_preset": str(answers.get("style_preset") or project.style_preset or ""),
            "background_asset": str(answers.get("background_asset") or project.background_asset_path or ""),
        }

    def _build_fixed_page_summary(self, title: str, bullets: list[str]) -> str:
        lines = [title.strip()] if title.strip() else []
        lines.extend(item.strip() for item in bullets if item.strip())
        return "\n".join(lines).strip()

    def _recent_messages(self, project_id: str, limit: int = 12) -> list[dict[str, Any]]:
        stmt = (
            select(ProjectMessage)
            .where(ProjectMessage.project_id == project_id)
            .order_by(ProjectMessage.created_at.desc())
            .limit(limit)
        )
        messages = list(self.session.scalars(stmt))
        messages.reverse()
        return [
            {
                "role": item.role,
                "stage": item.stage,
                "scope_type": item.scope_type,
                "target_page_id": item.target_page_id,
                "content_md": item.content_md,
            }
            for item in messages
        ]

    def _build_system_decision(
        self,
        *,
        scope_type: str,
        target_stage: str,
        target_page_id: str | None,
        action_type: str,
        reason: str,
        execution_plan: list[dict[str, str]],
    ) -> dict[str, Any]:
        return {
            "scope_type": scope_type,
            "target_stage": target_stage,
            "target_page_id": target_page_id,
            "intent_type": action_type,
            "action_type": action_type,
            "should_execute": True,
            "needs_clarification": False,
            "requires_confirmation": False,
            "missing_data": [],
            "data_updates": {
                "question_patch": None,
                "answer_patch": None,
                "outline_patch": None,
                "page_patch": None,
                "summary_patch": None,
            },
            "execution_plan": execution_plan,
            "next_recommendations": [],
            "reason": reason,
        }

    def _page_action_execution_plan(self, action_type: str) -> list[dict[str, str]]:
        plans = {
            "page_generate_search_queries": [
                {"step_code": "page_generate_search_queries", "step_name": "生成页面搜索词", "reason": "把当前页结构化需求翻译成搜索词集合。"},
            ],
            "page_search_run": [
                {"step_code": "page_search_bocha", "step_name": "执行 Bocha 搜索", "reason": "先获取搜索摘要结果，再决定后续抓取。"},
                {"step_code": "page_search_read", "step_name": "抓取全文并写入资料池", "reason": "把搜索结果扩展成可引用正文。"},
                {"step_code": "page_search_chunk", "step_name": "切块入库资料池", "reason": "把正文切块，供后续确定性选证据使用。"},
            ],
            "page_search_refresh": [
                {"step_code": "page_search_bocha", "step_name": "执行 Bocha 搜索", "reason": "先获取搜索摘要结果，再决定后续抓取。"},
                {"step_code": "page_search_read", "step_name": "抓取全文并写入资料池", "reason": "把搜索结果扩展成可引用正文。"},
                {"step_code": "page_search_chunk", "step_name": "切块入库资料池", "reason": "把正文切块，供后续确定性选证据使用。"},
            ],
            "page_summary_generate": [
                {"step_code": "page_summary_generate", "step_name": "生成页面 summary", "reason": "只从当前页资料池内召回并生成摘要。"},
            ],
            "page_draft_generate": [
                {"step_code": "page_draft_generate", "step_name": "生成页面策划稿", "reason": "基于当前页 summary 生成 draft。"},
            ],
            "page_design_generate": [
                {"step_code": "page_design_generate", "step_name": "生成页面设计稿", "reason": "基于当前页 draft 生成 design。"},
            ],
        }
        return plans.get(
            action_type,
            [{"step_code": action_type, "step_name": action_type, "reason": "按按钮动作执行。"}],
        )

    def _persist_agent_message(
        self,
        *,
        run: AgentRunRecorder,
        content_md: str,
        result_snapshot: dict[str, Any] | None = None,
    ) -> ProjectMessage:
        message = self._add_message(
            project_id=run.project.id,
            stage=run.stage,
            scope_type=run.scope_type,
            target_page_id=run.target_page_id,
            role="assistant",
            content_md=content_md,
            structured_payload_json={
                "message_kind": "agent_run",
                "agent_run_id": run.agent_run_id,
                "title": run.title,
                "router_decision": run.router_decision,
                "execution_plan": run.router_decision.get("execution_plan", []) if run.router_decision else [],
                "step_results": run.step_results,
                "next_recommendations": run.next_recommendations,
                "result_snapshot": result_snapshot or {},
            },
        )
        run.emit_message(message.id)
        return message

    def _format_exception_message(self, exc: Exception) -> str:
        detail = str(exc).strip()
        if detail:
            return f"{exc.__class__.__name__}: {detail}"
        return exc.__class__.__name__

    def _finalize_run_failure(
        self,
        *,
        run: AgentRunRecorder,
        step_code: str,
        step_name: str,
        exc: Exception,
        content_md: str,
        result_snapshot: dict[str, Any] | None = None,
    ) -> None:
        error_message = self._format_exception_message(exc)
        run.step_failed(step_code, step_name, error_message)
        snapshot = {
            "error_message": error_message,
            "error_type": exc.__class__.__name__,
            **(result_snapshot or {}),
        }
        self._persist_agent_message(
            run=run,
            content_md=content_md,
            result_snapshot=snapshot,
        )
        run.complete("failed")

    def _default_recommendations_for_action(self, action_type: str) -> list[dict[str, Any]]:
        mapping = {
            "page_generate_search_queries": [
                {"code": "page_search_run", "label": "执行当前页搜索", "reason": "搜索词已经有了，下一步可以建立页级资料池。"}
            ],
            "page_search_run": [
                {"code": "page_summary_generate", "label": "生成当前页 summary", "reason": "资料池已经建立，现在可以只在当前页内做召回摘要。"}
            ],
            "page_summary_generate": [
                {"code": "page_draft_generate", "label": "生成当前页策划稿", "reason": "summary 已准备好，可以继续出稿。"}
            ],
            "page_draft_generate": [
                {"code": "page_design_generate", "label": "生成当前页设计稿", "reason": "draft 已准备好，可以继续做设计增强。"}
            ],
            "project_batch_search": [
                {"code": "project_batch_summary", "label": "批量生成 summary", "reason": "批量搜索完成后，可以继续批量做页级摘要。"}
            ],
            "project_batch_summary": [
                {"code": "project_batch_draft", "label": "批量生成策划稿", "reason": "所有页 summary 准备好后再批量出策划稿。"}
            ],
            "project_batch_draft": [
                {"code": "project_batch_design", "label": "批量生成 design", "reason": "draft 已到位后可以继续批量设计。"}
            ],
        }
        return mapping.get(action_type, [])

    def _build_rejection_message(self, decision: dict[str, Any]) -> str:
        missing_data = decision.get("missing_data") or []
        if missing_data:
            return f"这条消息现在不能执行，缺少关键信息：{', '.join(missing_data)}。"
        return decision.get("reason") or "这条消息当前不能执行。"

    def _apply_init_answer_patch(self, project: Project, requirement_form: RequirementForm, answer_patch: dict[str, Any]) -> dict[str, Any]:
        question_code = str(answer_patch.get("question_code") or "").strip()
        if not question_code:
            raise RuntimeError("answer_patch 缺少 question_code")
        value = answer_patch.get("value")
        answers = dict(requirement_form.answers_json or {})
        answers[question_code] = value
        requirement_form.answers_json = answers
        if question_code == "page_count_target":
            project.page_count_target = self._coerce_page_count(value, project.page_count_target)
        if question_code == "style_preset" and value:
            project.style_preset = str(value)
        self._apply_working_title(project, answers)
        return {"question_code": question_code, "value": value}

    def _apply_init_question_patch(self, requirement_form: RequirementForm, question_patch: dict[str, Any]) -> dict[str, Any]:
        mode = str(question_patch.get("mode") or "upsert").strip()
        questions = list(requirement_form.ai_questions_json or [])
        if mode == "delete":
            question_code = str(question_patch.get("question_code") or "").strip()
            if not question_code:
                raise RuntimeError("question_patch 缺少 question_code")
            if question_code in CLARIFICATION_CODES:
                raise RuntimeError("不能删除需求澄清问题")
            requirement_form.ai_questions_json = [item for item in questions if item.get("question_code") != question_code]
            answers = dict(requirement_form.answers_json or {})
            answers.pop(question_code, None)
            requirement_form.answers_json = answers
            return {"mode": "delete", "question_code": question_code}
        question = question_patch.get("question")
        if not isinstance(question, dict):
            raise RuntimeError("question_patch 缺少 question")
        question_code = str(question.get("question_code") or "").strip()
        if not question_code:
            raise RuntimeError("question_patch.question 缺少 question_code")
        filtered = [item for item in questions if item.get("question_code") != question_code]
        filtered.append(question)
        requirement_form.ai_questions_json = filtered
        return {"mode": "upsert", "question_code": question_code}



def _read_upload_limited(file: UploadFile, max_bytes: int) -> bytes:
    chunk_size = 1024 * 1024
    buffer = bytearray()
    while True:
        chunk = file.file.read(chunk_size)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"背景图超过大小限制（最大 {max_bytes // (1024 * 1024)}MB）",
            )
    return bytes(buffer)


def _detect_background_suffix(payload: bytes) -> str | None:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if payload.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if payload.startswith(b"GIF87a") or payload.startswith(b"GIF89a"):
        return ".gif"
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return ".webp"
    return None


def _safe_storage_id(value: str) -> str:
    if not _STORAGE_ID_RE.fullmatch(value):
        raise HTTPException(status_code=400, detail="无效的项目 ID")
    return value.lower()


def run_bootstrap_job(project_id: str) -> None:
    with session_scope() as session:
        service = PptAgentService(session)
        service.run_bootstrap_flow(project_id)


def run_outline_job(project_id: str) -> None:
    with session_scope() as session:
        service = PptAgentService(session)
        service.run_outline_flow(project_id)


def run_page_action_job(
    project_id: str,
    page_id: str,
    action_type: str,
    agent_run_id: str,
    replace_existing: bool,
) -> None:
    with session_scope() as session:
        service = PptAgentService(session)
        service.run_page_action_flow(project_id, page_id, action_type, agent_run_id, replace_existing)


def run_batch_action_job(project_id: str, action_type: str, agent_run_id: str) -> None:
    with session_scope() as session:
        service = PptAgentService(session)
        service.run_batch_action_flow(project_id, action_type, agent_run_id)


def run_message_job(message_id: str) -> None:
    with session_scope() as session:
        service = PptAgentService(session)
        service.run_message_flow(message_id)
