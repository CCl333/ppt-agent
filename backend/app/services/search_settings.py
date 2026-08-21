from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_session_factory
from app.models.entities import ModelBinding, ModelStageSettings, SearchSettings
from app.services.model_settings import mask_api_key
from app.services.reader_settings import DEFAULT_TAVILY_API_URL, env_tavily_key, env_tavily_url, normalize_endpoint

SEARCH_MODES = ("bocha", "llm", "tavily")


def mask_bocha_auth_header(value: str) -> str:
    raw = (value or "").strip()
    if raw.lower().startswith("bearer "):
        token = raw[7:].strip()
        masked = mask_api_key(token)
        return f"Bearer {masked}" if masked else ""
    return mask_api_key(raw)


def normalize_bocha_auth_header(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("bearer "):
        return raw
    return f"Bearer {raw}"


def ensure_search_settings(session: Session) -> SearchSettings:
    row = session.get(SearchSettings, "default")
    if row is not None:
        return row
    env_header = normalize_bocha_auth_header(get_settings().mcp_bocha_auth_header)
    row = SearchSettings(
        id="default",
        mode="bocha",
        bocha_auth_header=env_header,
        tavily_api_key=env_tavily_key(),
        tavily_api_url=env_tavily_url(),
    )
    session.add(row)
    session.flush()
    return row


def serialize_search_settings(session: Session) -> dict[str, object]:
    row = ensure_search_settings(session)
    stored = (row.bocha_auth_header or "").strip()
    env_header = normalize_bocha_auth_header(get_settings().mcp_bocha_auth_header)
    effective = stored or env_header
    tavily_stored = (row.tavily_api_key or "").strip()
    tavily_key = tavily_stored or env_tavily_key()
    tavily_url = (row.tavily_api_url or "").strip() or env_tavily_url() or DEFAULT_TAVILY_API_URL
    mode = row.mode if row.mode in SEARCH_MODES else "bocha"
    return {
        "mode": mode,
        "bocha_configured": bool(effective),
        "bocha_auth_header_masked": mask_bocha_auth_header(effective),
        "bocha_from_env": not stored and bool(env_header),
        "tavily_configured": bool(tavily_key),
        "tavily_api_key_masked": mask_api_key(tavily_key),
        "tavily_api_url": tavily_url,
        "tavily_from_env": not tavily_stored and bool(env_tavily_key()),
    }


def update_search_settings(session: Session, payload: dict[str, object]) -> dict[str, object]:
    row = ensure_search_settings(session)
    if "mode" in payload and payload["mode"] is not None:
        mode = str(payload["mode"]).strip()
        if mode not in SEARCH_MODES:
            raise HTTPException(status_code=422, detail="搜索方式必须是 bocha、llm 或 tavily")
        row.mode = mode
    if "bocha_auth_header" in payload:
        value = payload["bocha_auth_header"]
        if isinstance(value, str) and value.strip():
            row.bocha_auth_header = normalize_bocha_auth_header(value)
    if "tavily_api_key" in payload:
        value = payload["tavily_api_key"]
        if isinstance(value, str) and value.strip():
            row.tavily_api_key = value.strip()
    if "tavily_api_url" in payload:
        value = payload["tavily_api_url"]
        if isinstance(value, str) and value.strip():
            row.tavily_api_url = value.strip().rstrip("/")
    session.flush()
    return serialize_search_settings(session)


def search_is_ready(session: Session) -> bool:
    row = ensure_search_settings(session)
    mode = row.mode if row.mode in SEARCH_MODES else "bocha"
    if mode == "llm":
        stage = session.get(ModelStageSettings, "default")
        if stage and stage.expert_enabled:
            expert_search = str((stage.expert_bindings_json or {}).get("search") or "").strip()
            return bool(expert_search)
        binding = session.get(ModelBinding, "search")
        return binding is not None and bool(binding.provider_id)
    if mode == "tavily":
        stored = (row.tavily_api_key or "").strip()
        return bool(stored or env_tavily_key())
    stored = (row.bocha_auth_header or "").strip()
    env_header = normalize_bocha_auth_header(get_settings().mcp_bocha_auth_header)
    return bool(stored or env_header)


@dataclass(frozen=True)
class SearchRuntime:
    mode: str
    bocha_auth_header: str
    tavily_api_key: str = ""
    tavily_api_url: str = DEFAULT_TAVILY_API_URL

    @property
    def tavily_search_url(self) -> str:
        return normalize_endpoint(self.tavily_api_url, default=DEFAULT_TAVILY_API_URL, suffix="/search")


def load_search_runtime(session: Session) -> SearchRuntime:
    row = ensure_search_settings(session)
    mode = row.mode if row.mode in SEARCH_MODES else "bocha"
    stored = (row.bocha_auth_header or "").strip()
    env_header = normalize_bocha_auth_header(get_settings().mcp_bocha_auth_header)
    tavily_stored = (row.tavily_api_key or "").strip()
    tavily_url = (row.tavily_api_url or "").strip() or env_tavily_url() or DEFAULT_TAVILY_API_URL
    return SearchRuntime(
        mode=mode,
        bocha_auth_header=stored or env_header,
        tavily_api_key=tavily_stored or env_tavily_key(),
        tavily_api_url=tavily_url,
    )


def snapshot_search_runtime() -> SearchRuntime:
    session = get_session_factory()()
    try:
        return load_search_runtime(session)
    finally:
        session.close()
