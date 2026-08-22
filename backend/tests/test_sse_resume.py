from __future__ import annotations

from app.services.events import append_event, query_events_after, serialize_event
from tests.helpers import make_project


def test_query_events_after_skips_replayed_ids(db_session):
    project = make_project(db_session, stage="design")
    first = append_event(db_session, project_id=project.id, event_type="one", stage="design", scope_type="project")
    second = append_event(db_session, project_id=project.id, event_type="two", stage="design", scope_type="project")
    third = append_event(db_session, project_id=project.id, event_type="three", stage="design", scope_type="project")
    db_session.commit()

    replayed = query_events_after(db_session, project.id, first.stream_id)
    assert [item.event_id for item in replayed] == [second.event_id, third.event_id]
    assert serialize_event(replayed[0])["stream_id"] == second.stream_id


def test_runtime_endpoint_exposes_fingerprint(client):
    health = client.get("/healthz")
    assert health.status_code == 200
    payload = health.json()
    assert payload["status"] == "ok"
    assert payload["exporter_version"] == "exporter.v1"
    assert payload["quality_eval_version"] == "quality-eval.v1"
    assert payload["scene_schema_version"] == "page-scene.v1"
    assert payload["schema_head"] == 1
    assert payload["schema_version"] == 1
    assert payload["schema_error"] is None
    runtime = client.get("/api/v1/runtime")
    assert runtime.status_code == 200
    assert runtime.json()["svg_contract_version"] == "svg-profile.v1"
    assert runtime.json()["started_at"]
