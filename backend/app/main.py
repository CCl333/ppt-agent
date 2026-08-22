from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from app.api.router import api_router
from app.core.config import get_settings
from app.core.db import init_db
from app.core.logging import configure_logging
from app.core.staticfiles import AttachmentStaticFiles
from app.models import entities  # noqa: F401
from app.services.tasks import start_scheduler, stop_scheduler


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    configure_logging(settings.app_debug)
    settings.file_storage_root.mkdir(parents=True, exist_ok=True)
    settings.upload_path.mkdir(parents=True, exist_ok=True)
    settings.background_path.mkdir(parents=True, exist_ok=True)
    settings.export_path.mkdir(parents=True, exist_ok=True)
    settings.quality_eval_path.mkdir(parents=True, exist_ok=True)
    init_db()
    start_scheduler()
    yield
    stop_scheduler()


def create_app() -> FastAPI:
    settings = get_settings()
    settings.file_storage_root.mkdir(parents=True, exist_ok=True)
    settings.upload_path.mkdir(parents=True, exist_ok=True)
    settings.background_path.mkdir(parents=True, exist_ok=True)
    settings.export_path.mkdir(parents=True, exist_ok=True)
    settings.quality_eval_path.mkdir(parents=True, exist_ok=True)
    init_db()
    app = FastAPI(
        title="PPT Agent Backend",
        version="0.1.0",
        default_response_class=ORJSONResponse,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router, prefix=settings.api_prefix)
    app.mount("/storage/uploads", AttachmentStaticFiles(directory=settings.upload_path), name="storage-uploads")
    app.mount(
        "/storage/backgrounds",
        AttachmentStaticFiles(directory=settings.background_path),
        name="storage-backgrounds",
    )
    app.mount("/storage/exports", AttachmentStaticFiles(directory=settings.export_path), name="storage-exports")

    @app.get("/healthz")
    def healthz() -> dict:
        from app.services.runtime import runtime_payload

        return runtime_payload()

    return app


app = create_app()
