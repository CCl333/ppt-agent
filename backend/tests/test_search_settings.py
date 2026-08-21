from __future__ import annotations

import pytest

from app.services.mcp_gateway import McpGateway
from app.services.model_gateway import LiveSearchDigest
from app.services.search_settings import SearchRuntime, normalize_bocha_auth_header
from tests.helpers import SAMPLE_DIGEST_MD


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


def test_normalize_bocha_auth_header():
    assert normalize_bocha_auth_header("sk-abc") == "Bearer sk-abc"
    assert normalize_bocha_auth_header("Bearer sk-abc") == "Bearer sk-abc"
    assert normalize_bocha_auth_header("  ") == ""


def test_search_settings_masks_key_and_keeps_blank(client):
    saved = client.put(
        "/api/v1/settings/search",
        json={"mode": "bocha", "bocha_auth_header": "sk-live-secret-aaa"},
    ).json()
    assert saved["mode"] == "bocha"
    assert saved["bocha_configured"] is True
    assert saved["bocha_auth_header_masked"] == "Bearer sk-***aaa"
    assert "bocha_auth_header" not in saved

    kept = client.put("/api/v1/settings/search", json={"bocha_auth_header": ""}).json()
    assert kept["bocha_auth_header_masked"] == "Bearer sk-***aaa"

    listed = client.get("/api/v1/settings/search").json()
    assert listed["bocha_configured"] is True


def test_search_settings_tavily_masks_key_and_keeps_blank(client):
    saved = client.put(
        "/api/v1/settings/search",
        json={"mode": "tavily", "tavily_api_key": "tvly-live-secret-aaa", "tavily_api_url": "https://api.tavily.com"},
    ).json()
    assert saved["mode"] == "tavily"
    assert saved["tavily_configured"] is True
    assert saved["tavily_api_key_masked"] == "tvl***aaa"
    assert saved["tavily_api_url"] == "https://api.tavily.com"
    assert "tavily_api_key" not in saved

    kept = client.put("/api/v1/settings/search", json={"tavily_api_key": ""}).json()
    assert kept["tavily_api_key_masked"] == "tvl***aaa"
    assert kept["mode"] == "tavily"


def test_search_settings_rejects_unknown_mode(client):
    response = client.put("/api/v1/settings/search", json={"mode": "bing"})
    assert response.status_code == 422
    assert "tavily" in response.json()["detail"]


def test_tavily_mode_ready_without_llm_binding(client):
    created = client.post("/api/v1/settings/models", json=_provider_payload()).json()
    client.put(
        "/api/v1/settings/model-bindings",
        json={"context": created["provider_id"], "svg": created["provider_id"]},
    )
    client.put(
        "/api/v1/settings/search",
        json={"mode": "tavily", "tavily_api_key": "tvly-live-secret-aaa"},
    )
    bound = client.get("/api/v1/settings/model-bindings").json()
    assert bound["needs_setup"] is False


def test_llm_search_needs_binding(client):
    created = client.post("/api/v1/settings/models", json=_provider_payload()).json()
    client.put("/api/v1/settings/search", json={"mode": "llm"})
    bound = client.put(
        "/api/v1/settings/model-bindings",
        json={"context": created["provider_id"], "svg": created["provider_id"]},
    ).json()
    assert bound["needs_setup"] is True
    ready = client.put(
        "/api/v1/settings/model-bindings",
        json={"search": created["provider_id"]},
    ).json()
    assert ready["needs_setup"] is False
    search = next(item for item in ready["items"] if item["role"] == "search")
    assert search["provider_id"] == created["provider_id"]


def test_bindings_put_keeps_omitted_roles(client):
    created = client.post("/api/v1/settings/models", json=_provider_payload(name="keep-roles")).json()
    client.put("/api/v1/settings/search", json={"mode": "llm"})
    client.put(
        "/api/v1/settings/model-bindings",
        json={
            "context": created["provider_id"],
            "svg": created["provider_id"],
            "search": created["provider_id"],
        },
    )
    client.put("/api/v1/settings/model-bindings", json={"context": created["provider_id"]})
    items = {
        item["role"]: item["provider_id"]
        for item in client.get("/api/v1/settings/model-bindings").json()["items"]
    }
    assert items["draft"] == created["provider_id"]
    assert items["design"] == created["provider_id"]
    assert items["search"] == created["provider_id"]


def test_search_web_bocha_uses_runtime_header(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": {"webPages": {"value": [{"name": "A", "url": "https://example.com", "snippet": "s"}]}}}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return FakeResponse()

    monkeypatch.setattr(
        "app.services.search_settings.snapshot_search_runtime",
        lambda: SearchRuntime(mode="bocha", bocha_auth_header="Bearer sk-db"),
    )
    monkeypatch.setattr("app.services.mcp_gateway.httpx.post", fake_post)
    results = McpGateway().search_web("q", limit=3)
    assert captured["headers"] == {"Authorization": "Bearer sk-db"}
    assert captured["json"]["query"] == "q"
    assert results[0].url == "https://example.com"
    assert results[0].provider == "bocha-mcp"


def test_search_web_bocha_missing_key(monkeypatch):
    monkeypatch.setattr(
        "app.services.search_settings.snapshot_search_runtime",
        lambda: SearchRuntime(mode="bocha", bocha_auth_header=""),
    )
    try:
        McpGateway().search_web("q")
        raise AssertionError("should have failed")
    except RuntimeError as exc:
        assert "未配置 Bocha 搜索鉴权信息" in str(exc)


def test_search_web_tavily_uses_runtime(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"results": [{"title": "A", "url": "https://example.com/tavily", "content": "s"}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return FakeResponse()

    monkeypatch.setattr(
        "app.services.search_settings.snapshot_search_runtime",
        lambda: SearchRuntime(
            mode="tavily",
            bocha_auth_header="",
            tavily_api_key="tvly-db",
            tavily_api_url="https://api.tavily.com",
        ),
    )
    monkeypatch.setattr("app.services.mcp_gateway.httpx.post", fake_post)
    results = McpGateway().search_web("北京旅游", limit=4)
    assert captured["url"] == "https://api.tavily.com/search"
    assert captured["headers"] == {"Authorization": "Bearer tvly-db"}
    assert captured["json"]["query"] == "北京旅游"
    assert captured["json"]["max_results"] == 4
    assert results[0].url == "https://example.com/tavily"
    assert results[0].provider == "tavily"
    assert results[0].snippet == "s"


def test_search_web_tavily_missing_key(monkeypatch):
    monkeypatch.setattr(
        "app.services.search_settings.snapshot_search_runtime",
        lambda: SearchRuntime(mode="tavily", bocha_auth_header="", tavily_api_key=""),
    )
    try:
        McpGateway().search_web("q")
        raise AssertionError("should have failed")
    except RuntimeError as exc:
        assert "未配置 Tavily 搜索 Key" in str(exc)


def test_search_web_llm_uses_bound_model(monkeypatch):
    monkeypatch.setattr(
        "app.services.search_settings.snapshot_search_runtime",
        lambda: SearchRuntime(mode="llm", bocha_auth_header=""),
    )
    monkeypatch.setattr(
        "app.services.model_gateway.ModelGateway.search_live",
        lambda self, query, limit=5: LiveSearchDigest(
            answer=SAMPLE_DIGEST_MD,
            items=[{"title": "T", "url": "https://example.com/a", "snippet": "s"}],
        ),
    )
    results = McpGateway().search_web("q", limit=5)
    assert len(results) == 1
    assert results[0].title == "T"
    assert results[0].provider == "llm-search"
    assert results[0].url == "https://example.com/a"
    assert not any(item.url.startswith("llm-search://") for item in results)


def test_search_web_llm_fails_when_items_empty(monkeypatch):
    monkeypatch.setattr(
        "app.services.search_settings.snapshot_search_runtime",
        lambda: SearchRuntime(mode="llm", bocha_auth_header=""),
    )
    monkeypatch.setattr(
        "app.services.model_gateway.ModelGateway.search_live",
        lambda self, query, limit=5: LiveSearchDigest(
            answer=SAMPLE_DIGEST_MD,
            items=[],
        ),
    )
    try:
        McpGateway().search_web("q", limit=5)
        raise AssertionError("should have failed")
    except RuntimeError as exc:
        assert "未获得任何可达来源" in str(exc)


def test_is_cache_fresh_accepts_naive_datetime():
    from datetime import datetime, timedelta, timezone

    from app.models.base import is_cache_fresh, now_utc

    aware_now = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)
    naive_future = datetime(2026, 8, 19, 12, 0, 0)
    naive_past = datetime(2026, 8, 17, 12, 0, 0)
    assert is_cache_fresh(naive_future, aware_now) is True
    assert is_cache_fresh(naive_past, aware_now) is False
    assert is_cache_fresh(None, aware_now) is True
    assert is_cache_fresh(now_utc() + timedelta(hours=1), aware_now) is True


def test_search_query_uses_sqlite_naive_cache(db_session, monkeypatch):
    from datetime import datetime

    from app.models.entities import BochaSearchCache
    from app.services.research import ResearchService

    service = ResearchService(db_session)
    query = "AI 2024"
    db_session.add(
        BochaSearchCache(
            query_key=service._query_key(query),
            query_text=query,
            result_json={"items": [{"title": "T", "url": "https://example.com/a", "snippet": "s"}]},
            result_count=1,
            expires_at=datetime(2099, 1, 1, 0, 0, 0),
        )
    )
    db_session.commit()
    db_session.expire_all()

    monkeypatch.setattr(
        service.mcp,
        "search_web",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("should use cache")),
    )
    results = service._search_query(query, 3)
    assert results[0].url == "https://example.com/a"


def test_live_search_retries_disconnect(monkeypatch):
    import httpx

    from app.services.model_gateway import _ModelConfig, _OpenAICompatibleClient

    monkeypatch.setattr("app.services.model_gateway.time.sleep", lambda *_args, **_kwargs: None)
    calls = {"n": 0}

    class FakeStreamResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def iter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"[A](https://example.com/a) s"}}]}'
            yield "data: [DONE]"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_stream(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return FakeStreamResponse()

    monkeypatch.setattr("app.services.model_gateway.httpx.stream", fake_stream)
    client = _OpenAICompatibleClient(
        _ModelConfig(
            base_url="https://rightapi.ai/grok/v1",
            api_key="sk-test",
            model="grok-4.6",
            path="/chat/completions",
            timeout_seconds=30,
        )
    )
    payload = client.live_search("q", system_prompt="sys", limit=3)
    assert calls["n"] == 2
    assert "https://example.com/a" in str(payload)


def test_connection_retry_uses_long_backoff(monkeypatch):
    import httpx

    from app.services.model_gateway import _CONNECTION_RETRY_BACKOFF, _call_with_retry

    delays: list[float] = []
    monkeypatch.setattr("app.services.model_gateway.time.sleep", delays.append)
    monkeypatch.setattr("app.services.model_gateway.random.uniform", lambda _a, _b: 0)
    calls = {"n": 0}

    def operation():
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return "ok"

    assert _call_with_retry(operation) == "ok"
    assert calls["n"] == 3
    assert delays == list(_CONNECTION_RETRY_BACKOFF)


def test_connect_timeout_retry_uses_long_backoff(monkeypatch):
    import httpx

    from app.services.model_gateway import _CONNECTION_RETRY_BACKOFF, _call_with_retry

    delays: list[float] = []
    monkeypatch.setattr("app.services.model_gateway.time.sleep", delays.append)
    monkeypatch.setattr("app.services.model_gateway.random.uniform", lambda _a, _b: 0)

    def operation():
        raise httpx.ConnectTimeout("The handshake operation timed out")

    with pytest.raises(httpx.ConnectTimeout):
        _call_with_retry(operation, max_retries=2)
    assert delays == list(_CONNECTION_RETRY_BACKOFF)


def test_http_retry_keeps_short_backoff(monkeypatch):
    import httpx

    from app.services.model_gateway import _call_with_retry

    delays: list[float] = []
    monkeypatch.setattr("app.services.model_gateway.time.sleep", delays.append)
    monkeypatch.setattr("app.services.model_gateway.random.uniform", lambda _a, _b: 0)
    request = httpx.Request("POST", "https://example.com/v1/responses")

    def operation():
        response = httpx.Response(500, request=request)
        raise httpx.HTTPStatusError("server error", request=request, response=response)

    with pytest.raises(httpx.HTTPStatusError):
        _call_with_retry(operation, max_retries=2)
    assert delays == [1.0, 2.0]


def test_grok_live_search_uses_streaming_chat_completions(monkeypatch):
    captured: dict[str, object] = {}

    class FakeStreamResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def iter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"- [A](https://example.com/a) snippet"}}]}'
            yield "data: [DONE]"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_stream(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return FakeStreamResponse()

    monkeypatch.setattr("app.services.model_gateway.httpx.stream", fake_stream)
    from app.services.model_gateway import ModelGateway, _ModelConfig, _OpenAICompatibleClient

    client = _OpenAICompatibleClient(
        _ModelConfig(
            base_url="https://rightapi.ai/grok/v1",
            api_key="sk-test",
            model="grok-4.6",
            path="/chat/completions",
            timeout_seconds=30,
        )
    )
    monkeypatch.setattr(ModelGateway, "_client", lambda self, role, required=True: client)
    items = ModelGateway().search_live("2025 AI trends", limit=4)
    body = captured["json"]
    assert captured["method"] == "POST"
    assert captured["url"] == "https://rightapi.ai/grok/v1/chat/completions"
    assert body["stream"] is True
    assert "response_format" not in body
    assert "tools" not in body
    assert items.items[0]["url"] == "https://example.com/a"
    assert items.items[0]["title"] == "A"
    assert "[A](https://example.com/a)" in items.answer


def test_grok_live_search_respects_responses_api_path(monkeypatch):
    captured: dict[str, object] = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "output_text": "- [A](https://example.com/a) snippet",
                    "output": [{"type": "web_search_call"}],
                }

        return FakeResponse()

    monkeypatch.setattr("app.services.model_gateway.httpx.post", fake_post)
    monkeypatch.setattr(
        "app.services.model_gateway.httpx.stream",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should use responses")),
    )
    from app.services.model_gateway import _ModelConfig, _OpenAICompatibleClient

    client = _OpenAICompatibleClient(
        _ModelConfig(
            base_url="https://rightapi.ai/grok/v1",
            api_key="sk-test",
            model="grok-4.6",
            path="/responses",
            timeout_seconds=30,
        )
    )
    payload = client.live_search("q", system_prompt="sys", limit=3)
    assert captured["url"] == "https://rightapi.ai/grok/v1/responses"
    assert captured["json"]["tools"][0]["type"] == "web_search"
    assert payload["output"][0]["type"] == "web_search_call"


def test_search_live_empty_payload_returns_empty_list(monkeypatch):
    from app.services.model_gateway import ModelGateway, _ModelConfig, _OpenAICompatibleClient

    class FakeClient(_OpenAICompatibleClient):
        def live_search(self, query, *, system_prompt, limit):
            return {"choices": [{"message": {"content": "没有检索结果"}}]}

    monkeypatch.setattr(
        ModelGateway,
        "_client",
        lambda self, role, required=True: FakeClient(
            _ModelConfig(
                base_url="https://rightapi.ai/grok/v1",
                api_key="sk-test",
                model="grok-4.6",
                path="/chat/completions",
                timeout_seconds=30,
            )
        ),
    )
    result = ModelGateway().search_live("q")
    assert result.items == []
    assert "没有检索结果" in result.answer


def test_search_query_summaries_keeps_results_when_one_query_empty(db_session, monkeypatch):
    from app.services.mcp_gateway import SearchResult
    from app.services.research import ResearchService

    service = ResearchService(db_session)

    def fake_search(query, limit=5):
        if "empty" in query:
            return []
        return [SearchResult(title="A", url="https://example.com/a", snippet="s", provider="llm-search")]

    monkeypatch.setattr(service.mcp, "search_web", fake_search)
    items = service.search_query_summaries(
        [
            {"query_text": "good query", "query_purpose": "定义类"},
            {"query_text": "empty query", "query_purpose": "证据类"},
        ],
        limit_per_query=3,
    )
    assert [item["url"] for item in items] == ["https://example.com/a"]


def test_search_query_summaries_fails_when_all_empty(db_session, monkeypatch):
    from app.services.research import ResearchService

    service = ResearchService(db_session)
    monkeypatch.setattr(service.mcp, "search_web", lambda *_args, **_kwargs: [])
    try:
        service.search_query_summaries([{"query_text": "q", "query_purpose": "定义类"}])
        raise AssertionError("should have failed")
    except RuntimeError as exc:
        assert "没有返回有效网页来源" in str(exc)


def test_search_query_does_not_cache_empty_results(db_session, monkeypatch):
    from app.services.research import ResearchService

    service = ResearchService(db_session)
    monkeypatch.setattr(service.mcp, "search_web", lambda *_args, **_kwargs: [])
    assert service._search_query("uncached empty", 3) == []
    from app.models.entities import BochaSearchCache
    from sqlalchemy import select

    cached = db_session.scalar(select(BochaSearchCache).where(BochaSearchCache.query_text == "uncached empty"))
    assert cached is None

