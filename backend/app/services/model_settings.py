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
from app.models.entities import ModelBinding, ModelProvider, ModelStageSettings

logger = logging.getLogger("ppt_agent.models")

MODEL_ROLES = ("search", "content", "draft", "design")
REQUIRED_STAGE_ROLES = ("content", "draft", "design")
ROLE_ALIASES = {
    "context": "content",
    "svg": "draft",
}
ROLE_LABELS = {
    "search": "资料检索",
    "content": "内容策划",
    "draft": "初稿布局",
    "design": "最终设计",
}
ROLE_HINTS = {
    "search": "大模型搜索与检索词生成；博查 / Tavily 模式下可留空",
    "content": "大纲、summary、内容策划、风格卡、路由决策",
    "draft": "策划稿 SVG",
    "design": "设计稿 SVG",
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


def canonicalize_role(role: str) -> str:
    value = str(role or "").strip()
    return ROLE_ALIASES.get(value, value)


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
            migrate_legacy_bindings(session)
            if owns_session:
                session.commit()
            return
        settings = get_settings()
        seeds = [
            (
                "content",
                settings.context_llm_base_url,
                settings.context_llm_api_key.get_secret_value() if settings.context_llm_api_key else None,
                settings.context_llm_model,
                settings.context_llm_path,
                settings.context_llm_timeout_seconds,
                "内容策划（来自 .env）",
            ),
            (
                "draft",
                settings.svg_llm_base_url,
                settings.svg_llm_api_key.get_secret_value() if settings.svg_llm_api_key else None,
                settings.svg_llm_model,
                settings.svg_llm_path,
                settings.svg_llm_timeout_seconds,
                "初稿布局（来自 .env）",
            ),
            (
                "design",
                settings.svg_llm_base_url,
                settings.svg_llm_api_key.get_secret_value() if settings.svg_llm_api_key else None,
                settings.svg_llm_model,
                settings.svg_llm_path,
                settings.svg_llm_timeout_seconds,
                "最终设计（来自 .env）",
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
    stage = session.get(ModelStageSettings, "default")
    if stage and isinstance(stage.expert_bindings_json, dict):
        stage.expert_bindings_json = {
            role: bound_id
            for role, bound_id in stage.expert_bindings_json.items()
            if bound_id != provider_id
        }
    session.delete(provider)
    session.flush()


def list_bindings(session: Session) -> dict[str, Any]:
    from app.services.reader_settings import ensure_reader_settings
    from app.services.search_settings import ensure_search_settings

    ensure_search_settings(session)
    ensure_reader_settings(session)
    migrate_legacy_bindings(session)
    stage = ensure_stage_settings(session)
    providers = {item.provider_id: item for item in session.scalars(select(ModelProvider))}
    bindings = {item.role: item for item in session.scalars(select(ModelBinding))}
    items = [_serialize_binding(role, bindings.get(role), providers) for role in MODEL_ROLES]
    expert_map = dict(stage.expert_bindings_json or {})
    expert_items = []
    for role in MODEL_ROLES:
        provider_id = str(expert_map.get(role) or "").strip() or None
        provider = providers.get(provider_id) if provider_id else None
        expert_items.append(
            {
                "role": role,
                "label": ROLE_LABELS[role],
                "hint": ROLE_HINTS[role],
                "provider_id": provider_id if provider else None,
                "provider": serialize_provider(provider) if provider else None,
            }
        )
    return {
        "items": items,
        "needs_setup": _needs_setup(session, items, stage=stage, expert_items=expert_items),
        "expert_enabled": bool(stage.expert_enabled),
        "expert_items": expert_items,
    }


def upsert_bindings(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    migrate_legacy_bindings(session)
    data = dict(payload)
    expert_enabled = data.pop("expert_enabled", None)
    expert = data.pop("expert", None)
    data = _canonicalize_binding_payload(data)
    for role, provider_id in data.items():
        if role not in MODEL_ROLES:
            raise HTTPException(status_code=422, detail=f"未知角色: {role}")
        if not provider_id:
            binding = session.get(ModelBinding, role)
            if binding:
                session.delete(binding)
            continue
        _require_provider(session, provider_id)
        session.merge(ModelBinding(role=role, provider_id=provider_id))
    if expert_enabled is not None or expert is not None:
        stage = ensure_stage_settings(session)
        if expert_enabled is not None:
            stage.expert_enabled = bool(expert_enabled)
        if expert is not None:
            if not isinstance(expert, dict):
                raise HTTPException(status_code=422, detail="专家档绑定必须是对象")
            current = dict(stage.expert_bindings_json or {})
            for raw_role, provider_id in _canonicalize_binding_payload(expert).items():
                if raw_role not in MODEL_ROLES:
                    raise HTTPException(status_code=422, detail=f"未知角色: {raw_role}")
                if not provider_id:
                    current.pop(raw_role, None)
                    continue
                _require_provider(session, str(provider_id))
                current[raw_role] = str(provider_id)
            stage.expert_bindings_json = current
        session.flush()
    session.flush()
    return list_bindings(session)


def snapshot_bound_provider(role: str) -> ProviderSnapshot | None:
    canonical = canonicalize_role(role)
    if canonical not in MODEL_ROLES:
        return None
    session = get_session_factory()()
    try:
        stage = session.get(ModelStageSettings, "default")
        provider_id = None
        if stage and stage.expert_enabled:
            provider_id = str((stage.expert_bindings_json or {}).get(canonical) or "").strip() or None
        else:
            binding = session.get(ModelBinding, canonical)
            if binding is None and canonical == "content":
                binding = session.get(ModelBinding, "context")
            elif binding is None and canonical in {"draft", "design"}:
                binding = session.get(ModelBinding, "svg")
            provider_id = binding.provider_id if binding else None
        if not provider_id:
            return None
        provider = session.get(ModelProvider, provider_id)
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


def expert_mode_enabled() -> bool:
    try:
        session = get_session_factory()()
    except Exception:
        return False
    try:
        stage = session.get(ModelStageSettings, "default")
        return bool(stage and stage.expert_enabled)
    except Exception:
        return False
    finally:
        session.close()


def get_provider(session: Session, provider_id: str) -> ModelProvider:
    return _require_provider(session, provider_id)


def _require_provider(session: Session, provider_id: str) -> ModelProvider:
    provider = session.get(ModelProvider, provider_id)
    if provider is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="模型不存在")
    return provider


def _needs_setup(
    session: Session,
    items: list[dict[str, Any]],
    *,
    stage: ModelStageSettings,
    expert_items: list[dict[str, Any]],
) -> bool:
    by_role = {item["role"]: item["provider_id"] for item in items}
    if any(not by_role.get(role) for role in REQUIRED_STAGE_ROLES):
        return True
    if stage.expert_enabled:
        expert_by_role = {item["role"]: item["provider_id"] for item in expert_items}
        if any(not expert_by_role.get(role) for role in REQUIRED_STAGE_ROLES):
            return True
    from app.services.reader_settings import reader_is_ready
    from app.services.search_settings import search_is_ready

    return not search_is_ready(session) or not reader_is_ready(session)


def ensure_stage_settings(session: Session) -> ModelStageSettings:
    row = session.get(ModelStageSettings, "default")
    if row is None:
        row = ModelStageSettings(id="default", expert_enabled=False, expert_bindings_json={})
        session.add(row)
        session.flush()
    return row


def migrate_legacy_bindings(session: Session) -> None:
    bindings = {item.role: item for item in session.scalars(select(ModelBinding))}
    copies: list[tuple[str, str]] = []
    if "content" not in bindings and "context" in bindings:
        copies.append(("content", bindings["context"].provider_id))
    if "draft" not in bindings and "svg" in bindings:
        copies.append(("draft", bindings["svg"].provider_id))
    if "design" not in bindings:
        source_id = None
        if "svg" in bindings:
            source_id = bindings["svg"].provider_id
        elif "draft" in bindings:
            source_id = bindings["draft"].provider_id
        elif any(role == "draft" for role, _provider_id in copies):
            source_id = next(provider_id for role, provider_id in copies if role == "draft")
        if source_id:
            copies.append(("design", source_id))
    if not copies:
        return
    for role, provider_id in copies:
        session.merge(ModelBinding(role=role, provider_id=provider_id))
    session.flush()


def _canonicalize_binding_payload(payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload)
    if "content" not in data and "context" in data:
        data["content"] = data.pop("context")
    else:
        data.pop("context", None)
    if "svg" in data:
        svg_value = data.pop("svg")
        data.setdefault("draft", svg_value)
        data.setdefault("design", svg_value)
    return data


def _serialize_binding(role: str, binding: ModelBinding | None, providers: dict[str, ModelProvider]) -> dict[str, Any]:
    provider = providers.get(binding.provider_id) if binding else None
    return {
        "role": role,
        "label": ROLE_LABELS[role],
        "hint": ROLE_HINTS[role],
        "provider_id": binding.provider_id if binding else None,
        "provider": serialize_provider(provider) if provider else None,
    }
