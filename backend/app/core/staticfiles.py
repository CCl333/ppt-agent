from __future__ import annotations

from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


class AttachmentStaticFiles(StaticFiles):
    """Serve uploaded assets as downloadable files, never as executable HTML."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Content-Disposition"] = "attachment"
        response.headers["X-Content-Type-Options"] = "nosniff"
        content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type in {"text/html", "application/xhtml+xml", "image/svg+xml"}:
            response.headers["content-type"] = "application/octet-stream"
        return response
