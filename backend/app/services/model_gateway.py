from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx
from openai import OpenAI

from app.core.config import Settings, get_settings
from app.models.entities import ModelProvider
from app.services.model_settings import (
    API_PATH_ANTHROPIC,
    ProviderSnapshot,
    canonicalize_api_path,
    snapshot_bound_provider,
)
from app.services.svg import extract_and_validate_svg

logger = logging.getLogger("ppt_agent.models")

_CHAT_PATHS = {"/chat/completions", "chat/completions", "/v1/chat/completions", "v1/chat/completions"}
_RESPONSES_PATHS = {"/responses", "responses", "/v1/responses", "v1/responses"}
_ANTHROPIC_PATHS = {"/messages", "messages", "/v1/messages", "v1/messages"}
_NON_RETRYABLE_STATUS = {400, 401, 403, 404, 409, 422}
_ROLE_MISSING_MESSAGES = {
    "context": "未配置文本模型，请在设置页导入并绑定",
    "svg": "未配置 SVG 模型，请在设置页导入并绑定",
    "search": "未配置搜索模型，请在设置页导入并绑定，或改用博查 Key 搜索",
}
_SEARCH_SYSTEM = (
    "你必须使用实时网页搜索，只根据检索到的公开网页作答。"
    "只输出 JSON 对象：{\"items\":[{\"title\":\"...\",\"url\":\"https://...\",\"snippet\":\"...\"}]}。"
    "url 必须是检索返回的真实链接。禁止编造来源。若没有检索结果，返回 {\"items\":[]}。"
)
_GROK_SEARCH_SYSTEM = (
    "你必须使用实时网页搜索，只根据检索到的公开网页作答。"
    "在回答末尾用 Markdown 链接列出信源，每条一行：- [标题](https://...) 一句话摘要。"
    "url 必须是检索返回的真实链接。禁止编造来源。若没有检索结果，写「没有检索结果」。"
)
_MD_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BARE_URL_PATTERN = re.compile(r"https?://[^\s<>\"'`，。、；：！？》）】\)]+")

_cache_lock = threading.Lock()
_role_clients: dict[str, tuple[str, str, "_OpenAICompatibleClient"]] = {}


def invalidate_model_cache() -> None:
    with _cache_lock:
        _role_clients.clear()


def test_provider_connection(provider: ModelProvider) -> dict[str, Any]:
    started = time.perf_counter()
    client = _OpenAICompatibleClient(_config_from_snapshot(_snapshot_from_provider(provider)))
    text = client.chat_text("You are a health check.", "Reply with the word ok.", temperature=0)
    ok = bool(text.strip())
    detail = (text or "")[:80]
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    if not ok:
        raise RuntimeError("模型返回为空")
    return {"ok": True, "latency_ms": elapsed_ms, "model": provider.model, "detail": detail}


def list_remote_models(
    *,
    base_url: str,
    api_key: str,
    api_path: str = "/chat/completions",
    timeout_seconds: int = 30,
) -> list[str]:
    base = base_url.strip().rstrip("/")
    key = api_key.strip()
    if not base or not key:
        raise RuntimeError("拉取模型列表需要 Base URL 和 API Key")
    path = canonicalize_api_path(api_path)
    urls = [f"{base}/models"]
    if not base.endswith("/v1"):
        urls.append(f"{base}/v1/models")
    headers_list = [_auth_headers(key, path)]
    if path == API_PATH_ANTHROPIC:
        headers_list.append(
            {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            }
        )
    last_error: Exception | None = None
    for url in urls:
        for headers in headers_list:
            try:
                response = httpx.get(url, headers=headers, timeout=timeout_seconds)
                response.raise_for_status()
                ids = _parse_model_ids(response.json())
                if not ids:
                    raise RuntimeError("该服务商未返回任何模型 ID")
                return ids
            except RuntimeError:
                raise
            except Exception as exc:
                last_error = exc
                continue
    detail = str(last_error) if last_error else "未知错误"
    if isinstance(last_error, httpx.HTTPStatusError) and last_error.response is not None:
        detail = f"{last_error.response.status_code} {(last_error.response.text or '')[:200]}"
    raise RuntimeError(f"拉取模型列表失败: {detail}") from last_error


def _auth_headers(api_key: str, api_path: str) -> dict[str, str]:
    if api_path in _ANTHROPIC_PATHS:
        return {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _parse_model_ids(payload: Any) -> list[str]:
    rows: list[Any]
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        data = payload.get("data")
        if data is None:
            data = payload.get("models")
        rows = data if isinstance(data, list) else []
    else:
        rows = []
    ids: list[str] = []
    seen: set[str] = set()
    for item in rows:
        raw = item if isinstance(item, str) else (item.get("id") or item.get("model") if isinstance(item, dict) else None)
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        ids.append(value)
    return ids


def _snapshot_from_provider(provider: ModelProvider) -> ProviderSnapshot:
    return ProviderSnapshot(
        provider_id=provider.provider_id,
        name=provider.name,
        base_url=provider.base_url,
        api_key=provider.api_key,
        model=provider.model,
        api_path=provider.api_path,
        timeout_seconds=provider.timeout_seconds,
        updated_at=provider.updated_at.isoformat(),
    )


def _config_from_snapshot(snapshot: ProviderSnapshot) -> "_ModelConfig":
    return _ModelConfig(
        base_url=snapshot.base_url,
        api_key=snapshot.api_key,
        model=snapshot.model,
        path=snapshot.api_path,
        timeout_seconds=snapshot.timeout_seconds,
    )


def _call_with_retry(operation: Callable[[], Any], *, max_retries: int = 2) -> Any:
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return operation()
        except httpx.HTTPStatusError as exc:
            last_error = exc
            status_code = exc.response.status_code if exc.response is not None else 0
            if status_code in _NON_RETRYABLE_STATUS or attempt >= max_retries:
                raise
        except RuntimeError:
            raise
        except Exception as exc:
            last_error = exc
            status_code = getattr(exc, "status_code", None)
            if status_code in _NON_RETRYABLE_STATUS or attempt >= max_retries:
                raise
        time.sleep((2 ** attempt) + random.uniform(0, 0.4))
    assert last_error is not None
    raise last_error


@dataclass(frozen=True)
class _ModelConfig:
    base_url: str | None
    api_key: str | None
    model: str
    path: str
    timeout_seconds: int

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.model)


class _OpenAICompatibleClient:
    def __init__(self, config: _ModelConfig):
        self.config = config
        self._sdk_client: OpenAI | None = None

    def chat_text(self, system_prompt: str, user_prompt: str, *, temperature: float = 0.2, json_mode: bool = False) -> str:
        if not self.config.enabled:
            raise RuntimeError(f"模型未启用: {self.config.model}")
        return _call_with_retry(lambda: self._chat_text_once(system_prompt, user_prompt, temperature=temperature, json_mode=json_mode))

    def _chat_text_once(self, system_prompt: str, user_prompt: str, *, temperature: float, json_mode: bool) -> str:
        path = self.config.path
        if path in _CHAT_PATHS:
            client = self._get_sdk_client()
            payload: dict[str, Any] = {
                "model": self.config.model,
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "timeout": self.config.timeout_seconds,
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            response = client.chat.completions.create(**payload)
            return response.choices[0].message.content or ""

        if path in _RESPONSES_PATHS:
            body: dict[str, Any] = {
                "model": self.config.model,
                "temperature": temperature,
                "instructions": system_prompt,
                "input": user_prompt,
            }
            if json_mode:
                body["text"] = {"format": {"type": "json_object"}}
            payload = self._post_json(body)
            return _extract_responses_text(payload)

        if path in _ANTHROPIC_PATHS:
            payload = self._post_json(
                {
                    "model": self.config.model,
                    "temperature": temperature,
                    "max_tokens": 16384,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_prompt}],
                }
            )
            return _extract_anthropic_text(payload)

        payload = self._post_json(
            {
                "model": self.config.model,
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                **({"response_format": {"type": "json_object"}} if json_mode else {}),
            }
        )
        return payload["choices"][0]["message"]["content"] or ""

    def live_search(self, query: str, *, system_prompt: str, limit: int) -> dict[str, Any]:
        if not self.config.enabled:
            raise RuntimeError(f"模型未启用: {self.config.model}")
        return _call_with_retry(lambda: self._live_search_once(query, system_prompt=system_prompt, limit=limit))

    def _live_search_once(self, query: str, *, system_prompt: str, limit: int) -> dict[str, Any]:
        path = self.config.path
        if path in _ANTHROPIC_PATHS:
            return self._post_json(
                {
                    "model": self.config.model,
                    "temperature": 0,
                    "max_tokens": 4096,
                    "system": system_prompt,
                    "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
                    "messages": [{"role": "user", "content": query}],
                }
            )
        if _looks_like_grok(self.config):
            return self._stream_chat_search(query, system_prompt=system_prompt)
        grok_body = {
            "model": self.config.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query},
            ],
            "response_format": {"type": "json_object"},
            "search_parameters": {
                "mode": "on",
                "return_citations": True,
                "max_search_results": max(limit, 8),
            },
        }
        if path in _RESPONSES_PATHS:
            return self._post_json(
                {
                    "model": self.config.model,
                    "temperature": 0,
                    "instructions": system_prompt,
                    "input": query,
                    "tools": [{"type": "web_search", "search_context_size": "medium"}],
                    "text": {"format": {"type": "json_object"}},
                }
            )
        return self._post_json(grok_body)

    def _stream_chat_search(self, query: str, *, system_prompt: str) -> dict[str, Any]:
        if not self.config.base_url:
            raise RuntimeError("模型 base_url 未配置")
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        body = {
            "model": self.config.model,
            "temperature": 0,
            "stream": True,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query},
            ],
        }
        timeout = httpx.Timeout(connect=6.0, read=float(self.config.timeout_seconds), write=10.0, pool=None)
        content = ""
        citations: list[Any] = []
        with httpx.stream(
            "POST",
            url,
            headers=_auth_headers(self.config.api_key or "", "/chat/completions"),
            json=body,
            timeout=timeout,
        ) as response:
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = ""
                try:
                    detail = (response.read() or b"").decode("utf-8", errors="replace")[:200]
                except Exception:
                    detail = str(exc)[:200]
                raise RuntimeError(
                    f"模型请求失败 ({response.status_code}): {detail}"
                ) from exc
            for line in response.iter_lines():
                text_line = line.strip() if isinstance(line, str) else str(line).strip()
                if not text_line.startswith("data:"):
                    continue
                data = text_line[5:].lstrip()
                if data in {"[DONE]", ""}:
                    continue
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                chunk_citations = chunk.get("citations")
                if isinstance(chunk_citations, list):
                    citations.extend(chunk_citations)
                choices = chunk.get("choices") or []
                if not isinstance(choices, list) or not choices:
                    continue
                first = choices[0]
                if not isinstance(first, dict):
                    continue
                delta = first.get("delta") if isinstance(first.get("delta"), dict) else {}
                content += str(delta.get("content") or "")
                message = first.get("message") if isinstance(first.get("message"), dict) else {}
                content += str(message.get("content") or "")
        return {"choices": [{"message": {"content": content}}], "citations": citations}

    def _post_json(self, body: dict[str, Any], *, url: str | None = None) -> dict[str, Any]:
        response = httpx.post(
            url or self._build_url(),
            headers=_auth_headers(self.config.api_key or "", self.config.path if url is None else "/chat/completions"),
            json=body,
            timeout=self.config.timeout_seconds,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = (exc.response.text or "")[:200] if exc.response is not None else str(exc)
            raise RuntimeError(f"模型请求失败 ({exc.response.status_code if exc.response is not None else '?'}): {detail}") from exc
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("模型返回的不是 JSON 对象")
        return payload

    def _get_sdk_client(self) -> OpenAI:
        if self._sdk_client is None:
            kwargs: dict[str, Any] = {
                "api_key": self.config.api_key or "",
                "timeout": self.config.timeout_seconds,
                "max_retries": 0,
            }
            if self.config.base_url:
                kwargs["base_url"] = self.config.base_url
            self._sdk_client = OpenAI(**kwargs)
        return self._sdk_client

    def _build_url(self) -> str:
        if not self.config.base_url:
            raise RuntimeError("模型 base_url 未配置")
        return self.config.base_url.rstrip("/") + "/" + self.config.path.lstrip("/")


def _extract_responses_text(payload: dict[str, Any]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    chunks: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"output_text", "text"}:
                chunks.append(str(part.get("text") or ""))
    text = "".join(chunks).strip()
    if not text:
        raise RuntimeError("Responses API 没有返回文本")
    return text


def _extract_anthropic_text(payload: dict[str, Any]) -> str:
    chunks: list[str] = []
    for part in payload.get("content") or []:
        if isinstance(part, dict) and part.get("type") == "text":
            chunks.append(str(part.get("text") or ""))
    text = "".join(chunks).strip()
    if not text:
        raise RuntimeError("Anthropic Messages 没有返回文本")
    return text


def _looks_like_grok(config: _ModelConfig) -> bool:
    model = (config.model or "").lower()
    url = (config.base_url or "").lower()
    return model.startswith("grok") or "x.ai" in url or "/grok" in url


def _is_live_search_unsupported(exc: Exception) -> bool:
    text = str(exc).lower()
    needles = (
        "search_parameters",
        "web_search",
        "unknown parameter",
        "unrecognized request argument",
        "does not support",
        "unknown field",
        "extra inputs are not permitted",
    )
    return any(item in text for item in needles)


def parse_search_live_payload(payload: dict[str, Any], *, limit: int) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(title: object, url: object, snippet: object) -> None:
        href = str(url or "").strip().rstrip(").,]>\"'")
        if not href.startswith("http://") and not href.startswith("https://"):
            return
        title_text = str(title or "").strip() or href
        snippet_text = str(snippet or "").strip()
        if href in seen:
            for item in items:
                if item["url"] != href:
                    continue
                if item["title"] in {href, ""} and title_text != href:
                    item["title"] = title_text
                if not item["snippet"] and snippet_text:
                    item["snippet"] = snippet_text
            return
        seen.add(href)
        items.append({"title": title_text, "url": href, "snippet": snippet_text})

    citations = payload.get("citations") or []
    if isinstance(citations, list):
        for cite in citations:
            if isinstance(cite, str):
                add(cite, cite, "")
            elif isinstance(cite, dict):
                add(cite.get("title"), cite.get("url") or cite.get("uri") or cite.get("link"), cite.get("snippet") or cite.get("text"))

    for part in payload.get("content") or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "web_search_tool_result":
            for row in part.get("content") or []:
                if isinstance(row, dict):
                    add(row.get("title"), row.get("url"), row.get("page_age") or "")
        for citation in part.get("citations") or []:
            if isinstance(citation, dict):
                add(citation.get("title"), citation.get("url"), citation.get("cited_text") or "")

    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            for annotation in part.get("annotations") or []:
                if isinstance(annotation, dict):
                    add(annotation.get("title"), annotation.get("url"), annotation.get("text") or "")
            if part.get("type") in {"url_citation", "citation"}:
                add(part.get("title"), part.get("url"), part.get("text") or "")

    text = ""
    try:
        text = payload["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        text = ""
    if not text:
        try:
            text = _extract_responses_text(payload)
        except Exception:
            try:
                text = _extract_anthropic_text(payload)
            except Exception:
                text = ""
    if text:
        parsed: Any = None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            wrapped = re.search(r"\{[\s\S]*\}", text)
            if wrapped:
                try:
                    parsed = json.loads(wrapped.group(0))
                except json.JSONDecodeError:
                    parsed = None
        if isinstance(parsed, dict):
            for row in parsed.get("items") or parsed.get("results") or []:
                if isinstance(row, dict):
                    add(row.get("title") or row.get("name"), row.get("url") or row.get("link"), row.get("snippet") or row.get("summary") or row.get("description"))
        elif isinstance(parsed, list):
            for row in parsed:
                if isinstance(row, dict):
                    add(row.get("title") or row.get("name"), row.get("url") or row.get("link"), row.get("snippet") or row.get("summary") or row.get("description"))
        for match in _MD_LINK_PATTERN.finditer(text):
            title = match.group(1)
            href = match.group(2)
            trailing = text[match.end():].splitlines()[0].strip().lstrip("-").strip()
            add(title, href, trailing)
        for href in _BARE_URL_PATTERN.findall(text):
            add(href, href, "")

    return items[:limit]


class ModelGateway:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def context_json(self, system_prompt: str, user_payload: str | dict[str, Any], *, temperature: float = 0.2) -> dict[str, Any]:
        text = self._client("context").chat_text(
            system_prompt,
            self._serialize_user_payload(user_payload),
            temperature=temperature,
            json_mode=True,
        )
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            wrapped_json = self._extract_wrapped_json(text)
            if wrapped_json is None:
                raise RuntimeError("上下文模型返回的不是有效 JSON")
            return json.loads(wrapped_json)

    def context_text(self, system_prompt: str, user_payload: str | dict[str, Any], *, temperature: float = 0.2) -> str:
        return self._client("context").chat_text(
            system_prompt,
            self._serialize_user_payload(user_payload),
            temperature=temperature,
        )

    def svg_text(self, system_prompt: str, user_payload: str | dict[str, Any], *, temperature: float = 0.2) -> str:
        text = self._client("svg").chat_text(
            system_prompt,
            self._serialize_user_payload(user_payload),
            temperature=temperature,
        )
        return extract_and_validate_svg(text)

    def search_live(self, query: str, *, limit: int = 5) -> list[dict[str, str]]:
        client = self._client("search")
        system_prompt = _GROK_SEARCH_SYSTEM if _looks_like_grok(client.config) else _SEARCH_SYSTEM
        try:
            payload = client.live_search(query, system_prompt=system_prompt, limit=limit)
        except RuntimeError as exc:
            if _is_live_search_unsupported(exc):
                raise RuntimeError(
                    "当前搜索模型不支持联网搜索。请换 Grok Live Search / OpenAI web_search / Anthropic web_search 兼容接口，或改用博查 Key。"
                ) from exc
            raise
        except httpx.RemoteProtocolError as exc:
            raise RuntimeError("搜索模型连接中断，请稍后重试") from exc
        return parse_search_live_payload(payload, limit=limit)

    def _client(self, role: str, *, required: bool = True) -> _OpenAICompatibleClient | None:
        snapshot = snapshot_bound_provider(role)
        if snapshot is None:
            fallback = self._env_fallback(role)
            if fallback.config.enabled:
                return self._cached_client(
                    role,
                    cache_key=f"env:{role}:{fallback.config.model}:{fallback.config.base_url}",
                    name="env",
                    factory=lambda: fallback,
                    model=fallback.config.model,
                    base_url=fallback.config.base_url,
                )
            if required:
                raise RuntimeError(_ROLE_MISSING_MESSAGES[role])
            return fallback
        client = self._cached_client(
            role,
            cache_key=f"{snapshot.provider_id}:{snapshot.updated_at}",
            name=snapshot.name,
            factory=lambda: _OpenAICompatibleClient(_config_from_snapshot(snapshot)),
            model=snapshot.model,
            base_url=snapshot.base_url,
        )
        if not client.config.enabled:
            if required:
                raise RuntimeError(_ROLE_MISSING_MESSAGES[role])
            return client
        return client

    def _cached_client(
        self,
        role: str,
        *,
        cache_key: str,
        name: str,
        factory: Callable[[], _OpenAICompatibleClient],
        model: str,
        base_url: str | None,
    ) -> _OpenAICompatibleClient:
        with _cache_lock:
            cached = _role_clients.get(role)
            if cached and cached[0] == cache_key:
                return cached[2]
            client = factory()
            _role_clients[role] = (cache_key, name, client)
        logger.info("using model role=%s name=%s model=%s base_url=%s", role, name, model, base_url)
        return client

    def _env_fallback(self, role: str) -> _OpenAICompatibleClient:
        settings = self.settings
        if role == "context":
            config = _ModelConfig(
                base_url=settings.context_llm_base_url,
                api_key=settings.context_llm_api_key.get_secret_value() if settings.context_llm_api_key else None,
                model=settings.context_llm_model,
                path=settings.context_llm_path,
                timeout_seconds=settings.context_llm_timeout_seconds,
            )
        elif role == "svg":
            config = _ModelConfig(
                base_url=settings.svg_llm_base_url,
                api_key=settings.svg_llm_api_key.get_secret_value() if settings.svg_llm_api_key else None,
                model=settings.svg_llm_model,
                path=settings.svg_llm_path,
                timeout_seconds=settings.svg_llm_timeout_seconds,
            )
        else:
            raise RuntimeError(_ROLE_MISSING_MESSAGES.get(role, f"未知模型角色: {role}"))
        return _OpenAICompatibleClient(config)

    def _serialize_user_payload(self, user_payload: str | dict[str, Any]) -> str:
        if isinstance(user_payload, str):
            return user_payload
        return json.dumps(user_payload, ensure_ascii=False)

    def _extract_wrapped_json(self, text: str) -> str | None:
        matched = re.search(r"\[(?P<tag>[A-Z_]+)\]\s*(?P<body>\{[\s\S]*\})\s*\[/\1\]", text)
        if matched is None:
            return None
        return matched.group("body")

    @property
    def has_search_model(self) -> bool:
        return self._is_role_enabled("search")

    @property
    def has_context_model(self) -> bool:
        return self._is_role_enabled("context")

    @property
    def has_svg_model(self) -> bool:
        return self._is_role_enabled("svg")

    def _is_role_enabled(self, role: str) -> bool:
        try:
            client = self._client(role, required=False)
        except Exception:
            return False
        return bool(client and client.config.enabled)
