from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_session_factory
from app.models.base import new_id, now_utc
from app.models.entities import ModelBinding, ModelProvider

logger = logging.getLogger("ppt_agent.models")

MODEL_ROLES = ("context", "svg", "search")
ROLE_LABELS = {
    "context": "文本模型",
    "svg": "SVG 模型",
    "search": "搜索模型",
}
API_PATH_CHAT = "/chat/completions"
API_PATH_RESPONSES = "/responses"
API_PATH_ANTHROPIC = "/messages"
_CHAT_PATH_ALIASES = frozenset({API_PATH_CHAT, "chat/completions", "/v1/chat/completions", "v1/chat/completions"})
_RESPONSES_PATH_ALIASES = frozenset({API_PATH_RESPONSES, "responses", "/v1/responses", "v1/responses"})
_ANTHROPIC_PATH_ALIASES = frozenset({API_PATH_ANTHROPIC, "messages", "/v1/messages", "v1/messages"})


def canonicalize_api_path(raw_value: str | None) -> str:
    value = str(raw_value or "").strip() or API_PATH_CHAT
    if value in _CHAT_PATH_ALIASES:
        return API_PATH_CHAT
    if value in _RESPONSES_PATH_ALIASES:
        return API_PATH_RESPONSES
    if value in _ANTHROPIC_PATH_ALIASES:
        return API_PATH_ANTHROPIC
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"不支持的 API 兼容格式: {value}")


@dataclass(frozen=True)
class ProviderSnapshot:
    provider_id: str
    name: str
    base_url: str
    api_key: str
    model: str
    api_path: str
    timeout_seconds: int
    updated_at: str


def mask_api_key(api_key: str) -> str:
    if not api_key:
        return ""
    if len(api_key) <= 6:
        return "***"
    return f"{api_key[:3]}***{api_key[-3:]}"


def serialize_provider(provider: ModelProvider, *, include_key: bool = False) -> dict[str, Any]:
    payload = {
        "provider_id": provider.provider_id,
        "name": provider.name,
        "base_url": provider.base_url,
        "model": provider.model,
        "api_path": provider.api_path,
        "timeout_seconds": provider.timeout_seconds,
        "api_key_masked": mask_api_key(provider.api_key),
        "created_at": provider.created_at.isoformat(),
        "updated_at": provider.updated_at.isoformat(),
    }
    if include_key:
        payload["api_key"] = provider.api_key
    return payload


def seed_models_from_env(session: Session | None = None) -> None:
    owns_session = session is None
    if owns_session:
        session = get_session_factory()()
    assert session is not None
    try:
        from app.services.reader_settings import ensure_reader_settings
        from app.services.search_settings import ensure_search_settings

        ensure_search_settings(session)
        ensure_reader_settings(session)
        if session.scalar(select(ModelProvider.provider_id).limit(1)):
            if owns_session:
                session.commit()
            return
        settings = get_settings()
        seeds = [
            (
                "context",
                settings.context_llm_base_url,
                settings.context_llm_api_key.get_secret_value() if settings.context_llm_api_key else None,
                settings.context_llm_model,
                settings.context_llm_path,
                settings.context_llm_timeout_seconds,
                "文本模型（来自 .env）",
            ),
            (
                "svg",
                settings.svg_llm_base_url,
                settings.svg_llm_api_key.get_secret_value() if settings.svg_llm_api_key else None,
                settings.svg_llm_model,
                settings.svg_llm_path,
                settings.svg_llm_timeout_seconds,
                "SVG 模型（来自 .env）",
            ),
        ]
        created_by_fingerprint: dict[tuple[str, str, str, str], ModelProvider] = {}
        for role, base_url, api_key, model, api_path, timeout_seconds, name in seeds:
            if not (base_url and api_key and model):
                continue
            fingerprint = (base_url.rstrip("/"), api_key, model, api_path)
            provider = created_by_fingerprint.get(fingerprint)
            if provider is None:
                try:
                    normalized_path = canonicalize_api_path(api_path)
                except HTTPException:
                    normalized_path = API_PATH_CHAT
                provider = ModelProvider(
                    provider_id=new_id(),
                    name=name,
                    base_url=base_url.rstrip("/"),
                    api_key=api_key,
                    model=model,
                    api_path=normalized_path,
                    timeout_seconds=timeout_seconds or 120,
                )
                session.add(provider)
                session.flush()
                created_by_fingerprint[fingerprint] = provider
            session.merge(ModelBinding(role=role, provider_id=provider.provider_id))
        if owns_session:
            session.commit()
    except Exception:
        if owns_session:
            session.rollback()
        raise
    finally:
        if owns_session:
            session.close()


def list_providers(session: Session) -> list[dict[str, Any]]:
    items = list(session.scalars(select(ModelProvider).order_by(ModelProvider.created_at.asc())))
    return [serialize_provider(item) for item in items]


def create_provider(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    provider = ModelProvider(
        provider_id=new_id(),
        name=str(payload["name"]).strip(),
        base_url=str(payload["base_url"]).strip().rstrip("/"),
        api_key=str(payload["api_key"]).strip(),
        model=str(payload["model"]).strip(),
        api_path=canonicalize_api_path(payload.get("api_path")),
        timeout_seconds=int(payload.get("timeout_seconds") or 120),
    )
    if not provider.name or not provider.base_url or not provider.api_key or not provider.model:
        raise HTTPException(status_code=422, detail="名称、base_url、api_key、model 均为必填")
    session.add(provider)
    session.flush()
    return serialize_provider(provider)


def update_provider(session: Session, provider_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    provider = _require_provider(session, provider_id)
    if payload.get("name") is not None:
        provider.name = str(payload["name"]).strip()
    if payload.get("base_url") is not None:
        provider.base_url = str(payload["base_url"]).strip().rstrip("/")
    if payload.get("model") is not None:
        provider.model = str(payload["model"]).strip()
    if payload.get("api_path") is not None:
        provider.api_path = canonicalize_api_path(payload["api_path"])
    if payload.get("timeout_seconds") is not None:
        provider.timeout_seconds = int(payload["timeout_seconds"])
    api_key = payload.get("api_key")
    if api_key:
        provider.api_key = str(api_key).strip()
    provider.updated_at = now_utc()
    session.flush()
    return serialize_provider(provider)


def delete_provider(session: Session, provider_id: str) -> None:
    provider = _require_provider(session, provider_id)
    bindings = list(session.scalars(select(ModelBinding).where(ModelBinding.provider_id == provider_id)))
    for binding in bindings:
        session.delete(binding)
    session.delete(provider)
    session.flush()


def list_bindings(session: Session) -> dict[str, Any]:
    from app.services.reader_settings import ensure_reader_settings
    from app.services.search_settings import ensure_search_settings

    ensure_search_settings(session)
    ensure_reader_settings(session)
    providers = {item.provider_id: item for item in session.scalars(select(ModelProvider))}
    bindings = {item.role: item for item in session.scalars(select(ModelBinding))}
    items = []
    for role in MODEL_ROLES:
        binding = bindings.get(role)
        provider = providers.get(binding.provider_id) if binding else None
        items.append(
            {
                "role": role,
                "label": ROLE_LABELS[role],
                "provider_id": binding.provider_id if binding else None,
                "provider": serialize_provider(provider) if provider else None,
            }
        )
    return {
        "items": items,
        "needs_setup": _needs_setup(session, items),
    }


def upsert_bindings(session: Session, payload: dict[str, str | None]) -> dict[str, Any]:
    for role, provider_id in payload.items():
        if role not in MODEL_ROLES:
            raise HTTPException(status_code=422, detail=f"未知角色: {role}")
        if not provider_id:
            binding = session.get(ModelBinding, role)
            if binding:
                session.delete(binding)
            continue
        _require_provider(session, provider_id)
        session.merge(ModelBinding(role=role, provider_id=provider_id))
    session.flush()
    return list_bindings(session)


def snapshot_bound_provider(role: str) -> ProviderSnapshot | None:
    if role not in MODEL_ROLES:
        return None
    session = get_session_factory()()
    try:
        binding = session.get(ModelBinding, role)
        if binding is None:
            return None
        provider = session.get(ModelProvider, binding.provider_id)
        if provider is None:
            return None
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
    finally:
        session.close()


def get_provider(session: Session, provider_id: str) -> ModelProvider:
    return _require_provider(session, provider_id)


def _require_provider(session: Session, provider_id: str) -> ModelProvider:
    provider = session.get(ModelProvider, provider_id)
    if provider is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="模型不存在")
    return provider


def _needs_setup(session: Session, items: list[dict[str, Any]]) -> bool:
    by_role = {item["role"]: item["provider_id"] for item in items}
    if not by_role.get("context") or not by_role.get("svg"):
        return True
    from app.services.reader_settings import reader_is_ready
    from app.services.search_settings import search_is_ready

    return not search_is_ready(session) or not reader_is_ready(session)
