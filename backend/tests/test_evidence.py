from __future__ import annotations

from app.models.entities import SourceChunk
from app.services.evidence import (
    DEFAULT_MAX_CHUNKS_PER_DOCUMENT,
    keyword_score,
    select_evidence,
    tokenize,
)
from app.services.research import ResearchService
from tests.helpers import add_page_chunk, make_content_page, make_project


def test_tokenize_uses_cjk_bigrams_and_keeps_latin_words():
    assert tokenize("AI medical imaging") == ["ai", "medical", "imaging"]
    assert tokenize("人工智能应用") == ["人工", "工智", "智能", "能应", "应用"]


def test_keyword_score_matches_chinese_partial_overlap():
    query = "人工智能医疗应用"
    early = "公司简介成立于1998年总部设在北京主营业务包括软件开发与系统集成"
    late = "人工智能在医疗领域的应用包括影像辅助诊断和药物研发加速临床决策"
    assert keyword_score(query, early) == 0.0
    assert keyword_score(query, late) > 0.5


def test_select_evidence_prefers_better_search_rank_and_early_chunks(db_session):
    project = make_project(db_session, stage="search")
    page = make_content_page(db_session, project, page_code="page-03", title="证据页", sort_order=1)
    later = add_page_chunk(
        db_session,
        project,
        page,
        uri="https://example.com/later",
        title="后排文档",
        content="LaterRank evidence",
    )
    later.source_document.metadata_json = {"search_rank": 2}
    earlier = add_page_chunk(
        db_session,
        project,
        page,
        uri="https://example.com/earlier",
        title="前排文档",
        content="EarlierRank evidence",
    )
    earlier.source_document.metadata_json = {"search_rank": 1}
    db_session.commit()

    collection = ResearchService(db_session).get_or_create_page_collection(project, page)
    selected = select_evidence(db_session, collection.id, token_budget=60_000, limit=10)
    assert [item.document.title for item in selected] == ["前排文档", "后排文档"]


def test_select_evidence_without_query_terms_is_unchanged(db_session):
    project = make_project(db_session, stage="search")
    page = make_content_page(db_session, project, page_code="page-03", title="证据页", sort_order=1)
    later = add_page_chunk(
        db_session,
        project,
        page,
        uri="https://example.com/later",
        title="后排文档",
        content="KeywordSecret later document",
    )
    later.source_document.metadata_json = {"search_rank": 2}
    earlier = add_page_chunk(
        db_session,
        project,
        page,
        uri="https://example.com/earlier",
        title="前排文档",
        content="Unrelated early document",
    )
    earlier.source_document.metadata_json = {"search_rank": 1}
    db_session.commit()

    collection = ResearchService(db_session).get_or_create_page_collection(project, page)
    selected = select_evidence(db_session, collection.id)
    with_empty = select_evidence(db_session, collection.id, query_terms=None)
    assert [item.chunk.id for item in selected] == [item.chunk.id for item in with_empty]
    assert [item.document.title for item in selected] == ["前排文档", "后排文档"]


def test_select_evidence_ranks_by_relevance(db_session):
    project = make_project(db_session, stage="search")
    page = make_content_page(db_session, project, page_code="page-03", title="证据页", sort_order=1)
    irrelevant = add_page_chunk(
        db_session,
        project,
        page,
        uri="https://example.com/rank-first",
        title="高排名无关文档",
        content="公司简介成立于1998年总部设在北京主营业务包括软件开发",
    )
    irrelevant.source_document.metadata_json = {"search_rank": 1}
    relevant = add_page_chunk(
        db_session,
        project,
        page,
        uri="https://example.com/rank-later",
        title="低排名相关文档",
        content="人工智能在医疗领域的应用包括影像辅助诊断",
    )
    relevant.source_document.metadata_json = {"search_rank": 8}
    db_session.commit()

    collection = ResearchService(db_session).get_or_create_page_collection(project, page)
    selected = select_evidence(
        db_session,
        collection.id,
        query_terms=["人工智能医疗应用"],
    )
    assert selected[0].document.title == "低排名相关文档"
    assert selected[0].relevance_score > selected[1].relevance_score


def test_select_evidence_picks_late_relevant_chunk(db_session):
    project = make_project(db_session, stage="search")
    page = make_content_page(db_session, project, page_code="page-03", title="证据页", sort_order=1)
    first = add_page_chunk(
        db_session,
        project,
        page,
        uri="https://example.com/long",
        title="长文",
        content="公司简介成立于1998年总部设在北京",
    )
    first.source_document.metadata_json = {"search_rank": 1}
    for index in range(1, 10):
        content = (
            "人工智能在医疗领域的应用包括影像辅助诊断和药物研发"
            if index == 8
            else f"无关背景段落{index}介绍组织架构与发展历程"
        )
        db_session.add(
            SourceChunk(
                source_document_id=first.source_document_id,
                chunk_index=index,
                section_path="正文",
                content_md=content,
                content_for_match=content,
                token_count=20,
            )
        )
    db_session.commit()

    collection = ResearchService(db_session).get_or_create_page_collection(project, page)
    selected = select_evidence(
        db_session,
        collection.id,
        query_terms=["人工智能医疗应用"],
        max_chunks_per_document=6,
    )
    assert selected[0].chunk.chunk_index == 8


def test_select_evidence_caps_chunks_per_document(db_session):
    project = make_project(db_session, stage="search")
    page = make_content_page(db_session, project, page_code="page-03", title="证据页", sort_order=1)
    first = add_page_chunk(
        db_session,
        project,
        page,
        uri="https://example.com/doc",
        title="同一文档",
        content="chunk-0",
    )
    for index in range(1, 5):
        db_session.add(
            SourceChunk(
                source_document_id=first.source_document_id,
                chunk_index=index,
                content_md=f"chunk-{index}",
                content_for_match=f"chunk-{index}",
                token_count=8,
            )
        )
    first.source_document.metadata_json = {"search_rank": 1}
    db_session.commit()

    collection = ResearchService(db_session).get_or_create_page_collection(project, page)
    selected = select_evidence(db_session, collection.id, max_chunks_per_document=2, token_budget=60_000)
    assert len(selected) == 2
    assert [item.chunk.chunk_index for item in selected] == [0, 1]


def test_select_evidence_respects_per_document_cap(db_session):
    project = make_project(db_session, stage="search")
    page = make_content_page(db_session, project, page_code="page-03", title="证据页", sort_order=1)
    first = add_page_chunk(
        db_session,
        project,
        page,
        uri="https://example.com/doc",
        title="同一文档",
        content="chunk-0",
    )
    for index in range(1, 20):
        db_session.add(
            SourceChunk(
                source_document_id=first.source_document_id,
                chunk_index=index,
                content_md=f"chunk-{index}",
                content_for_match=f"chunk-{index}",
                token_count=8,
            )
        )
    first.source_document.metadata_json = {"search_rank": 1}
    db_session.commit()

    collection = ResearchService(db_session).get_or_create_page_collection(project, page)
    selected = select_evidence(db_session, collection.id)
    assert len(selected) == DEFAULT_MAX_CHUNKS_PER_DOCUMENT
    assert [item.chunk.chunk_index for item in selected] == list(range(DEFAULT_MAX_CHUNKS_PER_DOCUMENT))
