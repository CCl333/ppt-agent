from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from app.services.svg import CANVAS_HEIGHT, CANVAS_WIDTH


def render_svg_screenshot(svg_markup: str, output_path: Path) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return _skipped("svg", "playwright_unavailable", output_path)
    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<style>html,body{margin:0;background:#fff;overflow:hidden}</style>"
        + (svg_markup or "")
    )
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": CANVAS_WIDTH, "height": CANVAS_HEIGHT})
                page.set_content(html, wait_until="load")
                page.evaluate("async () => { if (document.fonts) await document.fonts.ready; }")
                page.screenshot(path=str(output_path), type="png")
            finally:
                browser.close()
    except Exception as exc:
        return {
            "status": "skipped",
            "kind": "svg",
            "reason": "browser_render_failed",
            "detail": str(exc),
            "path": None,
        }
    return {"status": "ok", "kind": "svg", "reason": None, "path": str(output_path)}


def render_pptx_screenshot(pptx_path: Path, output_path: Path) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    converter = shutil.which("soffice") or shutil.which("libreoffice")
    if converter is None:
        return _skipped("pptx", "libreoffice_unavailable", output_path)
    workdir = output_path.parent / "pptx-render"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [converter, "--headless", "--convert-to", "png", "--outdir", str(workdir), str(pptx_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "status": "skipped",
            "kind": "pptx",
            "reason": "pptx_render_failed",
            "detail": str(exc),
            "path": None,
        }
    pngs = sorted(workdir.glob("*.png"))
    if result.returncode != 0 or not pngs:
        return {
            "status": "skipped",
            "kind": "pptx",
            "reason": "pptx_render_failed",
            "detail": (result.stderr or result.stdout or "")[:300],
            "path": None,
        }
    shutil.copyfile(pngs[0], output_path)
    return {"status": "ok", "kind": "pptx", "reason": None, "path": str(output_path)}


def _skipped(kind: str, reason: str, output_path: Path) -> dict[str, Any]:
    return {
        "status": "skipped",
        "kind": kind,
        "reason": reason,
        "detail": None,
        "path": None,
        "expected_path": str(output_path),
    }
