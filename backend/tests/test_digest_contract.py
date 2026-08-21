"""整理稿契约测试。

覆盖 test_llm_search.py 三个 happy-path 用例没有覆盖的真实失效形态——
这些形态在生产中真实发生过，正是它们没有测试才让 bug 存活：
  - `[6]`（3 字符，仅引用标记）
  - `{"draft": "..."}` / `{"query": "..."}`（模型自创键名的 JSON 信封）
  - `/responses` 同时下发 Markdown 指令与 text.format=json_object 导致输出退化
"""
from __future__ import annotations

import pytest

from app.services.model_gateway import (
    LiveSearchDigest,
    _ModelConfig,
    _OpenAICompatibleClient,
    parse_search_live_answer,
)
from app.services.search_quality import (
    MIN_DIGEST_CHARS,
    assert_digest_usable,
    looks_like_json_literal,
    substantive_length,
)
from tests.helpers import SAMPLE_DIGEST_MD, make_content_page, make_project

GOOD_DIGEST = SAMPLE_DIGEST_MD


# --- 解析层 -----------------------------------------------------------------


def test_parse_answer_extracts_body_from_unknown_json_key():
    """模型不守 Markdown 契约、自创键名时，取最长字符串值而非原样返回整段 JSON。"""
    body = "# 第1-2日：中轴皇城\\n\\n天安门—故宫—景山连成一线，建议午门进神武门出，主要殿宇约三小时。"
    payload = {
        "choices": [{"message": {"content": '{"draft": "' + body + '", "sources": "- [A](https://example.com/a)"}'}}]
    }
    answer = parse_search_live_answer(payload, model="grok-test")
    assert answer.startswith("# 第1-2日：中轴皇城")
    assert "神武门" in answer
    assert not answer.startswith("{"), "整段 JSON 原样入库正是旧的坏数据路径"


def test_parse_answer_keeps_digest_md_contract():
    payload = {"choices": [{"message": {"content": '{"digest_md":"整理稿正文","items":[]}'}}]}
    assert parse_search_live_answer(payload) == "整理稿正文"


def test_parse_answer_keeps_plain_markdown():
    payload = {"choices": [{"message": {"content": GOOD_DIGEST}}]}
    assert parse_search_live_answer(payload) == GOOD_DIGEST.strip()


def test_parse_answer_does_not_mistake_embedded_json_for_envelope():
    """正文里含 JSON 片段的合格整理稿，不能被当成 JSON 信封截走正文。"""
    markdown = GOOD_DIGEST + '\n示例响应：{"code": 0, "msg": "ok"}\n'
    payload = {"choices": [{"message": {"content": markdown}}]}
    answer = parse_search_live_answer(payload)
    assert answer.startswith("# 六日编排逻辑")
    assert "空间格局" in answer


def test_parse_answer_does_not_mistake_fenced_json_block_for_envelope():
    """正文中段的 ```json 代码块同样不能被当成信封——这是贪婪匹配的第二个入口。"""
    markdown = GOOD_DIGEST + '\n```json\n{"code": 0, "msg": "ok"}\n```\n'
    payload = {"choices": [{"message": {"content": markdown}}]}
    answer = parse_search_live_answer(payload)
    assert answer.startswith("# 六日编排逻辑")
    assert "常见踩坑" in answer


def test_parse_answer_unwraps_whole_text_fenced_envelope():
    """整段就是一个 ```json 代码块时，才按信封解包。"""
    payload = {"choices": [{"message": {"content": '```json\n{"digest_md":"整理稿正文"}\n```'}}]}
    assert parse_search_live_answer(payload) == "整理稿正文"


# --- 质量闸门 ---------------------------------------------------------------


def test_substantive_length_strips_citations_and_urls():
    assert substantive_length("[6]") == 0
    assert substantive_length("[[1]](https://example.com/a)") == 0
    assert substantive_length("正文两字") == 4


def test_looks_like_json_literal():
    assert looks_like_json_literal('{"query": "北京"}') is True
    assert looks_like_json_literal("[1, 2]") is True
    assert looks_like_json_literal(GOOD_DIGEST) is False


@pytest.mark.parametrize(
    "bad",
    [
        "[6]",
        "[1]",
        '{ "query": "北京旅游景点空间格局 二环皇城中轴线 西北郊园林 北部长城" }',
        "[[1]](https://example.com/a)[[2]](https://example.com/b)",
        "# 整理稿：六日编排逻辑\n\n[6]\n\n支持查询：北京旅游",
    ],
)
def test_assert_digest_usable_rejects_real_bad_shapes(bad):
    with pytest.raises(RuntimeError):
        assert_digest_usable(bad)


def test_assert_digest_usable_accepts_real_digest():
    assert assert_digest_usable(GOOD_DIGEST) == GOOD_DIGEST.strip()
    assert substantive_length(GOOD_DIGEST) >= MIN_DIGEST_CHARS


def test_assert_digest_usable_error_carries_upstream_excerpt():
    with pytest.raises(RuntimeError) as excinfo:
        assert_digest_usable("[6]")
    assert "[6]" in str(excinfo.value)


# --- 传输层：不得再强制 json_object -----------------------------------------


def _client(path: str, model: str = "grok-4.6", base_url: str = "https://relay.example.com/v1"):
    return _OpenAICompatibleClient(
        _ModelConfig(base_url=base_url, api_key="sk-test", model=model, path=path, timeout_seconds=30)
    )


def test_responses_live_search_does_not_force_json_object(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"output_text": GOOD_DIGEST, "output": [{"type": "web_search_call"}]}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return FakeResponse()

    monkeypatch.setattr("app.services.model_gateway.httpx.post", fake_post)
    _client("/responses").live_search("q", system_prompt="sys", limit=3)

    body = captured["json"]
    assert "text" not in body, "text.format=json_object 与 Markdown 指令冲突，实测导致正文退化到 3 字符"
    assert body["tools"][0]["type"] == "web_search"


def test_chat_live_search_does_not_force_json_object(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": GOOD_DIGEST}}]}

    def fake_post(url, **kwargs):
        captured["json"] = kwargs.get("json")
        return FakeResponse()

    monkeypatch.setattr("app.services.model_gateway.httpx.post", fake_post)
    # 非 grok 名 + /chat/completions → 走 search_parameters 分支
    _client("/chat/completions", model="gpt-4o", base_url="https://api.example.com/v1").live_search(
        "q", system_prompt="sys", limit=3
    )

    body = captured["json"]
    assert "response_format" not in body
    assert body["search_parameters"]["mode"] == "on"


# --- 重试边界 ---------------------------------------------------------------


def _digest(answer: str) -> LiveSearchDigest:
    return LiveSearchDigest(answer=answer, items=[{"title": "A", "url": "https://example.com/a", "snippet": "s"}])


def test_search_web_digest_retries_once_then_succeeds(monkeypatch):
    from app.services.mcp_gateway import McpGateway
    from app.services.model_gateway import ModelGateway

    answers = iter([_digest("[6]"), _digest(GOOD_DIGEST)])
    calls: list[str] = []

    def fake_search_live(self, query, *, limit=5):
        calls.append(query)
        return next(answers)

    monkeypatch.setattr(ModelGateway, "search_live", fake_search_live)
    digest = McpGateway().search_web_digest("北京六日游", limit=8)

    assert len(calls) == 2
    assert digest.answer == GOOD_DIGEST.strip()


def test_search_web_digest_raises_after_second_degeneration(monkeypatch):
    from app.services.mcp_gateway import McpGateway
    from app.services.model_gateway import ModelGateway

    calls: list[str] = []

    def fake_search_live(self, query, *, limit=5):
        calls.append(query)
        return _digest("[6]")

    monkeypatch.setattr(ModelGateway, "search_live", fake_search_live)
    with pytest.raises(RuntimeError):
        McpGateway().search_web_digest("北京六日游", limit=8)
    assert len(calls) == 2, "只重试一次，不许无限重试"


# --- 资料池闸门 -------------------------------------------------------------


def test_build_collection_digest_reports_content_chars(db_session):
    from app.services.research import ResearchService
    from tests.helpers import add_page_chunk

    project = make_project(db_session, stage="search")
    page = make_content_page(db_session, project, page_code="page-03", title="资料池", sort_order=1)
    add_page_chunk(
        db_session, project, page, uri="https://example.com/a", title="来源A", content="正文" * 100
    )
    research = ResearchService(db_session)
    collection = research.get_or_create_page_collection(project, page)

    digest = research.build_collection_digest(collection.id)
    assert digest["document_count"] == 1
    assert digest["content_chars"] == 200


def test_run_page_summary_rejects_thin_corpus(service, db_session):
    project = make_project(db_session, stage="search")
    page = make_content_page(db_session, project, page_code="page-03", title="六日编排逻辑", sort_order=1)
    # 线上坏数据的真实形态：有 1 篇文档，但全文只有 `[6]` 三个字符。
    page.page_corpus_digest_json = {"document_count": 1, "content_chars": 3}
    page.summary_status = "empty"
    db_session.commit()

    with pytest.raises(RuntimeError) as excinfo:
        service._run_page_summary(project=project, page=page, latest_instruction="")
    assert "3" in str(excinfo.value)
    assert page.summary_status == "empty", "闸门必须在写 running 之前拦截"


def test_run_page_summary_backfills_content_chars_for_legacy_digest(service, db_session):
    """content_chars 是后加字段：老资料池缺该键时应惰性补算，而不是一律拦死。"""
    from tests.helpers import add_page_chunk

    project = make_project(db_session, stage="search")
    page = make_content_page(db_session, project, page_code="page-03", title="六日编排逻辑", sort_order=1)
    add_page_chunk(
        db_session, project, page, uri="https://example.com/a", title="来源A", content="正文" * 3
    )
    # 老格式：只有 document_count，没有 content_chars
    page.page_corpus_digest_json = {"document_count": 1}
    page.summary_status = "empty"
    db_session.commit()

    with pytest.raises(RuntimeError) as excinfo:
        service._run_page_summary(project=project, page=page, latest_instruction="")
    # 补算出真实字符数（"正文"*3 = 6），据此拒绝，而不是按缺失值 0 拒绝
    assert "6" in str(excinfo.value)
    assert page.page_corpus_digest_json.get("content_chars") == 6
