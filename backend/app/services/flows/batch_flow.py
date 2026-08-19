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


class BatchFlowMixin:
    def run_batch_action_flow(self, project_id: str, action_type: str, agent_run_id: str) -> None:
        from app.services.orchestrator import AgentRunRecorder
        project = self._require_project(project_id)
        run = AgentRunRecorder(
            service=self,
            project=project,
            stage=project.current_stage if project.current_stage != "outline" else "search",
            scope_type="project",
            target_page_id=None,
            title=f"批量执行：{action_type}",
            origin="button",
            agent_run_id=agent_run_id,
        )
        run.start()
        run.set_router_decision(
            self._build_system_decision(
                scope_type="project",
                target_stage=run.stage,
                target_page_id=None,
                action_type=action_type,
                reason="用户通过按钮明确触发批量动作。",
                execution_plan=[{"step_code": action_type, "step_name": action_type, "reason": "符合条件的页并发执行，已可用且未过期的页跳过。"}],
            )
        )
        try:
            run.step_started(action_type, action_type, "把批量动作拆成页级任务并发执行。")
            tasks = enqueue_batch_action(
                self.session,
                project_id=project.id,
                action_type=action_type,
                agent_run_id=agent_run_id,
            )
            self.session.flush()
            run.step_completed(action_type, action_type, {"queued": len(tasks)})
            run.set_recommendations(self._default_recommendations_for_action(action_type))
            self._persist_agent_message(
                run=run,
                content_md=f"批量动作 `{action_type}` 已排队 {len(tasks)} 页；已可用且未过期的页已跳过。",
                result_snapshot={"queued": len(tasks)},
            )
            run.complete()
            wake_scheduler()
        except Exception as exc:
            self._finalize_run_failure(
                run=run,
                step_code=action_type,
                step_name=action_type,
                exc=exc,
                content_md=f"批量动作 `{action_type}` 执行失败。错误已经保留在当前动作卡片中。",
                result_snapshot={},
            )


