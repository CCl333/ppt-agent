from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_debug: bool = False
    api_prefix: str = "/api/v1"
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )
    database_url: str = "sqlite:///./backend/data/ppt_agent.db"
    file_storage_root: Path = Path("backend/data")
    upload_dir: str = "uploads"
    background_dir: str = "backgrounds"
    export_dir: str = "exports"

    context_llm_base_url: str | None = None
    context_llm_api_key: SecretStr | None = None
    context_llm_model: str = "gpt-4o-mini"
    context_llm_path: str = "/chat/completions"
    context_llm_timeout_seconds: int = 60

    svg_llm_base_url: str | None = None
    svg_llm_api_key: SecretStr | None = None
    svg_llm_model: str = "gpt-4o-mini"
    svg_llm_path: str = "/chat/completions"
    svg_llm_timeout_seconds: int = 60

    mcp_bocha_url: str | None = None
    mcp_bocha_auth_header: str | None = None
    mcp_fetch_url: str | None = None
    mcp_firecrawl_url: str | None = None
    mcp_markitdown_url: str | None = None
    tavily_api_key: str | None = None
    tavily_api_url: str | None = None
    firecrawl_api_key: str | None = None
    firecrawl_api_url: str | None = None

    max_research_concurrency: int = 4
    event_stream_replay_limit: int = 100
    run_jobs_inline: bool = False
    task_worker_enabled: bool = True
    task_worker_count: int = 2
    task_lease_seconds: int = 180
    task_poll_interval_ms: int = 400

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, list):
            return value
        if not value:
            return []
        return [item.strip() for item in value.split(",") if item.strip()]

    @property
    def upload_path(self) -> Path:
        return self.file_storage_root / self.upload_dir

    @property
    def background_path(self) -> Path:
        return self.file_storage_root / self.background_dir

    @property
    def export_path(self) -> Path:
        return self.file_storage_root / self.export_dir


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
