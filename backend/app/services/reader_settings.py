from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_session_factory
from app.models.entities import ReaderSettings
from app.services.model_settings import mask_api_key

READER_MODES = ("tavily", "firecrawl", "web_fetch")
DEFAULT_TAVILY_API_URL = "https://api.tavily.com"
DEFAULT_FIRECRAWL_API_URL = "https://api.firecrawl.dev/v2"


def normalize_endpoint(value: str | None, *, default: str, suffix: str) -> str:
    raw = (value or "").strip().rstrip("/")
    base = raw or default.rstrip("/")
    if base.endswith(suffix):
        return base
    return f"{base}{suffix}"


def env_tavily_key() -> str:
    return (get_settings().tavily_api_key or "").strip()


def env_tavily_url() -> str:
    return (get_settings().tavily_api_url or "").strip()


def env_firecrawl_key() -> str:
    return (get_settings().firecrawl_api_key or "").strip()


def env_firecrawl_url() -> str:
    settings = get_settings()
    return (settings.firecrawl_api_url or settings.mcp_firecrawl_url or "").strip()


def ensure_reader_settings(session: Session) -> ReaderSettings:
    row = session.get(ReaderSettings, "default")
    if row is not None:
        return row
    row = ReaderSettings(
        id="default",
        mode="web_fetch",
        tavily_api_key=env_tavily_key(),
        tavily_api_url=env_tavily_url(),
        firecrawl_api_key=env_firecrawl_key(),
        firecrawl_api_url=env_firecrawl_url(),
    )
    session.add(row)
    session.flush()
    return row


def serialize_reader_settings(session: Session) -> dict[str, object]:
    row = ensure_reader_settings(session)
    tavily_stored = (row.tavily_api_key or "").strip()
    firecrawl_stored = (row.firecrawl_api_key or "").strip()
    tavily_key = tavily_stored or env_tavily_key()
    firecrawl_key = firecrawl_stored or env_firecrawl_key()
    tavily_url = (row.tavily_api_url or "").strip() or env_tavily_url() or DEFAULT_TAVILY_API_URL
    firecrawl_url = (row.firecrawl_api_url or "").strip() or env_firecrawl_url() or DEFAULT_FIRECRAWL_API_URL
    mode = row.mode if row.mode in READER_MODES else "web_fetch"
    return {
        "mode": mode,
        "tavily_configured": bool(tavily_key),
        "tavily_api_key_masked": mask_api_key(tavily_key),
        "tavily_api_url": tavily_url,
        "tavily_from_env": not tavily_stored and bool(env_tavily_key()),
        "firecrawl_configured": bool(firecrawl_key),
        "firecrawl_api_key_masked": mask_api_key(firecrawl_key),
        "firecrawl_api_url": firecrawl_url,
        "firecrawl_from_env": not firecrawl_stored and bool(env_firecrawl_key()),
        "ready": reader_is_ready(session),
    }


def update_reader_settings(session: Session, payload: dict[str, object]) -> dict[str, object]:
    row = ensure_reader_settings(session)
    if "mode" in payload and payload["mode"] is not None:
        mode = str(payload["mode"]).strip()
        if mode not in READER_MODES:
            raise HTTPException(status_code=422, detail="解析方式必须是 tavily、firecrawl 或 web_fetch")
        row.mode = mode
    if "tavily_api_key" in payload:
        value = payload["tavily_api_key"]
        if isinstance(value, str) and value.strip():
            row.tavily_api_key = value.strip()
    if "tavily_api_url" in payload:
        value = payload["tavily_api_url"]
        if isinstance(value, str) and value.strip():
            row.tavily_api_url = value.strip().rstrip("/")
    if "firecrawl_api_key" in payload:
        value = payload["firecrawl_api_key"]
        if isinstance(value, str) and value.strip():
            row.firecrawl_api_key = value.strip()
    if "firecrawl_api_url" in payload:
        value = payload["firecrawl_api_url"]
        if isinstance(value, str) and value.strip():
            row.firecrawl_api_url = value.strip().rstrip("/")
    session.flush()
    return serialize_reader_settings(session)


def reader_is_ready(session: Session) -> bool:
    runtime = load_reader_runtime(session)
    if runtime.mode == "tavily":
        return bool(runtime.tavily_api_key)
    if runtime.mode == "firecrawl":
        return bool(runtime.firecrawl_api_key)
    return bool(runtime.tavily_api_key or runtime.firecrawl_api_key)


@dataclass(frozen=True)
class ReaderRuntime:
    mode: str
    tavily_api_key: str
    tavily_api_url: str
    firecrawl_api_key: str
    firecrawl_api_url: str

    @property
    def tavily_extract_url(self) -> str:
        return normalize_endpoint(self.tavily_api_url, default=DEFAULT_TAVILY_API_URL, suffix="/extract")

    @property
    def firecrawl_scrape_url(self) -> str:
        return normalize_endpoint(self.firecrawl_api_url, default=DEFAULT_FIRECRAWL_API_URL, suffix="/scrape")


def load_reader_runtime(session: Session) -> ReaderRuntime:
    row = ensure_reader_settings(session)
    mode = row.mode if row.mode in READER_MODES else "web_fetch"
    return ReaderRuntime(
        mode=mode,
        tavily_api_key=(row.tavily_api_key or "").strip() or env_tavily_key(),
        tavily_api_url=(row.tavily_api_url or "").strip() or env_tavily_url() or DEFAULT_TAVILY_API_URL,
        firecrawl_api_key=(row.firecrawl_api_key or "").strip() or env_firecrawl_key(),
        firecrawl_api_url=(row.firecrawl_api_url or "").strip() or env_firecrawl_url() or DEFAULT_FIRECRAWL_API_URL,
    )


def snapshot_reader_runtime() -> ReaderRuntime:
    session = get_session_factory()()
    try:
        return load_reader_runtime(session)
    finally:
        session.close()
