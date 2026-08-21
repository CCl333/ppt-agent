from __future__ import annotations

import pytest

from app.services.generation import normalize_outline_payload

_FLAT = {
    "cover": {"title": "封面"},
    "table_of_contents": {"title": "目录", "content": ["行程"]},
    "parts": [{"part_title": "行程", "pages": [{"title": "第一天", "content": ["故宫"]}]}],
    "end_page": {"title": "谢谢", "content": []},
}


def test_keeps_canonical_ppt_outline():
    payload = {"ppt_outline": _FLAT}
    assert normalize_outline_payload(payload) is payload


def test_wraps_flat_outline_body():
    assert normalize_outline_payload(_FLAT) == {"ppt_outline": _FLAT}


def test_wraps_alternate_ppt_outline_key():
    assert normalize_outline_payload({"PPT_OUTLINE": _FLAT}) == {"ppt_outline": _FLAT}


def test_unwraps_nested_alternate_key():
    nested = {"ppt_outline": _FLAT}
    assert normalize_outline_payload({"PPT_OUTLINE": nested}) == nested


def test_rejects_unrelated_object():
    with pytest.raises(RuntimeError, match="缺少 ppt_outline"):
        normalize_outline_payload({"foo": 1})
