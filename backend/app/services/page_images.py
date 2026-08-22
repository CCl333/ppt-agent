from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import httpx

from app.core.config import get_settings
from app.services.search_quality import assert_public_http_url, is_http_url
from app.services.svg import SVG_NS, XLINK_NS, _ensure_svg_xmlns, _local_tag, _parse_svg, _serialize_svg

MAX_PAGE_IMAGES = 4
MAX_IMAGE_BYTES = 4 * 1024 * 1024
_IMAGE_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp|gif)(?:\?|$)", re.IGNORECASE)
_MIME_BY_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
    (b"RIFF", "image/webp", ".webp"),
)
_IMAGE_URL_KEYS = ("image_url", "imageUrl", "thumbnailUrl", "thumbnail_url", "og_image", "cover_image")


def prompt_catalog(catalog: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for item in catalog or []:
        if not isinstance(item, dict):
            continue
        image_id = str(item.get("image_id") or "").strip()
        if not image_id:
            continue
        items.append(
            {
                "image_id": image_id,
                "caption": str(item.get("caption") or "").strip(),
                "source_title": str(item.get("source_title") or "").strip(),
                "source_url": str(item.get("source_url") or "").strip(),
                "license_status": str(item.get("license_status") or "unknown").strip() or "unknown",
            }
        )
    return items


def extract_image_candidates(*sources: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    seen: set[str] = set()
    candidates: list[dict[str, str]] = []
    for source in sources:
        for item in source or []:
            if not isinstance(item, dict):
                continue
            for url in _candidate_urls(item):
                if url in seen:
                    continue
                seen.add(url)
                candidates.append(
                    {
                        "url": url,
                        "caption": str(item.get("title") or item.get("caption") or "").strip(),
                        "source_title": str(item.get("title") or "").strip(),
                        "source_url": str(item.get("url") or url).strip(),
                        "license_status": str(item.get("license_status") or "unknown").strip() or "unknown",
                    }
                )
    return candidates


def materialize_page_images(
    *,
    project_id: str,
    page_id: str,
    search_results: list[dict[str, Any]] | None = None,
    extra_results: list[dict[str, Any]] | None = None,
    fetch: Callable[[str], tuple[str, bytes]] | None = None,
) -> list[dict[str, Any]]:
    downloader = fetch or fetch_image
    catalog: list[dict[str, Any]] = []
    for candidate in extract_image_candidates(search_results, extra_results):
        if len(catalog) >= MAX_PAGE_IMAGES:
            break
        try:
            mime, payload = downloader(candidate["url"])
        except Exception:
            continue
        image_id = f"IMG-{len(catalog) + 1}"
        suffix = _suffix_for_mime(mime)
        path = _image_dir(project_id, page_id) / f"{image_id}{suffix}"
        path.write_bytes(payload)
        catalog.append(
            {
                "image_id": image_id,
                "caption": candidate["caption"] or image_id,
                "source_title": candidate["source_title"],
                "source_url": candidate["source_url"],
                "license_status": str(candidate.get("license_status") or "unknown").strip() or "unknown",
                "mime": mime,
                "storage_path": str(path),
            }
        )
    return catalog


def public_catalog(catalog: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for item in catalog or []:
        if not isinstance(item, dict) or not str(item.get("image_id") or "").strip():
            continue
        items.append(
            {
                "image_id": str(item.get("image_id") or "").strip(),
                "caption": str(item.get("caption") or "").strip(),
                "source_title": str(item.get("source_title") or "").strip(),
                "source_url": str(item.get("source_url") or "").strip(),
                "license_status": str(item.get("license_status") or "unknown").strip() or "unknown",
                "available": "true" if Path(str(item.get("storage_path") or "")).is_file() else "false",
            }
        )
    return items


def collect_image_ids(svg_markup: str) -> list[str]:
    root = _parse_svg(_ensure_svg_xmlns(svg_markup))
    found: list[str] = []
    for elem in root.iter():
        if _local_tag(elem) != "image":
            continue
        if (elem.get("data-chrome") or "").strip():
            continue
        image_id = (elem.get("data-image-id") or "").strip()
        if image_id:
            found.append(image_id)
    return found


def resolve_image_refs(svg_markup: str, catalog: list[dict[str, Any]] | None) -> str:
    by_id = {
        str(item.get("image_id") or "").strip(): item
        for item in catalog or []
        if isinstance(item, dict) and str(item.get("image_id") or "").strip()
    }
    root = _parse_svg(_ensure_svg_xmlns(svg_markup))
    for elem in root.iter():
        if _local_tag(elem) != "image":
            continue
        if (elem.get("data-chrome") or "").strip():
            continue
        image_id = (elem.get("data-image-id") or "").strip()
        if not image_id:
            href = _image_href(elem)
            if href.startswith("data:image/"):
                continue
            raise RuntimeError("内容配图必须使用 data-image-id 引用检索目录，禁止直接写网络地址或文件路径")
        item = by_id.get(image_id)
        if item is None:
            raise RuntimeError(f"SVG 引用了不存在的配图 {image_id}")
        href = _data_uri_from_item(item)
        elem.set("href", href)
        elem.set(f"{{{XLINK_NS}}}href", href)
        elem.set("preserveAspectRatio", elem.get("preserveAspectRatio") or "xMidYMid slice")
    return _serialize_svg(root)


def assert_svg_uses_images(
    svg_markup: str,
    catalog: list[dict[str, Any]] | None,
    slots: list[dict[str, Any]] | None,
    *,
    stage: str = "design",
) -> None:
    catalog_ids = {str(item.get("image_id") or "").strip() for item in catalog or []}
    catalog_ids.discard("")
    used = collect_image_ids(svg_markup)
    unknown = [image_id for image_id in used if image_id not in catalog_ids]
    if unknown:
        raise RuntimeError(f"SVG 引用了目录外的配图: {', '.join(unknown)}")
    if stage == "draft":
        return
    required = [str(item.get("image_id") or "").strip() for item in slots or [] if str(item.get("image_id") or "").strip()]
    missing = [image_id for image_id in required if image_id not in used]
    if missing:
        raise RuntimeError(f"策划指定的配图未出现在 SVG 中: {', '.join(missing)}")


def fetch_image(url: str, *, timeout: float = 15.0) -> tuple[str, bytes]:
    current = assert_public_http_url(url, reason="配图")
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            for _ in range(3):
                current = assert_public_http_url(current, reason="配图")
                with client.stream("GET", current) as response:
                    status = response.status_code
                    if 300 <= status < 400:
                        location = response.headers.get("location")
                        if not location:
                            raise RuntimeError(f"配图重定向缺少 Location: {current}")
                        current = urljoin(current, location)
                        continue
                    response.raise_for_status()
                    buf = bytearray()
                    for chunk in response.iter_bytes():
                        buf.extend(chunk)
                        if len(buf) > MAX_IMAGE_BYTES:
                            raise RuntimeError("配图超过 4MB 上限")
                    return sniff_image(bytes(buf), response.headers.get("content-type") or "")
    except Exception as exc:
        if isinstance(exc, RuntimeError) and str(exc).startswith("配图"):
            raise
        raise RuntimeError(f"配图下载失败: {url}: {exc}") from exc
    raise RuntimeError(f"配图下载失败: {url}")


def sniff_image(payload: bytes, content_type: str = "") -> tuple[str, bytes]:
    if not payload:
        raise RuntimeError("配图为空")
    mime = ""
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        for magic, detected, _suffix in _MIME_BY_MAGIC:
            if magic != b"RIFF" and payload.startswith(magic):
                mime = detected
                break
    if not mime:
        lowered = content_type.split(";", 1)[0].strip().lower()
        if lowered in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            mime = lowered
    if not mime:
        raise RuntimeError("配图不是 png/jpeg/webp/gif")
    return mime, payload


def _candidate_urls(item: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for key in _IMAGE_URL_KEYS:
        value = str(item.get(key) or "").strip()
        if is_http_url(value):
            found.append(value)
    extra = item.get("extra_images") or item.get("images") or []
    if isinstance(extra, list):
        for raw in extra:
            value = str(raw or "").strip()
            if is_http_url(value):
                found.append(value)
    url = str(item.get("url") or "").strip()
    if is_http_url(url) and _IMAGE_EXT_RE.search(urlparse(url).path):
        found.append(url)
    return found


def _data_uri_from_item(item: dict[str, Any]) -> str:
    path = Path(str(item.get("storage_path") or ""))
    if not path.is_file():
        raise RuntimeError(f"配图文件不存在: {item.get('image_id')}")
    payload = path.read_bytes()
    if not payload:
        raise RuntimeError(f"配图文件为空: {item.get('image_id')}")
    mime = str(item.get("mime") or sniff_image(payload)[0])
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


def _image_href(elem: ET.Element) -> str:
    return (elem.get("href") or elem.get(f"{{{XLINK_NS}}}href") or "").strip()


def _image_dir(project_id: str, page_id: str) -> Path:
    path = get_settings().file_storage_root / "page-images" / project_id / page_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _suffix_for_mime(mime: str) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(mime, ".bin")
