from __future__ import annotations

from fastapi import APIRouter

from app.services.runtime import runtime_payload

router = APIRouter(tags=["runtime"])


@router.get("/runtime")
def get_runtime() -> dict:
    return runtime_payload()
