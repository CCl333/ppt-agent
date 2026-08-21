from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.schemas.api import (
    ModelBindingsPutRequest,
    ModelCatalogRequest,
    ModelProviderCreateRequest,
    ModelProviderPatchRequest,
    ReaderSettingsPutRequest,
    SearchSettingsPutRequest,
)
from app.services.model_gateway import invalidate_model_cache, list_remote_models, test_provider_connection
from app.services.model_settings import (
    create_provider,
    delete_provider,
    get_provider,
    list_bindings,
    list_providers,
    update_provider,
    upsert_bindings,
)
from app.services.reader_settings import serialize_reader_settings, update_reader_settings
from app.services.search_settings import serialize_search_settings, update_search_settings

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/models")
def get_models(db: Session = Depends(get_db)) -> dict:
    return {"items": list_providers(db)}


@router.post("/models")
def post_model(payload: ModelProviderCreateRequest, db: Session = Depends(get_db)) -> dict:
    item = create_provider(db, payload.model_dump())
    db.commit()
    invalidate_model_cache()
    return item


@router.patch("/models/{provider_id}")
def patch_model(provider_id: str, payload: ModelProviderPatchRequest, db: Session = Depends(get_db)) -> dict:
    item = update_provider(db, provider_id, payload.model_dump(exclude_unset=True))
    db.commit()
    invalidate_model_cache()
    return item


@router.delete("/models/{provider_id}")
def remove_model(provider_id: str, db: Session = Depends(get_db)) -> dict:
    delete_provider(db, provider_id)
    db.commit()
    invalidate_model_cache()
    return {"status": "deleted", "provider_id": provider_id}


@router.post("/models/{provider_id}/test")
def test_model(provider_id: str, db: Session = Depends(get_db)) -> dict:
    provider = get_provider(db, provider_id)
    try:
        return test_provider_connection(provider)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "模型连通性测试失败") from exc


@router.post("/models:catalog")
def catalog_models(payload: ModelCatalogRequest, db: Session = Depends(get_db)) -> dict:
    base_url = (payload.base_url or "").strip()
    api_key = (payload.api_key or "").strip()
    api_path = payload.api_path
    if payload.provider_id:
        provider = get_provider(db, payload.provider_id)
        base_url = base_url or provider.base_url
        api_key = api_key or provider.api_key
        if not (payload.api_path or "").strip():
            api_path = provider.api_path
    if not base_url or not api_key:
        raise HTTPException(status_code=400, detail="拉取模型列表需要 Base URL 和 API Key")
    try:
        items = list_remote_models(
            base_url=base_url,
            api_key=api_key,
            api_path=api_path,
            timeout_seconds=payload.timeout_seconds,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "拉取模型列表失败") from exc
    return {"items": items}


@router.get("/model-bindings")
def get_model_bindings(db: Session = Depends(get_db)) -> dict:
    return list_bindings(db)


@router.put("/model-bindings")
def put_model_bindings(payload: ModelBindingsPutRequest, db: Session = Depends(get_db)) -> dict:
    data = payload.model_dump(exclude_unset=True)
    if payload.expert is not None:
        data["expert"] = payload.expert.model_dump(exclude_unset=True)
    result = upsert_bindings(db, data)
    db.commit()
    invalidate_model_cache()
    return result


@router.get("/search")
def get_search_settings(db: Session = Depends(get_db)) -> dict:
    return serialize_search_settings(db)


@router.put("/search")
def put_search_settings(payload: SearchSettingsPutRequest, db: Session = Depends(get_db)) -> dict:
    result = update_search_settings(db, payload.model_dump(exclude_unset=True))
    db.commit()
    return result


@router.get("/reader")
def get_reader_settings(db: Session = Depends(get_db)) -> dict:
    return serialize_reader_settings(db)


@router.put("/reader")
def put_reader_settings(payload: ReaderSettingsPutRequest, db: Session = Depends(get_db)) -> dict:
    result = update_reader_settings(db, payload.model_dump(exclude_unset=True))
    db.commit()
    return result
