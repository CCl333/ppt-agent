from __future__ import annotations

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, select

from app.core.config import Settings
from app.models.entities import ModelBinding, ModelProvider
from app.services.model_gateway import ModelGateway, invalidate_model_cache
from app.services.model_settings import (
    create_provider,
    list_bindings,
    mask_api_key,
    seed_models_from_env,
    snapshot_bound_provider,
    update_provider,
    upsert_bindings,
)


def _clear_models(session) -> None:
    session.execute(delete(ModelBinding))
    session.execute(delete(ModelProvider))
    session.commit()


def _provider_payload(**overrides) -> dict:
    payload = {
        "name": "demo-a",
        "base_url": "https://example.com/v1",
        "api_key": "sk-live-secret-aaa",
        "model": "model-a",
        "api_path": "/chat/completions",
        "timeout_seconds": 30,
    }
    payload.update(overrides)
    return payload


def test_mask_api_key():
    assert mask_api_key("") == ""
    assert mask_api_key("short") == "***"
    assert mask_api_key("sk-live-secret-aaa") == "sk-***aaa"


def test_seed_from_env_creates_shared_provider_and_bindings(db_session, monkeypatch):
    _clear_models(db_session)
    settings = Settings(
        context_llm_base_url="https://seed.example/v1",
        context_llm_api_key=SecretStr("sk-seed-key-123"),
        context_llm_model="seed-chat",
        svg_llm_base_url="https://seed.example/v1",
        svg_llm_api_key=SecretStr("sk-seed-key-123"),
        svg_llm_model="seed-chat",
    )
    monkeypatch.setattr("app.services.model_settings.get_settings", lambda: settings)
    seed_models_from_env(db_session)
    db_session.commit()

    providers = list(db_session.scalars(select(ModelProvider)))
    assert len(providers) == 1
    assert providers[0].api_key == "sk-seed-key-123"
    bindings = {item.role: item.provider_id for item in db_session.scalars(select(ModelBinding))}
    assert bindings["content"] == providers[0].provider_id
    assert bindings["draft"] == providers[0].provider_id
    assert bindings["design"] == providers[0].provider_id
    assert "embedding" not in bindings
    assert "svg" not in bindings

    seed_models_from_env(db_session)
    db_session.commit()
    assert len(list(db_session.scalars(select(ModelProvider)))) == 1


def test_empty_env_seed_skips_and_needs_setup(db_session, monkeypatch):
    _clear_models(db_session)
    empty = Settings(
        context_llm_base_url=None,
        context_llm_api_key=None,
        svg_llm_base_url=None,
        svg_llm_api_key=None,
    )
    monkeypatch.setattr("app.services.model_settings.get_settings", lambda: empty)
    seed_models_from_env(db_session)
    db_session.commit()
    assert list(db_session.scalars(select(ModelProvider))) == []
    assert list_bindings(db_session)["needs_setup"] is True


def test_unconfigured_gateway_raises_setup_message(monkeypatch):
    empty = Settings(
        context_llm_base_url=None,
        context_llm_api_key=None,
        svg_llm_base_url=None,
        svg_llm_api_key=None,
    )
    invalidate_model_cache()
    monkeypatch.setattr("app.services.model_gateway.snapshot_bound_provider", lambda _role: None)
    gateway = ModelGateway(empty)
    with pytest.raises(RuntimeError, match="未配置内容策划模型"):
        gateway.context_text("sys", "user")
    with pytest.raises(RuntimeError, match="未配置初稿布局模型"):
        gateway.svg_text("sys", "user")


def test_settings_api_masks_key_and_binds(client):
    created = client.post("/api/v1/settings/models", json=_provider_payload()).json()
    assert "api_key" not in created
    assert created["api_key_masked"] == "sk-***aaa"

    listed = client.get("/api/v1/settings/models").json()["items"]
    match = next(item for item in listed if item["provider_id"] == created["provider_id"])
    assert "api_key" not in match
    assert match["api_key_masked"] == "sk-***aaa"

    client.put(
        "/api/v1/settings/model-bindings",
        json={"context": created["provider_id"], "svg": created["provider_id"]},
    )
    search = client.put(
        "/api/v1/settings/search",
        json={"mode": "bocha", "bocha_auth_header": "sk-bocha-test-key"},
    ).json()
    assert search["bocha_configured"] is True
    assert "bocha_auth_header" not in search
    bound = client.get("/api/v1/settings/model-bindings").json()
    assert bound["needs_setup"] is False
    roles = {item["role"] for item in bound["items"]}
    assert roles == {"search", "content", "draft", "design"}
    content = next(item for item in bound["items"] if item["role"] == "content")
    assert content["provider_id"] == created["provider_id"]
    assert content["provider"]["api_key_masked"] == "sk-***aaa"


def test_patch_keeps_key_when_omitted(client, db_session):
    created = client.post("/api/v1/settings/models", json=_provider_payload(name="keep-key")).json()
    patched = client.patch(
        f"/api/v1/settings/models/{created['provider_id']}",
        json={"name": "keep-key-2", "model": "model-a2"},
    ).json()
    assert patched["name"] == "keep-key-2"
    provider = db_session.get(ModelProvider, created["provider_id"])
    db_session.refresh(provider)
    assert provider.api_key == "sk-live-secret-aaa"
    assert provider.model == "model-a2"


def test_gateway_cache_follows_binding_and_patch(db_session, monkeypatch):
    _clear_models(db_session)
    first = create_provider(db_session, _provider_payload(name="first", model="model-a"))
    second = create_provider(db_session, _provider_payload(name="second", model="model-b", api_key="sk-live-secret-bbb"))
    upsert_bindings(db_session, {"content": first["provider_id"]})
    db_session.commit()
    invalidate_model_cache()

    seen: list[str] = []

    def fake_chat(self, *_args, **_kwargs):
        seen.append(self.config.model)
        return '{"ok": true}'

    monkeypatch.setattr("app.services.model_gateway._OpenAICompatibleClient.chat_text", fake_chat)
    gateway = ModelGateway()
    first_client = gateway._client("content")
    same_client = gateway._client("content")
    assert first_client is same_client
    gateway.context_json("sys", {"q": 1})
    assert seen[-1] == "model-a"

    upsert_bindings(db_session, {"content": second["provider_id"]})
    db_session.commit()
    invalidate_model_cache()
    switched = gateway._client("content")
    assert switched is not first_client
    gateway.context_json("sys", {"q": 1})
    assert seen[-1] == "model-b"

    update_provider(db_session, second["provider_id"], {"model": "model-b2"})
    db_session.commit()
    invalidate_model_cache()
    patched_client = gateway._client("content")
    assert patched_client is not switched
    assert patched_client.config.model == "model-b2"
    assert snapshot_bound_provider("content").model == "model-b2"


def test_test_endpoint_uses_gateway_helper(client, monkeypatch):
    created = client.post("/api/v1/settings/models", json=_provider_payload(name="ping")).json()

    def fake_test(provider):
        assert provider.api_key == "sk-live-secret-aaa"
        return {"ok": True, "latency_ms": 12, "model": provider.model, "detail": "ok"}

    monkeypatch.setattr("app.api.routes.settings.test_provider_connection", fake_test)
    response = client.post(f"/api/v1/settings/models/{created['provider_id']}/test")
    assert response.status_code == 200
    assert response.json()["ok"] is True

    monkeypatch.setattr(
        "app.api.routes.settings.test_provider_connection",
        lambda _provider: (_ for _ in ()).throw(RuntimeError("bad key")),
    )
    failed = client.post(f"/api/v1/settings/models/{created['provider_id']}/test")
    assert failed.status_code == 400
    assert failed.json()["detail"] == "bad key"


def test_create_canonicalizes_api_path(client):
    created = client.post(
        "/api/v1/settings/models",
        json=_provider_payload(api_path="/v1/chat/completions"),
    ).json()
    assert created["api_path"] == "/chat/completions"

    rejected = client.post(
        "/api/v1/settings/models",
        json=_provider_payload(name="bad-path", api_path="/embeddings"),
    )
    assert rejected.status_code == 400


def test_catalog_requires_base_url_and_key(client):
    missing = client.post("/api/v1/settings/models:catalog", json={"base_url": "https://example.com/v1"})
    assert missing.status_code == 400
    assert "API Key" in missing.json()["detail"]


def test_catalog_uses_stored_key(client, monkeypatch):
    created = client.post("/api/v1/settings/models", json=_provider_payload(name="catalog")).json()

    def fake_list(**kwargs):
        assert kwargs["api_key"] == "sk-live-secret-aaa"
        assert kwargs["base_url"] == "https://example.com/v1"
        return ["grok-4.6", "gpt-4o-mini"]

    monkeypatch.setattr("app.api.routes.settings.list_remote_models", fake_list)
    response = client.post(
        "/api/v1/settings/models:catalog",
        json={"provider_id": created["provider_id"]},
    )
    assert response.status_code == 200
    assert response.json()["items"] == ["grok-4.6", "gpt-4o-mini"]


def test_parse_and_extract_model_payloads():
    from app.services.model_gateway import _extract_anthropic_text, _extract_responses_text, _parse_model_ids, parse_search_live_payload

    assert _parse_model_ids({"data": [{"id": "a"}, {"id": "a"}, {"id": "b"}]}) == ["a", "b"]
    assert _extract_responses_text({"output_text": "hello"}) == "hello"
    assert _extract_anthropic_text({"content": [{"type": "text", "text": "ok"}]}) == "ok"
    items = parse_search_live_payload(
        {
            "citations": ["https://example.com/a"],
            "choices": [{"message": {"content": '{"items":[{"title":"A","url":"https://example.com/a","snippet":"s"}]}'}}],
        },
        limit=5,
    )
    assert items == [{"title": "A", "url": "https://example.com/a", "snippet": "s"}]
    assert parse_search_live_payload({"choices": [{"message": {"content": "no urls here"}}]}, limit=5) == []
    markdown_items = parse_search_live_payload(
        {
            "choices": [
                {
                    "message": {
                        "content": (
                            "观察结论如下。\n"
                            "- [Gartner 2025](https://www.gartner.com/en/newsroom/a) 战略技术趋势\n"
                            "- https://www.mckinsey.com/featured-insights/ai\n"
                        )
                    }
                }
            ]
        },
        limit=5,
    )
    assert [item["url"] for item in markdown_items] == [
        "https://www.gartner.com/en/newsroom/a",
        "https://www.mckinsey.com/featured-insights/ai",
    ]
    assert markdown_items[0]["title"] == "Gartner 2025"


def test_legacy_context_svg_bindings_migrate_to_stages(client):
    created = client.post("/api/v1/settings/models", json=_provider_payload(name="legacy")).json()
    client.put(
        "/api/v1/settings/model-bindings",
        json={"context": created["provider_id"], "svg": created["provider_id"]},
    )
    bound = client.get("/api/v1/settings/model-bindings").json()
    by_role = {item["role"]: item["provider_id"] for item in bound["items"]}
    assert by_role["content"] == created["provider_id"]
    assert by_role["draft"] == created["provider_id"]
    assert by_role["design"] == created["provider_id"]
    assert "context" not in by_role
    assert "svg" not in by_role


def test_expert_preset_uses_expert_binding_not_live(db_session, monkeypatch):
    _clear_models(db_session)
    live = create_provider(db_session, _provider_payload(name="live", model="live-model"))
    expert = create_provider(db_session, _provider_payload(name="expert", model="expert-model", api_key="sk-live-secret-exp"))
    upsert_bindings(
        db_session,
        {
            "content": live["provider_id"],
            "draft": live["provider_id"],
            "design": live["provider_id"],
            "expert_enabled": True,
            "expert": {
                "content": expert["provider_id"],
                "draft": expert["provider_id"],
                "design": expert["provider_id"],
            },
        },
    )
    db_session.commit()
    invalidate_model_cache()
    seen: list[str] = []

    def fake_chat(self, *_args, **_kwargs):
        seen.append(self.config.model)
        return '{"ok": true}'

    monkeypatch.setattr("app.services.model_gateway._OpenAICompatibleClient.chat_text", fake_chat)
    gateway = ModelGateway()
    gateway.context_json("sys", {"q": 1})
    assert seen[-1] == "expert-model"
    assert snapshot_bound_provider("content").model == "expert-model"


def test_expert_missing_slot_does_not_use_env_fallback(db_session, monkeypatch):
    _clear_models(db_session)
    live = create_provider(db_session, _provider_payload(name="live", model="live-model"))
    upsert_bindings(
        db_session,
        {
            "content": live["provider_id"],
            "draft": live["provider_id"],
            "design": live["provider_id"],
            "expert_enabled": True,
        },
    )
    db_session.commit()
    invalidate_model_cache()
    gateway = ModelGateway()
    with pytest.raises(RuntimeError, match="专家档未绑定内容策划"):
        gateway.context_text("sys", "user")
