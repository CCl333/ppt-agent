from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.entities import SourceChunk, SourceDocument

DEFAULT_TOKEN_BUDGET = 60_000
DEFAULT_MAX_CHUNKS_PER_DOCUMENT = 6
DEFAULT_LLM_ANSWER_MAX_CHUNKS = 40
SECTION_PATH_WEIGHT = 1.5

_TOKEN_RE = re.compile(r"[0-9A-Za-z]+|[\u4e00-\u9fff]+")


@dataclass(frozen=True)
class EvidenceItem:
    chunk: SourceChunk
    document: SourceDocument
    search_rank: int
    token_count: int
    relevance_score: float = 0.0


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


def tokenize(text: str) -> list[str]:
    """Split Latin/digit runs as whole tokens; CJK as overlapping 2-grams."""
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer((text or "").lower()):
        piece = match.group(0)
        if re.fullmatch(r"[0-9A-Za-z]+", piece):
            tokens.append(piece)
            continue
        if len(piece) == 1:
            tokens.append(piece)
            continue
        tokens.extend(piece[index : index + 2] for index in range(len(piece) - 1))
    return tokens


def keyword_score(query_terms: list[str] | str, text: str) -> float:
    if isinstance(query_terms, str):
        query_blob = query_terms
    else:
        query_blob = " ".join(term for term in query_terms if term)
    query_tokens = set(tokenize(query_blob))
    text_tokens = set(tokenize(text))
    if not query_tokens or not text_tokens:
        return 0.0
    return len(query_tokens & text_tokens) / max(len(query_tokens), 1)


def chunk_relevance_score(chunk: SourceChunk, query_terms: list[str]) -> float:
    body_score = keyword_score(query_terms, chunk.content_md)
    section_score = keyword_score(query_terms, chunk.section_path or "")
    return body_score + SECTION_PATH_WEIGHT * section_score


def select_evidence(
    session: Session,
    collection_id: str,
    *,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    max_chunks_per_document: int = DEFAULT_MAX_CHUNKS_PER_DOCUMENT,
    limit: int | None = None,
    query_terms: list[str] | None = None,
) -> list[EvidenceItem]:
    """Deterministic evidence: optional keyword relevance, else Bocha rank then early chunks."""
    chunks = list(
        session.scalars(
            select(SourceChunk)
            .join(SourceDocument, SourceDocument.id == SourceChunk.source_document_id)
            .where(SourceDocument.collection_id == collection_id)
            .options(selectinload(SourceChunk.source_document))
        )
    )
    terms = [term.strip() for term in (query_terms or []) if str(term).strip()]

    def sort_key(chunk: SourceChunk) -> tuple[Any, ...]:
        if terms:
            return (
                -chunk_relevance_score(chunk, terms),
                document_search_rank(chunk.source_document),
                chunk.chunk_index,
            )
        return (
            document_search_rank(chunk.source_document),
            chunk.chunk_index,
            chunk.created_at.isoformat() if chunk.created_at else "",
        )

    ranked = sorted(chunks, key=sort_key)
    selected: list[EvidenceItem] = []
    per_document: dict[str, int] = {}
    used_tokens = 0
    for chunk in ranked:
        document = chunk.source_document
        document_id = chunk.source_document_id
        document_cap = (
            DEFAULT_LLM_ANSWER_MAX_CHUNKS
            if document.source_type == "llm_answer"
            else max_chunks_per_document
        )
        if per_document.get(document_id, 0) >= document_cap:
            continue
        token_count = chunk.token_count or estimate_tokens(chunk.content_md)
        if selected and used_tokens + token_count > token_budget:
            break
        selected.append(
            EvidenceItem(
                chunk=chunk,
                document=document,
                search_rank=document_search_rank(document),
                token_count=token_count,
                relevance_score=chunk_relevance_score(chunk, terms) if terms else 0.0,
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
                "relevance_score": item.relevance_score,
            }
        )
    return payloads
