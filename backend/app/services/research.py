from __future__ import annotations

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from typing import Any, Callable
from urllib.parse import urldefrag

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload

from app.models.base import is_cache_fresh, now_utc
from app.models.entities import (
    BochaSearchCache,
    Citation,
    Project,
    ProjectPage,
    ProjectResearchSource,
    ResearchSession,
    ResearchSource,
    SourceChunk,
    SourceCollection,
    SourceDocument,
    URLContentCache,
)
from app.services.evidence import estimate_tokens, keyword_score, select_evidence, tokenize
from app.services.mcp_gateway import McpGateway, ReadResult, SearchResult
from app.services.model_gateway import ModelGateway
from app.services.prompt_contracts import get_prompt_text, render_prompt
from app.services.search_plan import copy_plan_fields, normalize_query_plan, stamp_search_results


class ResearchService:
    def __init__(self, session: Session):
        self.session = session
        self.mcp = McpGateway()
        self.models = ModelGateway()

    def build_query_plan(
        self,
        *,
        scope_type: str,
        session_role: str,
        request_text: str,
        project_stage: str,
        project_title: str,
        fixed_fields: dict[str, Any],
        answers: dict[str, Any],
        page_title: str = "",
        page_outline: list[str] | None = None,
        page_section_title: str | None = None,
        outline_full_snapshot: list[dict[str, Any]] | None = None,
        latest_instruction: str = "",
    ) -> list[dict[str, str]]:
        result = self.models.context_json(
            get_prompt_text("research.query_rewrite.system"),
            render_prompt(
                "research.query_rewrite.user",
                {
                    "scope_type": scope_type,
                    "session_role": session_role,
                    "request_text": request_text,
                    "project_stage": project_stage,
                    "project_title": project_title,
                    "fixed_fields_json": fixed_fields,
                    "answers_json": answers,
                    "page_title": page_title,
                    "page_outline_json": page_outline or [],
                    "page_section_title": page_section_title or "",
                    "outline_full_snapshot_json": outline_full_snapshot or [],
                    "latest_instruction": latest_instruction,
                },
            ),
        )
        queries = normalize_query_plan(result)
        if not queries:
            raise RuntimeError("research.query_rewrite 没有返回有效 queries")
        return queries

    def search_query_summaries(
        self,
        query_plan: list[dict[str, str]],
        *,
        limit_per_query: int = 3,
        search_round: int = 1,
        on_query_completed: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        seen_urls: set[str] = set()
        items: list[dict[str, Any]] = []
        total_queries = len(query_plan)
        for query_index, query_item in enumerate(query_plan, start=1):
            query_text = query_item["query_text"]
            query_purpose = query_item["query_purpose"]
            query_results: list[dict[str, Any]] = []
            for search_rank, result in enumerate(self._search_query(query_text, limit_per_query), start=1):
                normalized_url = self._normalize_url(result.url)
                if not normalized_url or normalized_url in seen_urls:
                    continue
                seen_urls.add(normalized_url)
                payload = {
                    "id": self._hash_text(f"{query_text}|{normalized_url}"),
                    "query_text": query_text,
                    "query_purpose": query_purpose,
                    "search_rank": search_rank,
                    "title": result.title,
                    "url": normalized_url,
                    "bocha_summary": result.snippet,
                    "snippet": result.snippet,
                    "content_excerpt_md": result.snippet if normalized_url.startswith("llm-search://") else "",
                    "source_kind": "llm_answer" if normalized_url.startswith("llm-search://") else "url",
                    "image_url": result.image_url,
                    "extra_images": list(result.extra_images),
                }
                payload.update(copy_plan_fields(query_item))
                payload["round"] = int(search_round)
                items.append(payload)
                query_results.append(payload)
            if on_query_completed is not None:
                on_query_completed(
                    {
                        "query_index": query_index,
                        "query_total": total_queries,
                        "query_text": query_text,
                        "query_purpose": query_purpose,
                        "query_result_count": len(query_results),
                        "result_count": len(items),
                        "search_round": int(search_round),
                        "items": self.build_search_result_cards(items),
                    }
                )
        if not items:
            raise RuntimeError("搜索没有返回有效网页来源。请确认该模型已开启实时搜索，或改用博查 Key。")
        return stamp_search_results(items, search_round=search_round)

    def build_search_result_cards(self, search_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cards: list[dict[str, Any]] = []
        for item in search_results:
            cards.append(
                {
                    "id": item["id"],
                    "query_text": item["query_text"],
                    "query_purpose": item.get("query_purpose") or "",
                    "search_rank": item["search_rank"],
                    "title": item["title"],
                    "url": self._normalize_url(item["url"]),
                    "bocha_summary": item.get("bocha_summary") or item.get("snippet") or "",
                    "snippet": item.get("snippet") or item.get("bocha_summary") or "",
                    "content_excerpt_md": item.get("content_excerpt_md") or "",
                    "read_status": item.get("read_status") or "pending",
                    "chunk_status": item.get("chunk_status") or "pending",
                    "source_document_id": item.get("source_document_id"),
                    "source_kind": item.get("source_kind") or "",
                    "image_url": item.get("image_url") or "",
                    "extra_images": item.get("extra_images") or [],
                    **copy_plan_fields(item),
                }
            )
        return cards

    def refresh_search_result_cards(self, search_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cards = self.build_search_result_cards(search_results)
        document_ids = [str(item.get("source_document_id")) for item in cards if item.get("source_document_id")]
        if not document_ids:
            return cards

        documents = {
            item.id: item
            for item in self.session.scalars(select(SourceDocument).where(SourceDocument.id.in_(document_ids)))
        }
        chunk_document_ids = set(
            self.session.scalars(
                select(SourceChunk.source_document_id)
                .where(SourceChunk.source_document_id.in_(document_ids))
                .distinct()
            )
        )

        refreshed_cards: list[dict[str, Any]] = []
        for item in cards:
            refreshed = dict(item)
            source_document_id = refreshed.get("source_document_id")
            if not source_document_id:
                refreshed_cards.append(refreshed)
                continue

            document = documents.get(str(source_document_id))
            if document is None:
                refreshed["source_document_id"] = None
                refreshed["read_status"] = "failed" if refreshed.get("read_status") == "failed" else "pending"
                refreshed["chunk_status"] = "failed" if refreshed.get("read_status") == "failed" else "pending"
                refreshed_cards.append(refreshed)
                continue

            refreshed["title"] = document.title or refreshed["title"]
            if not refreshed.get("content_excerpt_md"):
                refreshed["content_excerpt_md"] = self._clip_excerpt(document.markdown_content, limit=320)
            if refreshed.get("read_status") in {"", "pending", "failed"}:
                refreshed["read_status"] = "ready"
            refreshed["chunk_status"] = "ready" if str(source_document_id) in chunk_document_ids else "pending"
            refreshed_cards.append(refreshed)
        return refreshed_cards

    def retry_search_result_card(
        self,
        *,
        collection: SourceCollection,
        search_result: dict[str, Any],
    ) -> dict[str, Any]:
        candidate = self.build_search_result_cards([search_result])[0]
        normalized_url = self._normalize_url(candidate["url"])
        existing_document = None
        if candidate.get("source_document_id"):
            existing_document = self.session.get(SourceDocument, candidate["source_document_id"])
        if existing_document is None:
            existing_document = self.session.scalar(
                select(SourceDocument).where(
                    SourceDocument.collection_id == collection.id,
                    SourceDocument.source_uri == normalized_url,
                )
            )

        try:
            if existing_document is not None and candidate.get("read_status") != "failed":
                metadata = existing_document.metadata_json if isinstance(existing_document.metadata_json, dict) else {}
                read_result = ReadResult(
                    title=existing_document.title,
                    markdown_content=existing_document.markdown_content,
                    provider=str(metadata.get("provider") or "stored"),
                    metadata=metadata,
                )
            else:
                read_result = self._store_read_result(normalized_url, self.mcp.read_url_markdown(normalized_url))

            result = SearchResult(
                title=candidate["title"],
                url=normalized_url,
                snippet=candidate.get("snippet") or candidate.get("bocha_summary") or "",
            )
            document, chunks, reused_existing = self._upsert_source_document(
                collection,
                result,
                read_result,
                extra_metadata={"search_rank": candidate.get("search_rank")},
                defer_chunks=True,
            )
            if chunks:
                self.store_chunks(
                    [
                        {
                            "document": document,
                            "chunk": chunk,
                        }
                        for chunk in chunks
                    ]
                )
            candidate["title"] = read_result.title or result.title
            candidate["content_excerpt_md"] = self._clip_excerpt(read_result.markdown_content, limit=320)
            candidate["read_status"] = "reused" if reused_existing and candidate.get("read_status") != "failed" else "ready"
            candidate["chunk_status"] = "pending"
            candidate["source_document_id"] = document.id
        except Exception:
            candidate["content_excerpt_md"] = ""
            candidate["read_status"] = "failed"
            candidate["chunk_status"] = "failed"
            candidate["source_document_id"] = None

        return self.refresh_search_result_cards([candidate])[0]

    def get_or_create_page_collection(self, project: Project, page: ProjectPage) -> SourceCollection:
        return self._get_or_create_collection(
            project_id=project.id,
            collection_type="page_knowledge",
            page_id=page.id,
            title=f"{project.title}::{page.page_code} 页级资料池",
        )

    def clear_collection(self, collection: SourceCollection) -> None:
        document_ids = list(
            self.session.scalars(
                select(SourceDocument.id).where(SourceDocument.collection_id == collection.id)
            )
        )
        if document_ids:
            self.session.execute(delete(SourceDocument).where(SourceDocument.id.in_(document_ids)))
        self.session.flush()

    def ingest_search_results(
        self,
        *,
        collection: SourceCollection,
        search_results: list[dict[str, Any]],
        replace: bool = True,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        candidate_sources, pending_chunk_records, read_summary = self.hydrate_search_results(
            collection=collection,
            search_results=search_results,
            replace=replace,
        )
        self.store_chunks(pending_chunk_records)
        for candidate in candidate_sources:
            if candidate.get("source_document_id") and candidate.get("read_status") != "failed":
                candidate["chunk_status"] = "ready"
        failed_urls = read_summary.get("failed_urls") or []
        if not read_summary.get("ingested_count") and failed_urls:
            raise RuntimeError(f"研究来源读取全部失败: {failed_urls[0]}")
        return candidate_sources, self.build_collection_digest(collection.id)

    def ingest_llm_answer(
        self,
        *,
        collection: SourceCollection,
        page: ProjectPage,
        answer: str,
        sources: list[dict[str, Any]],
        query_text: str = "",
        page_title: str = "",
        replace: bool = True,
        search_round: int = 1,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        cleaned = (answer or "").strip()
        if not cleaned:
            raise RuntimeError("搜索模型没有返回整理稿。请确认该模型已开启实时搜索，或改用博查 Key。")
        if replace:
            self.clear_collection(collection)

        existing_answers = self.session.scalar(
            select(func.count(SourceDocument.id)).where(
                SourceDocument.collection_id == collection.id,
                SourceDocument.source_type == "llm_answer",
            )
        ) or 0
        round_no = int(search_round) if search_round else int(existing_answers) + 1
        title = f"整理稿：{(page_title or '').strip() or page.page_code}"
        source_uri = f"llm-search://{page.id}/{round_no}"
        search_result = SearchResult(
            title=title,
            url=source_uri,
            snippet=cleaned[:320],
            provider="llm-search",
        )
        read_result = ReadResult(
            title=title,
            markdown_content=cleaned,
            provider="llm-search",
            metadata={"sources": sources, "search_mode": "llm"},
        )
        document, chunks, _reused = self._upsert_source_document(
            collection,
            search_result,
            read_result,
            extra_metadata={"search_rank": 0},
            defer_chunks=True,
            source_type="llm_answer",
        )
        pending_chunk_records = [
            {"document": document, "chunk": chunk}
            for chunk in chunks
        ]
        digest_card = {
            "id": self._hash_text(f"{query_text}|{source_uri}"),
            "query_text": query_text,
            "query_purpose": "页级整理稿",
            "search_rank": 0,
            "title": title,
            "url": source_uri,
            "bocha_summary": cleaned[:320],
            "snippet": cleaned[:320],
            "content_excerpt_md": self._clip_excerpt(cleaned, limit=320),
            "read_status": "ready",
            "chunk_status": "pending",
            "source_document_id": document.id,
            "source_kind": "llm_answer",
        }
        source_cards: list[dict[str, Any]] = [digest_card]
        seen_urls = {source_uri}
        for index, row in enumerate(sources, start=1):
            url = self._normalize_url(str(row.get("url") or ""))
            if not url or url in seen_urls:
                continue
            if not url.startswith("http://") and not url.startswith("https://"):
                continue
            seen_urls.add(url)
            source_cards.append(
                {
                    "id": self._hash_text(f"{query_text}|{url}"),
                    "query_text": query_text,
                    "query_purpose": str(row.get("snippet") or "信源"),
                    "search_rank": index,
                    "title": str(row.get("title") or url),
                    "url": url,
                    "bocha_summary": str(row.get("snippet") or ""),
                    "snippet": str(row.get("snippet") or ""),
                    "content_excerpt_md": "",
                    "read_status": "pending",
                    "chunk_status": "pending",
                    "source_document_id": None,
                    "source_kind": "llm_source",
                }
            )
        return stamp_search_results(source_cards, search_round=round_no, default_dimension="页级整理稿"), pending_chunk_records

    def search_results_as_evidence(self, search_results: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        for index, raw in enumerate(search_results or [], start=1):
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "").strip()
            snippet = str(
                raw.get("content_excerpt_md") or raw.get("snippet") or raw.get("bocha_summary") or ""
            ).strip()
            url = str(raw.get("url") or "").strip()
            if not url.startswith("http://") and not url.startswith("https://"):
                continue
            if not title and not snippet and not url:
                continue
            evidence.append(
                {
                    "title": title or url,
                    "url": url,
                    "excerpt_md": snippet or url,
                    "rank_no": index,
                    "search_rank": raw.get("search_rank") or index,
                }
            )
        return evidence

    def hydrate_search_results(
        self,
        *,
        collection: SourceCollection,
        search_results: list[dict[str, Any]],
        replace: bool = True,
        on_read_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        if replace:
            self.clear_collection(collection)

        planned_candidates: list[dict[str, Any]] = []
        cached_results: dict[str, ReadResult] = {}
        for item in search_results:
            normalized_url = self._normalize_url(item["url"])
            result = SearchResult(
                title=item["title"],
                url=normalized_url,
                snippet=item.get("bocha_summary") or "",
            )
            candidate = {
                "id": item["id"],
                "query_text": item["query_text"],
                "query_purpose": item.get("query_purpose") or "",
                "search_rank": item["search_rank"],
                "title": item["title"],
                "url": normalized_url,
                "snippet": item.get("bocha_summary") or "",
                "bocha_summary": item.get("bocha_summary") or "",
                "content_excerpt_md": "",
                "read_status": "pending",
                "chunk_status": "pending",
                "source_document_id": None,
                "image_url": item.get("image_url") or "",
                "extra_images": item.get("extra_images") or [],
                **copy_plan_fields(item),
            }
            cached_result = self._get_cached_read_result(normalized_url)
            if cached_result is not None:
                cached_results[normalized_url] = cached_result
            planned_candidates.append(
                {
                    "candidate": candidate,
                    "result": result,
                    "cached": cached_result is not None,
                }
            )

        prefetched_results, fetch_errors = self._fetch_candidate_markdown(planned_candidates, cached_results)
        candidate_sources, pending_chunk_records, ingested_count, failed_urls = self._ingest_candidate_records(
            collection,
            planned_candidates,
            cached_results,
            prefetched_results,
            fetch_errors,
            on_candidate_progress=on_read_progress,
        )
        if ingested_count == 0 and failed_urls:
            raise RuntimeError(f"研究来源读取全部失败: {failed_urls[0]}")
        return candidate_sources, pending_chunk_records, {
            "ingested_count": ingested_count,
            "failed_count": len(failed_urls),
            "failed_urls": failed_urls,
        }

    def store_chunks(
        self,
        chunk_records: list[dict[str, Any]],
        *,
        on_chunk_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        self._store_chunks(chunk_records, on_chunk_progress=on_chunk_progress)
        return {
            "document_count": len({item["document"].id for item in chunk_records}),
            "chunk_count": len(chunk_records),
        }

    def build_collection_digest(self, collection_id: str) -> dict[str, Any]:
        document_count = self.session.scalar(
            select(func.count(SourceDocument.id)).where(SourceDocument.collection_id == collection_id)
        ) or 0
        chunk_count = self.session.scalar(
            select(func.count(SourceChunk.id))
            .join(SourceDocument, SourceDocument.id == SourceChunk.source_document_id)
            .where(SourceDocument.collection_id == collection_id)
        ) or 0
        content_chars = self.session.scalar(
            select(func.coalesce(func.sum(func.length(SourceChunk.content_md)), 0))
            .join(SourceDocument, SourceDocument.id == SourceChunk.source_document_id)
            .where(SourceDocument.collection_id == collection_id)
        ) or 0
        latest_document = self.session.scalars(
            select(SourceDocument)
            .where(SourceDocument.collection_id == collection_id)
            .order_by(SourceDocument.created_at.desc())
            .limit(1)
        ).first()
        return {
            "collection_id": collection_id,
            "document_count": document_count,
            "chunk_count": chunk_count,
            "content_chars": int(content_chars),
            "latest_document_title": latest_document.title if latest_document else "",
            "updated_at": latest_document.created_at.isoformat() if latest_document else None,
        }

    def create_session(
        self,
        *,
        project_id: str,
        page_id: str | None,
        scope_type: str,
        session_role: str,
        research_goal: str,
        query_plan: list[dict[str, str]],
        context_snapshot: dict[str, Any],
        status: str = "running",
    ) -> ResearchSession:
        session = ResearchSession(
            project_id=project_id,
            page_id=page_id,
            scope_type=scope_type,
            session_role=session_role,
            research_goal=research_goal,
            query_plan_json=query_plan,
            context_snapshot_json=context_snapshot,
            status=status,
        )
        self.session.add(session)
        self.session.flush()
        return session

    def retrieve_for_collection(
        self,
        *,
        project: Project,
        collection: SourceCollection,
        research_session: ResearchSession,
        query_plan: list[dict[str, str]],
        limit: int,
        excerpt_limit: int = 420,
    ) -> list[dict[str, Any]]:
        query_terms = [
            str(item.get("query_text") or "").strip()
            for item in query_plan
            if str(item.get("query_text") or "").strip()
        ]
        selected_items = select_evidence(
            self.session,
            collection.id,
            limit=limit,
            query_terms=query_terms or None,
        )
        self.session.execute(
            delete(ProjectResearchSource).where(ProjectResearchSource.research_session_id == research_session.id)
        )
        self.session.execute(delete(ResearchSource).where(ResearchSource.research_session_id == research_session.id))
        self.session.flush()
        if not selected_items:
            research_session.selected_citations_json = []
            return []

        usage_note = ""
        if query_plan:
            first_query = str(query_plan[0].get("query_text") or "").strip()
            if first_query:
                usage_note = f"用于支撑查询：{first_query}"

        selected_payloads: list[dict[str, Any]] = []
        selected_texts: list[str] = []
        for item in selected_items:
            excerpt = self._clip_excerpt(item.chunk.content_md, limit=excerpt_limit)
            if self._is_duplicate_excerpt(excerpt, selected_texts):
                continue
            selected_texts.append(excerpt)
            rank_no = len(selected_payloads) + 1
            relevance = (
                item.relevance_score
                if query_terms
                else float(max(0, 1000 - item.search_rank))
            )
            citation = self._get_or_create_citation(project.id, item.document, item.chunk, excerpt)
            self.session.add(
                ProjectResearchSource(
                    research_session_id=research_session.id,
                    source_document_id=item.document.id,
                    chunk_id=item.chunk.id,
                    rank_no=rank_no,
                    excerpt_md=excerpt,
                    relevance_score=relevance,
                    usage_note=usage_note,
                    is_pinned=False,
                )
            )
            self.session.add(
                ResearchSource(
                    research_session_id=research_session.id,
                    title=item.document.title,
                    url=item.document.source_uri,
                    snippet=excerpt,
                    content_md=item.chunk.content_md,
                )
            )
            selected_payloads.append(
                {
                    "citation_id": citation.id,
                    "source_document_id": item.document.id,
                    "chunk_id": item.chunk.id,
                    "title": item.document.title,
                    "url": item.document.source_uri,
                    "excerpt_md": excerpt,
                    "citation_label": citation.citation_label,
                    "rank_no": rank_no,
                    "relevance_score": relevance,
                    "usage_note": usage_note,
                    "search_rank": item.search_rank,
                    "chunk_index": item.chunk.chunk_index,
                }
            )
            if len(selected_payloads) >= limit:
                break

        research_session.selected_citations_json = selected_payloads
        self.session.flush()
        return selected_payloads

    def serialize_selected_sources(self, research_session_id: str) -> list[dict[str, Any]]:
        stmt = (
            select(ProjectResearchSource)
            .where(ProjectResearchSource.research_session_id == research_session_id)
            .order_by(ProjectResearchSource.rank_no.asc())
            .options(
                selectinload(ProjectResearchSource.source_document),
                selectinload(ProjectResearchSource.chunk),
            )
        )
        return [self._selected_source_payload(item) for item in self.session.scalars(stmt)]

    def _get_or_create_collection(
        self,
        *,
        project_id: str,
        collection_type: str,
        page_id: str | None,
        title: str,
    ) -> SourceCollection:
        stmt = select(SourceCollection).where(
            SourceCollection.project_id == project_id,
            SourceCollection.collection_type == collection_type,
        )
        if page_id is None:
            stmt = stmt.where(SourceCollection.page_id.is_(None))
        else:
            stmt = stmt.where(SourceCollection.page_id == page_id)
        collection = self.session.scalars(stmt).first()
        if collection is None:
            collection = SourceCollection(
                project_id=project_id,
                page_id=page_id,
                collection_type=collection_type,
                title=title,
            )
            self.session.add(collection)
            self.session.flush()
        return collection

    def _search_query(self, query_text: str, limit: int) -> list[SearchResult]:
        query_key = self._query_key(query_text)
        cache = self.session.scalar(select(BochaSearchCache).where(BochaSearchCache.query_key == query_key))
        now = now_utc()
        if cache and is_cache_fresh(cache.expires_at, now):
            items = cache.result_json.get("items", [])
            return [
                SearchResult(
                    title=str(item.get("title") or item.get("url") or ""),
                    url=str(item.get("url") or ""),
                    snippet=str(item.get("snippet") or item.get("bocha_summary") or ""),
                    provider=str(item.get("provider") or "bocha-mcp"),
                    image_url=str(item.get("image_url") or ""),
                    extra_images=tuple(item.get("extra_images") or []) if isinstance(item.get("extra_images"), list) else (),
                )
                for item in items
                if item.get("url")
            ]

        results = self.mcp.search_web(query_text, limit=limit)
        if not results:
            return []
        payload = {"items": [item.__dict__ for item in results]}
        if cache is None:
            cache = BochaSearchCache(
                query_key=query_key,
                query_text=query_text,
                result_json=payload,
                result_count=len(results),
                expires_at=now + timedelta(hours=12),
            )
            self.session.add(cache)
        else:
            cache.query_text = query_text
            cache.result_json = payload
            cache.result_count = len(results)
            cache.expires_at = now + timedelta(hours=12)
        self.session.flush()
        return results

    def _get_cached_read_result(self, url: str) -> ReadResult | None:
        normalized_url = self._normalize_url(url)
        now = now_utc()
        cache = self.session.scalar(select(URLContentCache).where(URLContentCache.normalized_url == normalized_url))
        if cache and cache.status == "ready" and is_cache_fresh(cache.expires_at, now):
            return ReadResult(
                title=cache.title,
                markdown_content=cache.markdown_content,
                provider=cache.provider,
                metadata=cache.metadata_json,
            )
        return None

    def _store_read_result(self, url: str, result: ReadResult) -> ReadResult:
        normalized_url = self._normalize_url(url)
        now = now_utc()
        cache = self.session.scalar(select(URLContentCache).where(URLContentCache.normalized_url == normalized_url))
        content_hash = self._hash_text(result.markdown_content)
        if cache is None:
            cache = URLContentCache(
                normalized_url=normalized_url,
                provider=result.provider,
                title=result.title,
                markdown_content=result.markdown_content,
                metadata_json=result.metadata,
                content_hash=content_hash,
                status="ready",
                expires_at=now + timedelta(days=7),
            )
            self.session.add(cache)
        else:
            cache.provider = result.provider
            cache.title = result.title
            cache.markdown_content = result.markdown_content
            cache.metadata_json = result.metadata
            cache.content_hash = content_hash
            cache.status = "ready"
            cache.expires_at = now + timedelta(days=7)
        self.session.flush()
        return ReadResult(
            title=result.title,
            markdown_content=result.markdown_content,
            provider=result.provider,
            metadata=result.metadata,
        )

    def _fetch_candidate_markdown(
        self,
        planned_candidates: list[dict[str, Any]],
        cached_results: dict[str, ReadResult],
    ) -> tuple[dict[str, ReadResult], dict[str, Exception]]:
        prefetched_results: dict[str, ReadResult] = {}
        fetch_errors: dict[str, Exception] = {}
        urls_to_fetch = [
            item["candidate"]["url"]
            for item in planned_candidates
            if item["candidate"]["url"] not in cached_results
        ]
        if not urls_to_fetch:
            return prefetched_results, fetch_errors
        max_workers = max(1, min(self.mcp.settings.max_research_concurrency, len(urls_to_fetch)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(self.mcp.read_url_markdown, url): url
                for url in urls_to_fetch
            }
            for future in as_completed(future_map):
                url = future_map[future]
                try:
                    prefetched_results[url] = future.result()
                except Exception as exc:
                    fetch_errors[url] = exc
        return prefetched_results, fetch_errors

    def _ingest_candidate_records(
        self,
        collection: SourceCollection,
        planned_candidates: list[dict[str, Any]],
        cached_results: dict[str, ReadResult],
        prefetched_results: dict[str, ReadResult],
        fetch_errors: dict[str, Exception],
        on_candidate_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, list[str]]:
        ingested_count = 0
        failed_urls: list[str] = []
        candidate_sources: list[dict[str, Any]] = []
        pending_chunk_records: list[dict[str, Any]] = []
        total_candidates = len(planned_candidates)
        for item in planned_candidates:
            candidate = item["candidate"]
            result = item["result"]
            normalized_url = candidate["url"]
            try:
                read_result = cached_results.get(normalized_url)
                if read_result is None:
                    if normalized_url in fetch_errors:
                        raise fetch_errors[normalized_url]
                    prefetched = prefetched_results.get(normalized_url)
                    if prefetched is None:
                        raise RuntimeError("markdown prefetch missing result")
                    read_result = self._store_read_result(normalized_url, prefetched)
                document, chunks, reused_existing = self._upsert_source_document(
                    collection,
                    result,
                    read_result,
                    extra_metadata={"search_rank": candidate.get("search_rank")},
                    defer_chunks=True,
                )
                candidate["title"] = read_result.title or result.title
                candidate["content_excerpt_md"] = self._clip_excerpt(read_result.markdown_content, limit=320)
                candidate["read_status"] = "reused" if item["cached"] or reused_existing else "ready"
                candidate["source_document_id"] = document.id
                candidate_sources.append(candidate)
                if chunks:
                    pending_chunk_records.extend(
                        {
                            "document": document,
                            "chunk": chunk,
                        }
                        for chunk in chunks
                    )
                ingested_count += 1
            except Exception as exc:
                candidate["read_status"] = "failed"
                candidate["chunk_status"] = "failed"
                candidate_sources.append(candidate)
                failed_urls.append(f"{normalized_url}: {exc}")
            if on_candidate_progress is not None:
                on_candidate_progress(
                    {
                        "completed": len(candidate_sources),
                        "total": total_candidates,
                        "ingested_count": ingested_count,
                        "failed_count": len(failed_urls),
                        "candidate_sources": self.build_search_result_cards(candidate_sources),
                        "latest_candidate": candidate.copy(),
                    }
                )
        return candidate_sources, pending_chunk_records, ingested_count, failed_urls

    def _upsert_source_document(
        self,
        collection: SourceCollection,
        search_result: SearchResult,
        read_result: ReadResult,
        *,
        extra_metadata: dict[str, Any] | None = None,
        defer_chunks: bool = False,
        source_type: str = "url",
    ) -> tuple[SourceDocument, list[dict[str, Any]], bool]:
        normalized_url = self._normalize_url(search_result.url)
        cache = self.session.scalar(select(URLContentCache).where(URLContentCache.normalized_url == normalized_url))
        document = self.session.scalar(
            select(SourceDocument).where(
                SourceDocument.collection_id == collection.id,
                SourceDocument.source_uri == normalized_url,
            )
        )
        content_hash = self._hash_text(read_result.markdown_content)
        metadata_json = {
            "provider": read_result.provider,
            "snippet": search_result.snippet,
            **read_result.metadata,
            **(extra_metadata or {}),
        }
        if document is None:
            document = SourceDocument(
                collection_id=collection.id,
                source_type=source_type,
                source_uri=normalized_url,
                url_cache_id=cache.id if cache else None,
                title=read_result.title or search_result.title,
                markdown_content=read_result.markdown_content,
                metadata_json=metadata_json,
                content_hash=content_hash,
                status="ready",
            )
            self.session.add(document)
            self.session.flush()
            reused_existing = False
        elif document.content_hash == content_hash and self.session.scalar(
            select(SourceChunk.id).where(SourceChunk.source_document_id == document.id).limit(1)
        ):
            merged = dict(document.metadata_json or {})
            merged.update(extra_metadata or {})
            document.metadata_json = merged
            return document, [], True
        else:
            document.url_cache_id = cache.id if cache else document.url_cache_id
            document.source_type = source_type or document.source_type
            document.title = read_result.title or search_result.title
            document.markdown_content = read_result.markdown_content
            document.metadata_json = metadata_json
            document.content_hash = content_hash
            document.status = "ready"
            self.session.execute(delete(SourceChunk).where(SourceChunk.source_document_id == document.id))
            self.session.flush()
            reused_existing = False

        chunks = self._chunk_markdown(document.title, document.markdown_content)
        if defer_chunks:
            return document, chunks, reused_existing
        self._store_chunks(
            [
                {
                    "document": document,
                    "chunk": item,
                }
                for item in chunks
            ]
        )
        return document, [], reused_existing

    def _store_chunks(
        self,
        chunk_records: list[dict[str, Any]],
        *,
        on_chunk_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if not chunk_records:
            return
        total_chunks = len(chunk_records)
        total_documents = len({item["document"].id for item in chunk_records})
        for index, item in enumerate(chunk_records):
            document = item["document"]
            chunk = item["chunk"]
            self.session.add(
                SourceChunk(
                    source_document_id=document.id,
                    chunk_index=chunk["chunk_index"],
                    section_path=chunk["section_path"],
                    content_md=chunk["content_md"],
                    content_for_match=chunk["content_for_match"],
                    token_count=chunk["token_count"],
                )
            )
            if on_chunk_progress is not None:
                on_chunk_progress(
                    {
                        "completed_chunks": index + 1,
                        "total_chunks": total_chunks,
                        "document_count": total_documents,
                    }
                )
        self.session.flush()

    def _chunk_markdown(self, title: str, markdown: str) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        current_section = title or "正文"
        buffer: list[str] = []

        def flush() -> None:
            nonlocal buffer
            content = "\n".join(line for line in buffer if line.strip()).strip()
            if not content:
                buffer = []
                return
            content_for_match = f"{title}\n{current_section}\n{content}".strip()
            chunks.append(
                {
                    "chunk_index": len(chunks),
                    "section_path": current_section,
                    "content_md": content[:4000],
                    "content_for_match": content_for_match[:5000],
                    "token_count": self._estimate_token_count(content_for_match),
                }
            )
            buffer = []

        for raw_line in markdown.splitlines():
            line = raw_line.strip()
            if not line:
                flush()
                continue
            if re.match(r"^#{1,6}\s+", line):
                flush()
                current_section = line.lstrip("#").strip() or current_section
                continue
            buffer.append(line)
            if len("\n".join(buffer)) > 800:
                flush()
        flush()

        if not chunks:
            content = markdown.strip()[:4000]
            chunks.append(
                {
                    "chunk_index": 0,
                    "section_path": title or "正文",
                    "content_md": content,
                    "content_for_match": f"{title}\n{content}".strip()[:5000],
                    "token_count": self._estimate_token_count(content),
                }
            )
        return chunks

    def _get_or_create_citation(
        self,
        project_id: str,
        document: SourceDocument,
        chunk: SourceChunk,
        excerpt_md: str,
    ) -> Citation:
        citation = self.session.scalar(
            select(Citation).where(Citation.project_id == project_id, Citation.chunk_id == chunk.id)
        )
        if citation is None:
            citation = Citation(
                project_id=project_id,
                source_document_id=document.id,
                chunk_id=chunk.id,
                title=document.title,
                url=document.source_uri,
                excerpt_md=excerpt_md,
                citation_label=self._build_citation_label(document.title, document.source_uri),
            )
            self.session.add(citation)
            self.session.flush()
        return citation

    def _selected_source_payload(self, item: ProjectResearchSource) -> dict[str, Any]:
        document = item.source_document
        chunk = item.chunk
        return {
            "source_document_id": item.source_document_id,
            "chunk_id": item.chunk_id,
            "title": document.title if document else "",
            "url": document.source_uri if document else "",
            "excerpt_md": item.excerpt_md,
            "content_md": chunk.content_md if chunk else "",
            "rank_no": item.rank_no,
            "relevance_score": item.relevance_score,
            "usage_note": item.usage_note,
        }

    def _normalize_query_items(self, payload: Any) -> list[dict[str, str]]:
        return normalize_query_plan(payload)

    def _build_citation_label(self, title: str, url: str) -> str:
        return (title or url)[:60]

    def _query_key(self, query: str) -> str:
        normalized = re.sub(r"\s+", " ", query).strip().lower()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _normalize_url(self, url: str) -> str:
        cleaned, _ = urldefrag(url.strip())
        return cleaned

    def _hash_text(self, text: str) -> str:
        return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()

    def _estimate_token_count(self, text: str) -> int:
        return estimate_tokens(text)

    def _keyword_score(self, query: str, text: str) -> float:
        return keyword_score(query, text)

    def _tokenize(self, text: str) -> list[str]:
        return tokenize(text)

    def _clip_excerpt(self, content: str, limit: int = 220) -> str:
        cleaned = re.sub(r"\s+", " ", content).strip()
        return cleaned[:limit]

    def _is_duplicate_excerpt(self, excerpt: str, existing: list[str]) -> bool:
        current_tokens = set(self._tokenize(excerpt))
        if not current_tokens:
            return False
        for item in existing:
            tokens = set(self._tokenize(item))
            if not tokens:
                continue
            jaccard = len(current_tokens & tokens) / len(current_tokens | tokens)
            if jaccard >= 0.8:
                return True
        return False
