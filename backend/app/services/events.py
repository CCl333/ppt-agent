from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.entities import ProjectEvent


def append_event(
    session: Session,
    *,
    project_id: str,
    event_type: str,
    stage: str,
    scope_type: str,
    target_page_id: str | None = None,
    payload: dict[str, Any] | None = None,
    agent_run_id: str | None = None,
) -> ProjectEvent:
    event = ProjectEvent(
        project_id=project_id,
        event_type=event_type,
        stage=stage,
        scope_type=scope_type,
        target_page_id=target_page_id,
        payload_json=payload or {},
        agent_run_id=agent_run_id,
    )
    session.add(event)
    session.flush()
    return event


def query_events_after(session: Session, project_id: str, after_id: int, limit: int = 100) -> list[ProjectEvent]:
    from sqlalchemy import select

    return list(
        session.scalars(
            select(ProjectEvent)
            .where(ProjectEvent.project_id == project_id, ProjectEvent.stream_id > after_id)
            .order_by(ProjectEvent.stream_id.asc())
            .limit(max(1, limit))
        )
    )


def serialize_event(event: ProjectEvent) -> dict[str, Any]:
    return {
        "stream_id": event.stream_id,
        "event_id": event.event_id,
        "event_type": event.event_type,
        "project_id": event.project_id,
        "stage": event.stage,
        "scope_type": event.scope_type,
        "target_page_id": event.target_page_id,
        "agent_run_id": event.agent_run_id,
        "payload": event.payload_json,
        "created_at": event.created_at.isoformat(),
    }

