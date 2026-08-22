from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from sqlalchemy import DateTime, Integer, Text, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, now_utc

SCHEMA_MIGRATIONS_TABLE = "schema_migrations"

# Pre-versioned SQLite files may still carry this dead column.
_LEGACY_DROPS: dict[str, tuple[str, ...]] = {
    "source_chunks": ("content_for_embedding",),
}


class SchemaVersionError(RuntimeError):
    """Database schema is empty, behind, ahead, or drifted from the ORM."""


class SchemaMigrationError(RuntimeError):
    """A numbered migration failed; later versions were not applied."""


class SchemaMigration(Base):
    __tablename__ = SCHEMA_MIGRATIONS_TABLE

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    upgrade: Callable[[Connection], None]


@dataclass(frozen=True)
class SchemaStatus:
    current: int | None
    head: int
    empty: bool
    drift: tuple[str, ...] = ()

    @property
    def at_head(self) -> bool:
        return (not self.empty) and self.current == self.head and not self.drift


@dataclass
class MigrationResult:
    from_version: int
    to_version: int
    applied: list[str] = field(default_factory=list)


def _load_models() -> None:
    from app.models import entities  # noqa: F401


def _upgrade_001_baseline(conn: Connection) -> None:
    """Current ORM schema as the first versioned baseline.

    Empty databases get create_all. Pre-versioned databases (create_all +
    handwritten ALTER) catch up missing tables/columns once, then stop using
    scattered column lists.
    """
    _load_models()
    Base.metadata.create_all(bind=conn)
    _add_missing_orm_columns(conn)
    _drop_legacy_columns(conn)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "baseline", _upgrade_001_baseline),
)


def head_schema_version(migrations: Sequence[Migration] | None = None) -> int:
    chain = migrations if migrations is not None else MIGRATIONS
    if not chain:
        raise SchemaVersionError("no schema migrations are registered")
    return chain[-1].version


def current_schema_version(engine: Engine) -> int | None:
    inspector = inspect(engine)
    if SCHEMA_MIGRATIONS_TABLE not in inspector.get_table_names():
        return None
    with engine.connect() as conn:
        value = conn.execute(text(f"SELECT MAX(version) FROM {SCHEMA_MIGRATIONS_TABLE}")).scalar()
    if value is None:
        return None
    return int(value)


def inspect_schema(engine: Engine, *, migrations: Sequence[Migration] | None = None) -> SchemaStatus:
    _load_models()
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    app_tables = [table.name for table in Base.metadata.sorted_tables if table.name != SCHEMA_MIGRATIONS_TABLE]
    empty = not any(name in existing for name in app_tables)
    drift: list[str] = []
    if not empty:
        for table in Base.metadata.sorted_tables:
            if table.name == SCHEMA_MIGRATIONS_TABLE:
                continue
            if table.name not in existing:
                drift.append(f"missing table {table.name}")
                continue
            columns = {item["name"] for item in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name not in columns:
                    drift.append(f"missing column {table.name}.{column.name}")
    return SchemaStatus(
        current=current_schema_version(engine),
        head=head_schema_version(migrations),
        empty=empty,
        drift=tuple(drift),
    )


def apply_migrations(
    engine: Engine,
    *,
    migrations: Sequence[Migration] | None = None,
    target: int | None = None,
) -> MigrationResult:
    """Apply pending migrations in order. Fail-stop; do not stamp a failed version."""
    chain = _validate_chain(migrations if migrations is not None else MIGRATIONS)
    _ensure_registry_table(engine)
    from_version = current_schema_version(engine) or 0
    target_version = target if target is not None else chain[-1].version
    applied: list[str] = []
    current = from_version
    for item in chain:
        if item.version > target_version:
            break
        try:
            with engine.begin() as conn:
                if item.version in _applied_versions(conn):
                    current = max(current, item.version)
                    continue
                item.upgrade(conn)
                _stamp(conn, item)
        except IntegrityError:
            latest = current_schema_version(engine) or 0
            if latest >= item.version:
                current = latest
                continue
            raise SchemaMigrationError(
                f"migration {item.version:03d}_{item.name} failed; later versions were not applied"
            ) from None
        except Exception as exc:
            raise SchemaMigrationError(
                f"migration {item.version:03d}_{item.name} failed; later versions were not applied"
            ) from exc
        applied.append(f"{item.version:03d}_{item.name}")
        current = item.version
    return MigrationResult(from_version=from_version, to_version=current, applied=applied)


def ensure_schema(engine: Engine, *, auto_migrate: bool) -> MigrationResult | SchemaStatus:
    """Initialize empty databases. Upgrade only when auto_migrate is on.

    Startup in production should pass auto_migrate=False so existing databases
    are version-checked, not silently altered.
    """
    status = inspect_schema(engine)
    if status.empty:
        return apply_migrations(engine)
    if status.current is not None and status.current > status.head:
        raise SchemaVersionError(
            f"database schema version {status.current} is newer than this build (head {status.head})"
        )
    needs_upgrade = status.current != status.head or bool(status.drift)
    if not needs_upgrade:
        return status
    if status.current == status.head and status.drift:
        raise SchemaVersionError(
            "schema version matches this build, but the database is missing ORM tables or columns: "
            + "; ".join(status.drift)
            + ". Add a numbered migration instead of relying on create_all."
        )
    if not auto_migrate:
        raise SchemaVersionError(_upgrade_required_message(status))
    return apply_migrations(engine)


def backup_sqlite_database(database_url: str, *, suffix: str | None = None) -> Path | None:
    path = sqlite_file_from_url(database_url)
    if path is None or not path.exists() or path.stat().st_size == 0:
        return None
    stamp = suffix or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.stem}.pre-migrate-{stamp}{path.suffix}")
    source = sqlite3.connect(str(path))
    try:
        destination = sqlite3.connect(str(backup))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    return backup


def sqlite_file_from_url(database_url: str) -> Path | None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return None
    database = url.database
    if not database or database == ":memory:":
        return None
    return Path(database)


def _upgrade_required_message(status: SchemaStatus) -> str:
    current = "unstamped" if status.current is None else str(status.current)
    drift = f" Drift: {'; '.join(status.drift)}." if status.drift else ""
    return (
        f"database schema version is {current}, code requires {status.head}.{drift} "
        "This process will not alter tables at startup. Backup the SQLite file, then run: "
        "PYTHONPATH=backend python -m app.core.migrations"
    )


def _validate_chain(chain: Sequence[Migration]) -> tuple[Migration, ...]:
    items = tuple(chain)
    if not items:
        raise SchemaVersionError("no schema migrations are registered")
    versions = [item.version for item in items]
    if any(version < 1 for version in versions):
        raise SchemaVersionError("migration versions must be >= 1")
    if len(set(versions)) != len(versions):
        raise SchemaVersionError("duplicate migration versions")
    expected = list(range(1, len(versions) + 1))
    if versions != expected:
        raise SchemaVersionError("migration versions must be unique, start at 1, and increase without gaps")
    return items


def _applied_versions(conn: Connection) -> set[int]:
    inspector = inspect(conn)
    inspector.clear_cache()
    if SCHEMA_MIGRATIONS_TABLE not in inspector.get_table_names():
        return set()
    rows = conn.execute(text(f"SELECT version FROM {SCHEMA_MIGRATIONS_TABLE}")).scalars()
    return {int(value) for value in rows}


def _quote(conn: Connection, name: str) -> str:
    return conn.dialect.identifier_preparer.quote(name)


def _ensure_registry_table(engine: Engine) -> None:
    SchemaMigration.__table__.create(bind=engine, checkfirst=True)


def _stamp(conn: Connection, item: Migration) -> None:
    conn.execute(
        text(
            f"INSERT INTO {SCHEMA_MIGRATIONS_TABLE} (version, name, applied_at) "
            "VALUES (:version, :name, :applied_at)"
        ),
        {
            "version": item.version,
            "name": item.name,
            "applied_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _add_missing_orm_columns(conn: Connection) -> None:
    inspector = inspect(conn)
    inspector.clear_cache()
    existing_tables = set(inspector.get_table_names())
    dialect = conn.dialect
    for table in Base.metadata.sorted_tables:
        if table.name == SCHEMA_MIGRATIONS_TABLE or table.name not in existing_tables:
            continue
        existing_columns = {item["name"] for item in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing_columns:
                continue
            type_sql = column.type.compile(dialect=dialect)
            conn.execute(
                text(
                    f"ALTER TABLE {_quote(conn, table.name)} "
                    f"ADD COLUMN {_quote(conn, column.name)} {type_sql}"
                )
            )


def _drop_legacy_columns(conn: Connection) -> None:
    inspector = inspect(conn)
    inspector.clear_cache()
    existing_tables = set(inspector.get_table_names())
    for table_name, columns in _LEGACY_DROPS.items():
        if table_name not in existing_tables:
            continue
        existing_columns = {item["name"] for item in inspector.get_columns(table_name)}
        for column_name in columns:
            if column_name not in existing_columns:
                continue
            conn.execute(
                text(
                    f"ALTER TABLE {_quote(conn, table_name)} "
                    f"DROP COLUMN {_quote(conn, column_name)}"
                )
            )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PPT Agent schema migrations (SQLite default).")
    parser.add_argument("--check", action="store_true", help="exit 1 if the database is not at HEAD")
    parser.add_argument("--no-backup", action="store_true", help="skip SQLite file backup before upgrade")
    args = parser.parse_args(list(argv) if argv is not None else None)

    from app.core.config import get_settings
    from app.core.db import get_engine

    settings = get_settings()
    engine = get_engine()
    status = inspect_schema(engine)
    if args.check:
        if status.at_head:
            print(f"schema {status.current} == head {status.head}")
            return 0
        print(_upgrade_required_message(status), file=sys.stderr)
        return 1
    if not args.no_backup:
        backup = backup_sqlite_database(settings.database_url)
        if backup is not None:
            print(f"sqlite backup: {backup}")
    result = apply_migrations(engine)
    print(f"schema {result.from_version} -> {result.to_version}")
    if result.applied:
        print("applied: " + ", ".join(result.applied))
    else:
        print("applied: none")
    status = inspect_schema(engine)
    if not status.at_head:
        print(_upgrade_required_message(status), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
