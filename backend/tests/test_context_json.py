from __future__ import annotations

import pytest

from app.services.model_gateway import parse_context_json


def test_parse_plain_json_object():
    assert parse_context_json('{"ok": true}') == {"ok": True}


def test_parse_tagged_outline_json():
    text = '[PPT_OUTLINE]\n{"ppt_outline": {"cover": {"title": "封面"}}}\n[/PPT_OUTLINE]'
    assert parse_context_json(text)["ppt_outline"]["cover"]["title"] == "封面"


def test_parse_tagged_json_with_prefix_noise():
    text = 'thinking...\n[PPT_OUTLINE]\n{"ok": true}\n[/PPT_OUTLINE]\n'
    assert parse_context_json(text) == {"ok": True}


def test_parse_markdown_json_fence():
    assert parse_context_json("```json\n{\"ok\": true}\n```") == {"ok": True}


def test_parse_prefers_tag_wrapper_over_surrounding_text():
    text = 'note: ignore me\n[PPT_OUTLINE]\n{"picked": 1}\n[/PPT_OUTLINE]'
    assert parse_context_json(text) == {"picked": 1}


def test_parse_rejects_empty_and_non_json():
    with pytest.raises(RuntimeError, match="不是有效 JSON"):
        parse_context_json("   ")
    with pytest.raises(RuntimeError, match="不是有效 JSON"):
        parse_context_json("not json")
