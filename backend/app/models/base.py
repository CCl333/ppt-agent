from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import DeclarativeBase


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def is_cache_fresh(expires_at: datetime | None, now: datetime | None = None) -> bool:
    if expires_at is None:
        return True
    current = ensure_utc(now) or now_utc()
    expiry = ensure_utc(expires_at)
    return expiry is not None and expiry > current


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass

