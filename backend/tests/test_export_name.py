from __future__ import annotations

import pytest

from app.services.export_name import looks_like_title, resolve_export_stem, safe_filename


def test_safe_filename_keeps_chinese_and_strips_illegal():
    assert safe_filename('2026年北京五日深度游:全攻略') == "2026年北京五日深度游_全攻略"
    assert safe_filename("a..b") == "a_b"
    assert safe_filename("a\x00b") == "ab"


def test_looks_like_title_rejects_request_snippets():
    assert looks_like_title("请帮我生成一份约 12 页的咨询风 PPT") is False
    assert looks_like_title("2026年北京五日深度游全攻略") is True


def test_resolve_export_stem_prefers_cover_title():
    stem = resolve_export_stem(
        outline_json={"ppt_outline": {"cover": {"title": "封面标题"}}},
        first_page_title="第一页",
        project_title="请生成PPT",
    )
    assert stem == "封面标题"


def test_resolve_export_stem_falls_back_to_first_page_then_project():
    assert (
        resolve_export_stem(outline_json={}, first_page_title="第一页标题", project_title="请生成")
        == "第一页标题"
    )
    assert (
        resolve_export_stem(outline_json={}, first_page_title="", project_title="企业落地路径")
        == "企业落地路径"
    )


def test_resolve_export_stem_raises_when_nothing_usable():
    with pytest.raises(RuntimeError, match="无法确定导出文件名"):
        resolve_export_stem(outline_json={}, first_page_title="", project_title="请帮我生成约 8 页")
