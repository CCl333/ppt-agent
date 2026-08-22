from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from typing import Any

from app.services.page_scene import SCHEMA_VERSION as SCENE_SCHEMA_VERSION
from app.services.quality_report import CONTRACT_VERSION

PROMPT_CONTRACT_VERSION = "prompt.v1"
SVG_CONTRACT_VERSION = "svg-profile.v1"
EXPORTER_VERSION = "exporter.v1"
QUALITY_EVAL_VERSION = "quality-eval.v1"
STARTED_AT = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
_GIT_SHA: str | None | bool = False


def app_version() -> str:
    env_version = str(os.environ.get("APP_VERSION") or "").strip()
    if env_version:
        return env_version
    git_sha = _git_sha()
    return git_sha or "dev"


def build_fingerprint() -> dict[str, Any]:
    from app.core.migrations import head_schema_version

    return {
        "app": app_version(),
        "prompt_contract_version": PROMPT_CONTRACT_VERSION,
        "svg_contract_version": SVG_CONTRACT_VERSION,
        "scene_schema_version": SCENE_SCHEMA_VERSION,
        "schema_head": head_schema_version(),
        "exporter_version": EXPORTER_VERSION,
        "quality_contract_version": CONTRACT_VERSION,
        "quality_eval_version": QUALITY_EVAL_VERSION,
    }


def runtime_payload() -> dict[str, Any]:
    fingerprint = build_fingerprint()
    schema_version, schema_error = _schema_status()
    status = "ok"
    if schema_error or schema_version != fingerprint["schema_head"]:
        status = "degraded"
    return {
        "status": status,
        "app_version": fingerprint["app"],
        "prompt_contract_version": fingerprint["prompt_contract_version"],
        "svg_contract_version": fingerprint["svg_contract_version"],
        "scene_schema_version": fingerprint["scene_schema_version"],
        "schema_head": fingerprint["schema_head"],
        "schema_version": schema_version,
        "schema_error": schema_error,
        "exporter_version": fingerprint["exporter_version"],
        "quality_contract_version": fingerprint["quality_contract_version"],
        "quality_eval_version": fingerprint["quality_eval_version"],
        "started_at": STARTED_AT,
    }


def _schema_status() -> tuple[int | None, str | None]:
    try:
        from app.core.db import get_engine
        from app.core.migrations import current_schema_version

        return current_schema_version(get_engine()), None
    except Exception as exc:
        return None, str(exc)


def _git_sha() -> str | None:
    global _GIT_SHA
    if _GIT_SHA is not False:
        return _GIT_SHA if isinstance(_GIT_SHA, str) else None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        _GIT_SHA = None
        return None
    sha = (result.stdout or "").strip()
    _GIT_SHA = sha or None
    return _GIT_SHA
