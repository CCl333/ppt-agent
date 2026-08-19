from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings, get_settings


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    provider: str = "bocha-mcp"


@dataclass(frozen=True)
class ReadResult:
    title: str
    markdown_content: str
    provider: str
    metadata: dict[str, Any]


class McpGateway:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def search_web(self, query: str, limit: int = 5) -> list[SearchResult]:
        from app.services.search_settings import snapshot_search_runtime

        runtime = snapshot_search_runtime()
        if runtime.mode == "llm":
            return self._search_with_llm(query, limit)
        return self._search_with_bocha(query, limit, runtime.bocha_auth_header)

    def _search_with_bocha(self, query: str, limit: int, auth_header: str) -> list[SearchResult]:
        if not auth_header:
            raise RuntimeError("未配置 Bocha 搜索鉴权信息，请在设置页填写博查 Key，或改用大模型搜索")
        try:
            headers = {"Authorization": auth_header}
            response = httpx.post(
                "https://api.bochaai.com/v1/web-search",
                headers=headers,
                json={"query": query, "summary": True, "count": limit},
                timeout=20,
            )
            response.raise_for_status()
            return self._parse_bocha_results(response.json(), limit)
        except Exception as exc:
            raise RuntimeError(f"bocha search failed: {exc}") from exc

    def _search_with_llm(self, query: str, limit: int) -> list[SearchResult]:
        from app.services.model_gateway import ModelGateway

        rows = ModelGateway(self.settings).search_live(query, limit=limit)
        return [
            SearchResult(
                title=row["title"],
                url=row["url"],
                snippet=row["snippet"],
                provider="llm-search",
            )
            for row in rows
            if row.get("url")
        ]

    def read_url_markdown(self, url: str) -> ReadResult:
        from app.services.reader_settings import snapshot_reader_runtime

        runtime = snapshot_reader_runtime()
        if runtime.mode == "tavily":
            return self._read_with_tavily(url, runtime)
        if runtime.mode == "firecrawl":
            return self._read_with_firecrawl(url, runtime)
        return self._read_with_web_fetch(url, runtime)

    def _parse_bocha_results(self, payload: dict[str, Any], limit: int) -> list[SearchResult]:
        candidates = payload.get("data", payload)
        web_pages = candidates.get("webPages") or candidates.get("webpages") or candidates.get("value") or {}
        items: list[dict[str, Any]]
        if isinstance(web_pages, dict):
            items = web_pages.get("value") or web_pages.get("items") or []
        else:
            items = web_pages
        return [
            SearchResult(
                title=item.get("name") or item.get("title") or f"来源 {index}",
                url=item.get("url") or item.get("link") or "",
                snippet=item.get("snippet") or item.get("summary") or item.get("description") or "",
            )
            for index, item in enumerate(items[:limit], start=1)
            if item.get("url") or item.get("link")
        ]

    def _read_with_web_fetch(self, url: str, runtime) -> ReadResult:
        errors: list[str] = []
        if runtime.tavily_api_key:
            try:
                result = self._read_with_tavily(url, runtime)
                return ReadResult(
                    title=result.title,
                    markdown_content=result.markdown_content,
                    provider="web_fetch",
                    metadata={**result.metadata, "reader": "tavily"},
                )
            except Exception as exc:
                errors.append(f"tavily: {exc}")
        if runtime.firecrawl_api_key:
            try:
                result = self._read_with_firecrawl(url, runtime)
                return ReadResult(
                    title=result.title,
                    markdown_content=result.markdown_content,
                    provider="web_fetch",
                    metadata={**result.metadata, "reader": "firecrawl", "fallback": bool(errors)},
                )
            except Exception as exc:
                errors.append(f"firecrawl: {exc}")
        if not runtime.tavily_api_key and not runtime.firecrawl_api_key:
            raise RuntimeError("未配置 Tavily / Firecrawl，请在设置页填写解析 Key，或改用 grok-search web_fetch 所需的密钥")
        raise RuntimeError(f"grok-search web_fetch 失败: {url}: {'; '.join(errors)}")

    def _read_with_tavily(self, url: str, runtime) -> ReadResult:
        if not runtime.tavily_api_key:
            raise RuntimeError("未配置 Tavily Key，请在设置页填写")
        response = httpx.post(
            runtime.tavily_extract_url,
            headers={"Authorization": f"Bearer {runtime.tavily_api_key}"},
            json={
                "api_key": runtime.tavily_api_key,
                "urls": [url],
                "format": "markdown",
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        failed = payload.get("failed_results") or payload.get("failedUrls") or []
        results = payload.get("results") or payload.get("data") or []
        row = results[0] if isinstance(results, list) and results else {}
        markdown = str(
            row.get("raw_content")
            or row.get("markdown")
            or row.get("content")
            or payload.get("raw_content")
            or ""
        ).strip()
        if len(markdown) < 120:
            detail = failed[0] if failed else "tavily markdown too short"
            raise RuntimeError(str(detail))
        title = str(row.get("title") or "").strip() or self._extract_markdown_title(url, markdown)
        return ReadResult(
            title=title,
            markdown_content=markdown,
            provider="tavily",
            metadata={"source_url": url},
        )

    def _read_with_firecrawl(self, url: str, runtime) -> ReadResult:
        if not runtime.firecrawl_api_key:
            raise RuntimeError("未配置 Firecrawl Key，请在设置页填写")
        response = httpx.post(
            runtime.firecrawl_scrape_url,
            headers={"Authorization": f"Bearer {runtime.firecrawl_api_key}"},
            json={"url": url, "formats": ["markdown"]},
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        markdown = (
            payload.get("markdown")
            or data.get("markdown")
            or payload.get("result", {}).get("markdown")
            or ""
        ).strip()
        if len(markdown) < 120:
            raise RuntimeError("firecrawl markdown too short")
        metadata = data.get("metadata") or payload.get("metadata") or {}
        title = str(metadata.get("title") or "").strip() or self._extract_markdown_title(url, markdown)
        return ReadResult(
            title=title,
            markdown_content=markdown,
            provider="firecrawl",
            metadata={"source_url": url, "payload_meta": metadata},
        )

    def _extract_markdown_title(self, url: str, markdown: str) -> str:
        for line in markdown.splitlines():
            cleaned = re.sub(r"^Title:\s*", "", line.strip().lstrip("#").strip(), flags=re.IGNORECASE)
            if cleaned:
                return cleaned[:180]
        return url
