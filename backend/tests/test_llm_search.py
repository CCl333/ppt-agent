from __future__ import annotations

from app.models.entities import SourceChunk, SourceDocument
from app.services.evidence import DEFAULT_MAX_CHUNKS_PER_DOCUMENT, select_evidence
from app.services.model_gateway import LiveSearchDigest, parse_search_live_answer
from app.services.research import ResearchService
from app.services.search_settings import SearchRuntime
from tests.helpers import make_content_page, make_project


def test_parse_search_live_answer_grok_shape():
    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        "人工智能正在进入临床影像辅助诊断。\n"
                        "- [来源A](https://example.com/a) 一句话摘要"
                    )
                }
            }
        ],
        "citations": ["https://example.com/a"],
    }
    answer = parse_search_live_answer(payload)
    assert "临床影像辅助诊断" in answer
    assert "https://example.com/a" in answer


def test_parse_search_live_answer_anthropic_shape():
    payload = {
        "content": [
            {"type": "text", "text": "第一段整理。"},
            {"type": "tool_use", "name": "web_search"},
            {"type": "text", "text": "第二段补充事实。"},
        ]
    }
    assert parse_search_live_answer(payload) == "第一段整理。第二段补充事实。"


def test_parse_search_live_answer_json_shape():
    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"digest_md":"整理稿正文","items":[{"title":"A","url":"https://example.com/a","snippet":"s"}]}'
                    )
                }
            }
        ]
    }
    assert parse_search_live_answer(payload) == "整理稿正文"


def test_ingest_llm_answer_creates_document_and_chunks(db_session):
    project = make_project(db_session, stage="search")
    page = make_content_page(db_session, project, page_code="page-03", title="医疗AI", sort_order=1)
    research = ResearchService(db_session)
    collection = research.get_or_create_page_collection(project, page)

    cards, pending = research.ingest_llm_answer(
        collection=collection,
        page=page,
        answer="# 整理稿\n\n人工智能在医疗影像辅助诊断中的应用正在加快。\n\n## 进展\n医院开始试点。",
        sources=[{"title": "A", "url": "https://example.com/a", "snippet": "摘要"}],
        query_text="医疗AI",
        page_title="医疗AI",
    )
    research.store_chunks(pending)
    db_session.commit()

    documents = list(db_session.query(SourceDocument).filter(SourceDocument.collection_id == collection.id))
    assert len(documents) == 1
    assert documents[0].source_type == "llm_answer"
    chunks = list(db_session.query(SourceChunk).filter(SourceChunk.source_document_id == documents[0].id))
    assert chunks
    digest = research.build_collection_digest(collection.id)
    assert digest["document_count"] == 1
    assert digest["chunk_count"] == len(chunks)
    assert cards[0]["source_kind"] == "llm_answer"
    assert cards[0]["source_document_id"] == documents[0].id


def test_llm_mode_page_search_does_not_call_reader(service, db_session, monkeypatch):
    project = make_project(db_session, stage="search")
    page = make_content_page(db_session, project, page_code="page-03", title="医疗AI", sort_order=1)
    page.page_search_queries_json = [{"query_text": "人工智能医疗应用", "query_purpose": "定义类"}]
    db_session.commit()

    monkeypatch.setattr(
        "app.services.search_settings.snapshot_search_runtime",
        lambda: SearchRuntime(mode="llm", bocha_auth_header=""),
    )
    reader_calls: list[str] = []
    monkeypatch.setattr(
        service.research.mcp,
        "read_url_markdown",
        lambda url: reader_calls.append(url),
    )
    monkeypatch.setattr(
        service.research.mcp,
        "search_web_digest",
        lambda query, limit=8: LiveSearchDigest(
            answer="整理稿：人工智能医疗应用包括影像辅助诊断。",
            items=[{"title": "A", "url": "https://example.com/unreachable", "snippet": "s"}],
        ),
    )

    service._run_page_search(
        project=project,
        page=page,
        latest_instruction="",
        replace_existing=True,
    )
    db_session.refresh(page)
    assert reader_calls == []
    assert page.page_corpus_digest_json.get("document_count") == 1
    assert page.search_status == "ready"
    documents = list(
        db_session.query(SourceDocument).filter(SourceDocument.source_type == "llm_answer")
    )
    assert len(documents) == 1


def test_llm_mode_keeps_page_isolation(db_session):
    project = make_project(db_session, stage="search")
    page_a = make_content_page(db_session, project, page_code="page-03", title="页面 A", sort_order=1)
    page_b = make_content_page(db_session, project, page_code="page-04", title="页面 B", sort_order=2)
    research = ResearchService(db_session)
    collection_a = research.get_or_create_page_collection(project, page_a)
    collection_b = research.get_or_create_page_collection(project, page_b)
    research.ingest_llm_answer(
        collection=collection_a,
        page=page_a,
        answer="AlphaSecretToken 只属于页面 A 的整理稿。",
        sources=[],
        page_title="页面 A",
    )
    cards_b, pending_b = research.ingest_llm_answer(
        collection=collection_b,
        page=page_b,
        answer="BetaSecretToken 只属于页面 B 的整理稿。",
        sources=[],
        page_title="页面 B",
    )
    research.store_chunks(pending_b)
    session = research.create_session(
        project_id=project.id,
        page_id=page_b.id,
        scope_type="page",
        session_role="page_summary",
        research_goal="隔离",
        query_plan=[{"query_text": "AlphaSecretToken BetaSecretToken", "query_purpose": "test"}],
        context_snapshot={},
    )
    evidence = research.retrieve_for_collection(
        project=project,
        collection=collection_b,
        research_session=session,
        query_plan=[{"query_text": "AlphaSecretToken BetaSecretToken", "query_purpose": "test"}],
        limit=20,
    )
    text = " ".join(str(item.get("excerpt_md") or "") for item in evidence)
    assert "AlphaSecretToken" not in text
    assert "BetaSecretToken" in text
    assert cards_b[0]["source_kind"] == "llm_answer"


def test_search_results_as_evidence_keeps_digest_excerpt(db_session):
    research = ResearchService(db_session)
    evidence = research.search_results_as_evidence(
        [
            {
                "title": "整理稿",
                "url": "llm-search://digest/abc",
                "content_excerpt_md": "这是模型整理稿正文，应进入大纲。",
                "snippet": "短摘要",
                "bocha_summary": "更短",
                "search_rank": 0,
            },
            {
                "title": "",
                "url": "https://example.com/only-url",
                "snippet": "",
                "bocha_summary": "",
            },
        ]
    )
    assert all(not item["url"].startswith("llm-search://") for item in evidence)
    assert evidence[0]["title"] == "https://example.com/only-url"
    assert evidence[0]["excerpt_md"] == "https://example.com/only-url"


def test_select_evidence_llm_answer_raises_chunk_cap(db_session):
    project = make_project(db_session, stage="search")
    page = make_content_page(db_session, project, page_code="page-03", title="长整理稿", sort_order=1)
    research = ResearchService(db_session)
    collection = research.get_or_create_page_collection(project, page)
    document = SourceDocument(
        collection_id=collection.id,
        source_type="llm_answer",
        source_uri=f"llm-search://{page.id}/1",
        title="整理稿：长整理稿",
        markdown_content="x",
        metadata_json={"search_rank": 0},
        status="ready",
    )
    db_session.add(document)
    db_session.flush()
    for index in range(20):
        db_session.add(
            SourceChunk(
                source_document_id=document.id,
                chunk_index=index,
                content_md=f"chunk-{index} 人工智能医疗应用",
                content_for_match=f"chunk-{index}",
                token_count=8,
            )
        )
    db_session.commit()
    selected = select_evidence(db_session, collection.id, query_terms=["人工智能医疗"])
    assert len(selected) > DEFAULT_MAX_CHUNKS_PER_DOCUMENT
    assert len(selected) == 20
