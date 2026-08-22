from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.migrations import (
    Migration,
    SchemaMigrationError,
    SchemaVersionError,
    apply_migrations,
    backup_sqlite_database,
    current_schema_version,
    ensure_schema,
    head_schema_version,
    inspect_schema,
)
from app.models.entities import Project

_POSTGRES_URL = (os.environ.get("PPT_POSTGRES_TEST_URL") or os.environ.get("TEST_DATABASE_URL") or "").strip()


def _configure_sqlite(dbapi_connection, _) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _sqlite_engine(tmp_path: Path, name: str = "schema.db") -> Engine:
    path = tmp_path / name
    engine = create_engine(f"sqlite:///{path.resolve().as_posix()}", future=True)
    event.listen(engine, "connect", _configure_sqlite)
    return engine


def _legacy_projects_sql() -> str:
    return """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            title TEXT,
            request_text TEXT,
            current_stage TEXT,
            latest_checkpoint_code TEXT,
            checkpoint_status TEXT
        )
        """


def test_empty_sqlite_migrates_to_head_and_accepts_orm_writes(tmp_path: Path):
    engine = _sqlite_engine(tmp_path)
    result = apply_migrations(engine)
    assert result.from_version == 0
    assert result.to_version == head_schema_version()
    assert result.applied == ["001_baseline"]
    assert inspect_schema(engine).at_head

    factory = sessionmaker(bind=engine, future=True)
    session = factory()
    try:
        session.add(Project(title="迁移演练", request_text="empty-db"))
        session.commit()
        assert session.query(Project).count() == 1
    finally:
        session.close()
    engine.dispose()


def test_apply_migrations_is_idempotent_at_head(tmp_path: Path):
    engine = _sqlite_engine(tmp_path)
    apply_migrations(engine)
    second = apply_migrations(engine)
    assert second.from_version == head_schema_version()
    assert second.to_version == head_schema_version()
    assert second.applied == []
    engine.dispose()


def test_preversioned_sqlite_gains_missing_columns_and_drops_dead_ones(tmp_path: Path):
    engine = _sqlite_engine(tmp_path)
    with engine.begin() as conn:
        conn.execute(text(_legacy_projects_sql()))
        conn.execute(
            text(
                """
                CREATE TABLE source_chunks (
                    id TEXT PRIMARY KEY,
                    source_document_id TEXT,
                    chunk_index INTEGER,
                    section_path TEXT,
                    content_md TEXT,
                    content_for_embedding TEXT,
                    token_count INTEGER
                )
                """
            )
        )
        conn.execute(
            text("INSERT INTO projects (id, title, request_text) VALUES ('p1', '旧库', 'legacy')")
        )

    status = inspect_schema(engine)
    assert status.empty is False
    assert status.current is None
    assert any("style_card_json" in item for item in status.drift)

    with pytest.raises(SchemaVersionError, match="will not alter tables at startup"):
        ensure_schema(engine, auto_migrate=False)

    result = ensure_schema(engine, auto_migrate=True)
    assert result.applied == ["001_baseline"]
    assert inspect_schema(engine).at_head

    columns = {item["name"] for item in inspect(engine).get_columns("projects")}
    assert "style_card_json" in columns
    assert "workflow_constraints_json" in columns
    chunk_columns = {item["name"] for item in inspect(engine).get_columns("source_chunks")}
    assert "content_for_match" in chunk_columns
    assert "content_for_embedding" not in chunk_columns
    assert "batch_runs" in inspect(engine).get_table_names()

    with engine.connect() as conn:
        title = conn.execute(text("SELECT title FROM projects WHERE id = 'p1'")).scalar()
    assert title == "旧库"
    engine.dispose()


def test_empty_database_initializes_even_when_auto_migrate_is_off(tmp_path: Path):
    engine = _sqlite_engine(tmp_path)
    result = ensure_schema(engine, auto_migrate=False)
    assert result.to_version == head_schema_version()
    assert inspect_schema(engine).at_head
    engine.dispose()


def test_head_drift_fails_instead_of_silent_alter(tmp_path: Path):
    engine = _sqlite_engine(tmp_path)
    apply_migrations(engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE projects DROP COLUMN style_card_json"))

    status = inspect_schema(engine)
    assert status.current == head_schema_version()
    assert "missing column projects.style_card_json" in status.drift

    with pytest.raises(SchemaVersionError, match="Add a numbered migration"):
        ensure_schema(engine, auto_migrate=True)
    engine.dispose()


def test_failed_migration_does_not_stamp_or_continue(tmp_path: Path):
    engine = _sqlite_engine(tmp_path)
    boom_state = {"ran_later": False}

    def ok(conn: Connection) -> None:
        conn.execute(text("CREATE TABLE ok_step (id INTEGER PRIMARY KEY)"))

    def boom(conn: Connection) -> None:
        conn.execute(text("CREATE TABLE boom_step (id INTEGER PRIMARY KEY)"))
        raise RuntimeError("boom")

    def later(conn: Connection) -> None:
        boom_state["ran_later"] = True
        conn.execute(text("CREATE TABLE later_step (id INTEGER PRIMARY KEY)"))

    chain = (
        Migration(1, "ok", ok),
        Migration(2, "boom", boom),
        Migration(3, "later", later),
    )
    with pytest.raises(SchemaMigrationError, match="002_boom"):
        apply_migrations(engine, migrations=chain)

    assert current_schema_version(engine) == 1
    tables = set(inspect(engine).get_table_names())
    assert "ok_step" in tables
    assert "later_step" not in tables
    assert boom_state["ran_later"] is False
    engine.dispose()


def test_newer_database_is_rejected(tmp_path: Path):
    engine = _sqlite_engine(tmp_path)
    apply_migrations(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO schema_migrations (version, name, applied_at) "
                "VALUES (99, 'future', '2026-08-23T00:00:00+00:00')"
            )
        )
    with pytest.raises(SchemaVersionError, match="newer than this build"):
        ensure_schema(engine, auto_migrate=False)
    engine.dispose()


def test_sqlite_backup_uses_online_backup_api(tmp_path: Path):
    engine = _sqlite_engine(tmp_path, "live.db")
    apply_migrations(engine)
    factory = sessionmaker(bind=engine, future=True)
    session = factory()
    try:
        session.add(Project(title="backup-row", request_text="keep-me"))
        session.commit()
    finally:
        session.close()
    url = f"sqlite:///{(tmp_path / 'live.db').resolve().as_posix()}"
    backup = backup_sqlite_database(url, suffix="test")
    assert backup is not None
    backup_engine = create_engine(f"sqlite:///{backup.resolve().as_posix()}", future=True)
    try:
        titles = backup_engine.connect().execute(text("SELECT title FROM projects")).scalars().all()
        assert "backup-row" in titles
    finally:
        backup_engine.dispose()
        engine.dispose()


def test_cli_reports_drift_at_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = _sqlite_engine(tmp_path, "cli.db")
    apply_migrations(engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE projects DROP COLUMN style_card_json"))
    monkeypatch.setattr("app.core.db.get_engine", lambda: engine)
    monkeypatch.setattr(
        "app.core.config.get_settings",
        lambda: type("Settings", (), {"database_url": f"sqlite:///{(tmp_path / 'cli.db').resolve().as_posix()}"})(),
    )
    from app.core.migrations import main

    assert main(["--check", "--no-backup"]) == 1
    assert main(["--no-backup"]) == 1
    engine.dispose()


def test_duplicate_migration_versions_are_rejected(tmp_path: Path):
    engine = _sqlite_engine(tmp_path)
    chain = (
        Migration(1, "one", lambda _conn: None),
        Migration(1, "again", lambda _conn: None),
    )
    with pytest.raises(SchemaVersionError, match="duplicate"):
        apply_migrations(engine, migrations=chain)
    engine.dispose()


def test_migrate_cli_check_then_upgrade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = _sqlite_engine(tmp_path, "cli.db")
    monkeypatch.setattr("app.core.db.get_engine", lambda: engine)
    monkeypatch.setattr(
        "app.core.config.get_settings",
        lambda: type("Settings", (), {"database_url": f"sqlite:///{(tmp_path / 'cli.db').resolve().as_posix()}"})(),
    )
    from app.core.migrations import main

    assert main(["--check", "--no-backup"]) == 1
    assert main(["--no-backup"]) == 0
    assert main(["--check", "--no-backup"]) == 0
    assert inspect_schema(engine).at_head
    engine.dispose()


def test_init_db_is_idempotent(fresh_db):
    from app.core.db import get_engine, init_db

    init_db()
    status = inspect_schema(get_engine())
    assert status.at_head
    assert status.current == 1


@pytest.mark.skipif(not _POSTGRES_URL, reason="Postgres optional, not a gate")
def test_optional_postgres_baseline_migration():
    engine = create_engine(_POSTGRES_URL, future=True)
    try:
        apply_migrations(engine)
        assert inspect_schema(engine).at_head
        factory = sessionmaker(bind=engine, future=True)
        session: Session = factory()
        try:
            session.add(Project(title="pg optional", request_text="optional"))
            session.commit()
        finally:
            session.close()
    finally:
        engine.dispose()
