from __future__ import annotations

import ipaddress
import json
import re
import socket
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import httpx

_SCAFFOLD_RE = re.compile(
    r"(invoke\s+tool|<tool_call>|```html|web_search\s*\(|web_sear\b)",
    re.IGNORECASE,
)
_HEADING_RE = re.compile(r"^#{2,3}\s+(.+)$", re.MULTILINE)
_FENCE_RE = re.compile(r"^```[\w-]*\s*|\s*```$", re.MULTILINE)
_EMPTY_ANSWERS = {"", "没有检索结果", "无检索结果", "no search results"}

# 整理稿最少实质正文字符数。线上坏数据实测为 3（`[6]`）与 44（`{"query": "..."}`），
# 合格整理稿实测 2658，200 是一个不会误伤真实短稿的下限。
MIN_DIGEST_CHARS = 200

_MD_CITATION_RE = re.compile(r"\[{1,2}\d+\]{1,2}(\([^)]*\))?")
_URL_RE = re.compile(r"https?://\S+")
_MD_SYMBOL_RE = re.compile(r"[#*`>\-_|\[\]()~]+")


def substantive_length(text: str | None) -> int:
    """剥离引用标记、URL、Markdown 符号与空白后的实质正文长度。"""
    cleaned = _MD_CITATION_RE.sub(" ", str(text or ""))
    cleaned = _URL_RE.sub(" ", cleaned)
    cleaned = _MD_SYMBOL_RE.sub(" ", cleaned)
    return len(re.sub(r"\s+", "", cleaned))


def looks_like_json_literal(text: str | None) -> bool:
    stripped = str(text or "").strip()
    if not (stripped.startswith("{") and stripped.endswith("}")) and not (
        stripped.startswith("[") and stripped.endswith("]")
    ):
        return False
    try:
        json.loads(stripped)
    except ValueError:
        return False
    return True


def normalize_digest_answer(text: str | None) -> str:
    cleaned = _FENCE_RE.sub("", str(text or "")).strip()
    cleaned = re.sub(r"^[\s`#\-*>]+", "", cleaned)
    first_line = cleaned.splitlines()[0].strip() if cleaned else ""
    first_line = re.sub(r"[\s。.!！？?，,；;：:]+$", "", first_line)
    return first_line.lower()


def is_empty_digest(text: str | None) -> bool:
    return normalize_digest_answer(text) in _EMPTY_ANSWERS


def detect_degeneration(text: str | None) -> str | None:
    raw = str(text or "")
    if _SCAFFOLD_RE.search(raw):
        return "整理稿含工具调用/脚手架文本，不能当作检索结果"
    headings = [match.group(1).strip() for match in _HEADING_RE.finditer(raw)]
    counts: dict[str, int] = {}
    for heading in headings:
        counts[heading] = counts.get(heading, 0) + 1
        if counts[heading] >= 3:
            return f"整理稿出现复读循环（标题重复 3 次以上：{heading}）"
    return None


def assert_digest_usable(answer: str | None) -> str:
    degeneration = detect_degeneration(answer)
    if degeneration:
        raise RuntimeError(degeneration)
    if is_empty_digest(answer):
        raise RuntimeError("搜索模型没有返回检索结果")
    text = str(answer or "").strip()
    excerpt = re.sub(r"\s+", " ", text)[:240]
    if looks_like_json_literal(text):
        raise RuntimeError(f"整理稿是 JSON 字面量而非 Markdown 正文。上游原文：{excerpt}")
    actual = substantive_length(text)
    if actual < MIN_DIGEST_CHARS:
        raise RuntimeError(
            f"整理稿实质正文仅 {actual} 字（下限 {MIN_DIGEST_CHARS}），"
            f"疑似只返回了引用标记或结构骨架。上游原文：{excerpt}"
        )
    return text


def is_http_url(url: str | None) -> bool:
    value = str(url or "").strip()
    return value.startswith("http://") or value.startswith("https://")


def is_public_http_url(url: str | None) -> bool:
    if not is_http_url(url):
        return False
    parsed = urlparse(str(url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    return _host_is_public(parsed.hostname)


def assert_public_http_url(url: str | None, *, reason: str = "URL") -> str:
    value = str(url or "").strip()
    if not is_public_http_url(value):
        raise RuntimeError(f"{reason} 不是可访问的公网 http(s) 地址: {value or '(empty)'}")
    return value


def _host_is_public(hostname: str) -> bool:
    host = hostname.strip("[]").lower()
    if not host or host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"} or host.endswith(".localhost"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return bool(ip.is_global)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


def probe_http_url(url: str, *, timeout: float = 3.0) -> bool:
    if not is_http_url(url):
        return False
    current = url
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            for _ in range(3):
                parsed = urlparse(current)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname or not _host_is_public(parsed.hostname):
                    return False
                response = client.head(current)
                status = response.status_code
                headers = response.headers
                if status in {405, 501}:
                    with client.stream("GET", current) as streamed:
                        status = streamed.status_code
                        headers = streamed.headers
                if 200 <= status < 300:
                    return True
                if 300 <= status < 400:
                    location = headers.get("location")
                    if not location:
                        return False
                    current = urljoin(current, location)
                    continue
                return False
    except Exception:
        return False
    return False


def filter_reachable_http_items(
    items: list[dict[str, Any]] | None,
    *,
    probe: Callable[[str], bool] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    checker = probe or (lambda url: probe_http_url(url))
    alive: list[dict[str, Any]] = []
    dropped: list[str] = []
    for item in items or []:
        url = str(item.get("url") or "").strip()
        if not is_http_url(url):
            dropped.append(url or "(empty)")
            continue
        if checker(url):
            alive.append(item)
        else:
            dropped.append(url)
    return alive, dropped


def require_reachable_sources(
    items: list[dict[str, Any]] | None,
    *,
    probe: Callable[[str], bool] | None = None,
    raw_excerpt: str = "",
) -> list[dict[str, Any]]:
    alive, dropped = filter_reachable_http_items(items, probe=probe)
    if alive:
        return alive
    excerpt = re.sub(r"\s+", " ", raw_excerpt or "").strip()[:240]
    dropped_text = "、".join(dropped[:5]) if dropped else "无"
    detail = f"检索未获得任何可达来源（已丢弃: {dropped_text}）"
    if excerpt:
        detail += f"。上游原文：{excerpt}"
    raise RuntimeError(detail)
