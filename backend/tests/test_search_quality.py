from __future__ import annotations

import pytest

from app.services.search_quality import (
    _host_is_public,
    assert_digest_usable,
    detect_degeneration,
    is_empty_digest,
    require_reachable_sources,
)


def test_empty_digest_ignores_fences_and_punctuation():
    assert is_empty_digest("```\n没有检索结果\n```") is True
    assert is_empty_digest("没有检索结果。") is True
    assert is_empty_digest("故宫博物院开放公告") is False


def test_detect_degeneration_tool_call_and_repeat():
    assert detect_degeneration("```html\ninvoke tool web_search(") is not None
    repeated = "\n".join(["## 故宫周边", "一些内容"] * 3)
    assert detect_degeneration(repeated) is not None


def test_assert_digest_usable_raises():
    with pytest.raises(RuntimeError, match="脚手架"):
        assert_digest_usable("<tool_call>web_search")
    with pytest.raises(RuntimeError, match="没有返回检索结果"):
        assert_digest_usable("没有检索结果")


def test_require_reachable_sources_hard_fails_when_all_dead():
    with pytest.raises(RuntimeError, match="未获得任何可达来源"):
        require_reachable_sources(
            [{"title": "假", "url": "https://dead.example", "snippet": "x"}],
            probe=lambda _url: False,
            raw_excerpt="编造的整理稿",
        )


def test_require_reachable_sources_keeps_live_http_only():
    alive = require_reachable_sources(
        [
            {"title": "整理稿", "url": "llm-search://digest/abc", "snippet": "记忆"},
            {"title": "真", "url": "https://example.com/a", "snippet": "ok"},
        ],
        probe=lambda url: url.startswith("https://example.com"),
    )
    assert len(alive) == 1
    assert alive[0]["url"] == "https://example.com/a"


def test_host_is_public_rejects_private_and_metadata():
    assert _host_is_public("127.0.0.1") is False
    assert _host_is_public("localhost") is False
    assert _host_is_public("10.0.0.1") is False
    assert _host_is_public("169.254.169.254") is False
    assert _host_is_public("8.8.8.8") is True
