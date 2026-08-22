from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.migrations import ensure_schema

_ENGINE: Engine | None = None
_SESSION_FACTORY: sessionmaker[Session] | None = None
_SEEDED = False


def get_engine() -> Engine:
    global _ENGINE
    if _ENGINE is None:
        settings = get_settings()
        connect_args: dict[str, object] = {}
        if settings.database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
            connect_args["timeout"] = 10
        _ENGINE = create_engine(
            settings.database_url,
            future=True,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
        if settings.database_url.startswith("sqlite"):
            event.listen(_ENGINE, "connect", _configure_sqlite)
    return _ENGINE


def get_session_factory() -> sessionmaker[Session]:
    global _SESSION_FACTORY
    if _SESSION_FACTORY is None:
        _SESSION_FACTORY = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            future=True,
        )
    return _SESSION_FACTORY


def get_db() -> Generator[Session, None, None]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    global _SEEDED
    engine = get_engine()
    ensure_schema(engine, auto_migrate=get_settings().effective_schema_auto_migrate)
    if _SEEDED:
        return
    from app.services.model_settings import seed_models_from_env
    from app.services.style_cards import seed_style_library

    seed_models_from_env()
    with session_scope() as session:
        seed_style_library(session)
    _SEEDED = True


def reset_db_state() -> None:
    global _ENGINE, _SESSION_FACTORY, _SEEDED
    if _ENGINE is not None:
        _ENGINE.dispose()
    _ENGINE = None
    _SESSION_FACTORY = None
    _SEEDED = False


def is_sqlite_locked(exc: BaseException) -> bool:
    original = getattr(exc, "orig", None)
    text = f"{original or exc}".lower()
    return "database is locked" in text or "database table is locked" in text


def _configure_sqlite(dbapi_connection, _) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.close()
