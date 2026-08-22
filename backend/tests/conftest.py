from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(tempfile.mkdtemp(prefix="ppt-agent-test-"))
_DB = _ROOT / "ppt_agent.db"
_STORAGE = _ROOT / "storage"
_STORAGE.mkdir(parents=True, exist_ok=True)

os.environ["DATABASE_URL"] = f"sqlite:///{_DB.resolve().as_posix()}"
os.environ["FILE_STORAGE_ROOT"] = str(_STORAGE.resolve())
os.environ["UPLOAD_DIR"] = "uploads"
os.environ["BACKGROUND_DIR"] = "backgrounds"
os.environ["EXPORT_DIR"] = "exports"
os.environ["RUN_JOBS_INLINE"] = "false"
os.environ["TASK_WORKER_ENABLED"] = "false"
os.environ["APP_DEBUG"] = "false"
os.environ["CORS_ORIGINS"] = "http://localhost:3000"
os.environ["TAVILY_API_KEY"] = "tvly-test-key"
os.environ["TAVILY_API_URL"] = "https://api.tavily.com"
os.environ["FIRECRAWL_API_KEY"] = "fc-test-key"
os.environ["FIRECRAWL_API_URL"] = "https://api.firecrawl.dev/v2"

from app.core.config import get_settings, reset_settings_cache
from app.core.db import get_engine, get_session_factory, init_db, reset_db_state
from app.models.base import Base
from app.services.orchestrator import PptAgentService


@pytest.fixture
def fresh_db():
    reset_settings_cache()
    reset_db_state()
    engine = get_engine()
    Base.metadata.drop_all(engine)
    get_settings().upload_path.mkdir(parents=True, exist_ok=True)
    get_settings().background_path.mkdir(parents=True, exist_ok=True)
    get_settings().export_path.mkdir(parents=True, exist_ok=True)
    get_settings().quality_eval_path.mkdir(parents=True, exist_ok=True)
    init_db()
    yield
    reset_db_state()


@pytest.fixture
def db_session(fresh_db):
    session = get_session_factory()()
    try:
        yield session
        session.rollback()
    finally:
        session.close()


@pytest.fixture
def client(fresh_db):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _stub_url_probe(monkeypatch):
    monkeypatch.setattr(
        "app.services.search_quality.probe_http_url",
        lambda url, **kwargs: str(url).startswith("http"),
    )


@pytest.fixture
def service(db_session):
    return PptAgentService(db_session)
