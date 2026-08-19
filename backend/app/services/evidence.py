from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.entities import SourceChunk, SourceDocument

DEFAULT_TOKEN_BUDGET = 60_000
DEFAULT_MAX_CHUNKS_PER_DOCUMENT = 6


@dataclass(frozen=True)
class EvidenceItem:
    chunk: SourceChunk
    document: SourceDocument
    search_rank: int
    token_count: int


def estimate_tokens(text: str) -> int:
    cleaned = (text or "").strip()
    if not cleaned:
        return 1
    return max(1, (len(cleaned) + 1) // 2)


def document_search_rank(document: SourceDocument) -> int:
    metadata = document.metadata_json if isinstance(document.metadata_json, dict) else {}
    raw = metadata.get("search_rank")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 10_000


def select_evidence(
    session: Session,
    collection_id: str,
    *,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    max_chunks_per_document: int = DEFAULT_MAX_CHUNKS_PER_DOCUMENT,
    limit: int | None = None,
) -> list[EvidenceItem]:
    """Deterministic evidence: Bocha rank, then early chunks, capped by token budget."""
    chunks = list(
        session.scalars(
            select(SourceChunk)
            .join(SourceDocument, SourceDocument.id == SourceChunk.source_document_id)
            .where(SourceDocument.collection_id == collection_id)
            .options(selectinload(SourceChunk.source_document))
        )
    )
    ranked = sorted(
        chunks,
        key=lambda chunk: (
            document_search_rank(chunk.source_document),
            chunk.chunk_index,
            chunk.created_at.isoformat() if chunk.created_at else "",
        ),
    )
    selected: list[EvidenceItem] = []
    per_document: dict[str, int] = {}
    used_tokens = 0
    for chunk in ranked:
        document_id = chunk.source_document_id
        if per_document.get(document_id, 0) >= max_chunks_per_document:
            continue
        token_count = chunk.token_count or estimate_tokens(chunk.content_md)
        if selected and used_tokens + token_count > token_budget:
            break
        selected.append(
            EvidenceItem(
                chunk=chunk,
                document=chunk.source_document,
                search_rank=document_search_rank(chunk.source_document),
                token_count=token_count,
            )
        )
        per_document[document_id] = per_document.get(document_id, 0) + 1
        used_tokens += token_count
        if limit is not None and len(selected) >= limit:
            break
    return selected


def evidence_payloads(items: list[EvidenceItem]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        payloads.append(
            {
                "chunk_id": item.chunk.id,
                "source_document_id": item.document.id,
                "title": item.document.title,
                "url": item.document.source_uri,
                "excerpt_md": item.chunk.content_md,
                "search_rank": item.search_rank,
                "chunk_index": item.chunk.chunk_index,
                "rank_no": index,
                "token_count": item.token_count,
            }
        )
    return payloads
