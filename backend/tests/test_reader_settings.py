from __future__ import annotations

from app.services.mcp_gateway import McpGateway
from app.services.reader_settings import ReaderRuntime, normalize_endpoint


LONG_MARKDOWN = ("全文段落。" * 40).strip()


def test_normalize_endpoint_appends_suffix():
    assert normalize_endpoint("", default="https://api.tavily.com", suffix="/extract") == "https://api.tavily.com/extract"
    assert (
        normalize_endpoint("https://api.tavily.com/extract", default="https://api.tavily.com", suffix="/extract")
        == "https://api.tavily.com/extract"
    )
    assert (
        normalize_endpoint("https://api.firecrawl.dev/v2", default="https://api.firecrawl.dev/v2", suffix="/scrape")
        == "https://api.firecrawl.dev/v2/scrape"
    )


def test_reader_settings_masks_key_and_keeps_blank(client):
    saved = client.put(
        "/api/v1/settings/reader",
        json={
            "mode": "web_fetch",
            "tavily_api_key": "tvly-live-secret-aaa",
            "firecrawl_api_key": "fc-live-secret-bbb",
            "tavily_api_url": "https://api.tavily.com",
            "firecrawl_api_url": "https://api.firecrawl.dev/v2",
        },
    ).json()
    assert saved["mode"] == "web_fetch"
    assert saved["tavily_configured"] is True
    assert saved["firecrawl_configured"] is True
    assert saved["tavily_api_key_masked"] == "tvl***aaa"
    assert saved["firecrawl_api_key_masked"] == "fc-***bbb"
    assert "tavily_api_key" not in saved
    assert "firecrawl_api_key" not in saved
    assert saved["ready"] is True

    kept = client.put("/api/v1/settings/reader", json={"tavily_api_key": "", "firecrawl_api_key": ""}).json()
    assert kept["tavily_api_key_masked"] == "tvl***aaa"
    assert kept["firecrawl_api_key_masked"] == "fc-***bbb"


def test_reader_settings_rejects_invalid_mode(client):
    response = client.put("/api/v1/settings/reader", json={"mode": "jina"})
    assert response.status_code == 422


def test_read_url_tavily(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"results": [{"title": "AI Index", "raw_content": LONG_MARKDOWN}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return FakeResponse()

    monkeypatch.setattr(
        "app.services.reader_settings.snapshot_reader_runtime",
        lambda: ReaderRuntime(
            mode="tavily",
            tavily_api_key="tvly-db",
            tavily_api_url="https://api.tavily.com",
            firecrawl_api_key="",
            firecrawl_api_url="https://api.firecrawl.dev/v2",
        ),
    )
    monkeypatch.setattr("app.services.mcp_gateway.httpx.post", fake_post)
    result = McpGateway().read_url_markdown("https://example.com/a")
    assert captured["url"] == "https://api.tavily.com/extract"
    assert captured["headers"] == {"Authorization": "Bearer tvly-db"}
    assert captured["json"]["urls"] == ["https://example.com/a"]
    assert result.provider == "tavily"
    assert result.title == "AI Index"
    assert result.markdown_content == LONG_MARKDOWN


def test_read_url_firecrawl(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": {"markdown": LONG_MARKDOWN, "metadata": {"title": "Report"}}}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return FakeResponse()

    monkeypatch.setattr(
        "app.services.reader_settings.snapshot_reader_runtime",
        lambda: ReaderRuntime(
            mode="firecrawl",
            tavily_api_key="",
            tavily_api_url="https://api.tavily.com",
            firecrawl_api_key="fc-db",
            firecrawl_api_url="https://api.firecrawl.dev/v2",
        ),
    )
    monkeypatch.setattr("app.services.mcp_gateway.httpx.post", fake_post)
    result = McpGateway().read_url_markdown("https://example.com/a")
    assert captured["url"] == "https://api.firecrawl.dev/v2/scrape"
    assert captured["headers"] == {"Authorization": "Bearer fc-db"}
    assert captured["json"] == {"url": "https://example.com/a", "formats": ["markdown"]}
    assert result.provider == "firecrawl"
    assert result.title == "Report"


def test_read_url_web_fetch_falls_back_to_firecrawl(monkeypatch):
    calls: list[str] = []

    class FirecrawlResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": {"markdown": LONG_MARKDOWN, "metadata": {"title": "Fallback"}}}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(url)
        if "tavily" in url:
            raise RuntimeError("tavily refused")
        return FirecrawlResponse()

    monkeypatch.setattr(
        "app.services.reader_settings.snapshot_reader_runtime",
        lambda: ReaderRuntime(
            mode="web_fetch",
            tavily_api_key="tvly-db",
            tavily_api_url="https://api.tavily.com",
            firecrawl_api_key="fc-db",
            firecrawl_api_url="https://api.firecrawl.dev/v2",
        ),
    )
    monkeypatch.setattr("app.services.mcp_gateway.httpx.post", fake_post)
    result = McpGateway().read_url_markdown("https://example.com/a")
    assert calls == ["https://api.tavily.com/extract", "https://api.firecrawl.dev/v2/scrape"]
    assert result.provider == "web_fetch"
    assert result.metadata["reader"] == "firecrawl"
    assert result.title == "Fallback"


def test_read_url_web_fetch_missing_keys(monkeypatch):
    monkeypatch.setattr(
        "app.services.reader_settings.snapshot_reader_runtime",
        lambda: ReaderRuntime(
            mode="web_fetch",
            tavily_api_key="",
            tavily_api_url="https://api.tavily.com",
            firecrawl_api_key="",
            firecrawl_api_url="https://api.firecrawl.dev/v2",
        ),
    )
    try:
        McpGateway().read_url_markdown("https://example.com/a")
        raise AssertionError("should have failed")
    except RuntimeError as exc:
        assert "未配置 Tavily / Firecrawl" in str(exc)
