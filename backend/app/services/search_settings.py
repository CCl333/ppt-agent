from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_session_factory
from app.models.entities import ModelBinding, SearchSettings
from app.services.model_settings import mask_api_key

SEARCH_MODES = ("bocha", "llm")

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
    row = SearchSettings(id="default", mode="bocha", bocha_auth_header=env_header)
    session.add(row)
    session.flush()
    return row


def serialize_search_settings(session: Session) -> dict[str, object]:
    row = ensure_search_settings(session)
    stored = (row.bocha_auth_header or "").strip()
    env_header = normalize_bocha_auth_header(get_settings().mcp_bocha_auth_header)
    effective = stored or env_header
    mode = row.mode if row.mode in SEARCH_MODES else "bocha"
    return {
        "mode": mode,
        "bocha_configured": bool(effective),
        "bocha_auth_header_masked": mask_bocha_auth_header(effective),
        "bocha_from_env": not stored and bool(env_header),
    }


def update_search_settings(session: Session, payload: dict[str, object]) -> dict[str, object]:
    row = ensure_search_settings(session)
    if "mode" in payload and payload["mode"] is not None:
        mode = str(payload["mode"]).strip()
        if mode not in SEARCH_MODES:
            raise HTTPException(status_code=422, detail="搜索方式必须是 bocha 或 llm")
        row.mode = mode
    if "bocha_auth_header" in payload:
        value = payload["bocha_auth_header"]
        if isinstance(value, str) and value.strip():
            row.bocha_auth_header = normalize_bocha_auth_header(value)
    session.flush()
    return serialize_search_settings(session)


def search_is_ready(session: Session) -> bool:
    row = ensure_search_settings(session)
    mode = row.mode if row.mode in SEARCH_MODES else "bocha"
    if mode == "llm":
        binding = session.get(ModelBinding, "search")
        return binding is not None and bool(binding.provider_id)
    stored = (row.bocha_auth_header or "").strip()
    env_header = normalize_bocha_auth_header(get_settings().mcp_bocha_auth_header)
    return bool(stored or env_header)


@dataclass(frozen=True)
class SearchRuntime:
    mode: str
    bocha_auth_header: str


def load_search_runtime(session: Session) -> SearchRuntime:
    row = ensure_search_settings(session)
    mode = row.mode if row.mode in SEARCH_MODES else "bocha"
    stored = (row.bocha_auth_header or "").strip()
    env_header = normalize_bocha_auth_header(get_settings().mcp_bocha_auth_header)
    return SearchRuntime(mode=mode, bocha_auth_header=stored or env_header)


def snapshot_search_runtime() -> SearchRuntime:
    session = get_session_factory()()
    try:
        return load_search_runtime(session)
    finally:
        session.close()
