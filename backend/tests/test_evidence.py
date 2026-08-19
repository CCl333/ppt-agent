from __future__ import annotations

from app.services.evidence import select_evidence
from tests.helpers import add_page_chunk, make_content_page, make_project


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

    from app.services.research import ResearchService

    collection = ResearchService(db_session).get_or_create_page_collection(project, page)
    selected = select_evidence(db_session, collection.id, token_budget=60_000, limit=10)
    assert [item.document.title for item in selected] == ["前排文档", "后排文档"]


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
    from app.models.entities import SourceChunk

    for index in range(1, 5):
        db_session.add(
            SourceChunk(
                source_document_id=first.source_document_id,
                chunk_index=index,
                content_md=f"chunk-{index}",
                content_for_embedding=f"chunk-{index}",
                token_count=8,
            )
        )
    first.source_document.metadata_json = {"search_rank": 1}
    db_session.commit()

    from app.services.research import ResearchService

    collection = ResearchService(db_session).get_or_create_page_collection(project, page)
    selected = select_evidence(db_session, collection.id, max_chunks_per_document=2, token_budget=60_000)
    assert len(selected) == 2
    assert [item.chunk.chunk_index for item in selected] == [0, 1]
