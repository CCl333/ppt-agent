from __future__ import annotations

from app.models.entities import (
    DesignVersion,
    DraftVersion,
    PageBriefVersion,
    Project,
    ProjectPage,
    RequirementForm,
    SourceChunk,
    SourceDocument,
)
from app.services.research import ResearchService

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def make_project(
    session,
    *,
    stage: str = "init",
    page_count_target: int | None = 8,
    style_preset: str | None = "minimalism",
    answers: dict | None = None,
    questions: list | None = None,
    document_count: int = 3,
) -> Project:
    project = Project(
        title="测试项目",
        request_text="用于不变量测试的需求描述",
        current_stage=stage,
        page_count_target=page_count_target,
        style_preset=style_preset,
        workflow_constraints_json={"items": []},
    )
    session.add(project)
    session.flush()
    form = RequirementForm(
        project_id=project.id,
        status="pending_confirmation",
        answers_json=answers
        if answers is not None
        else {
            "page_count_target": page_count_target,
            "style_preset": style_preset,
            "q1": "已回答",
        },
        ai_questions_json=questions if questions is not None else [{"question_code": "q1", "label": "补充问题"}],
        init_corpus_digest_json={"document_count": document_count},
        init_search_results_json=(
            [
                {
                    "id": "init-src",
                    "query_text": "fixture",
                    "query_purpose": "fixture",
                    "search_rank": 1,
                    "title": "初始化摘要",
                    "url": "https://example.com/init",
                    "bocha_summary": "fixture summary",
                    "snippet": "fixture summary",
                }
            ]
            if document_count
            else []
        ),
        fixed_items_json={},
    )
    session.add(form)
    session.commit()
    session.refresh(project)
    return project


def make_content_page(
    session,
    project: Project,
    *,
    page_code: str,
    title: str,
    sort_order: int,
    summary_md: str = "页摘要",
    with_draft: bool = True,
    with_design: bool = True,
) -> ProjectPage:
    page = ProjectPage(
        project_id=project.id,
        page_code=page_code,
        page_role="content",
        part_title="章节一",
        sort_order=sort_order,
        outline_status="ready",
        search_status="ready",
        summary_status="ready" if summary_md else "empty",
        draft_status="ready" if with_draft else "empty",
        design_status="ready" if with_design else "empty",
        page_summary_md=summary_md,
        page_search_results_json=[
            {
                "id": f"{page_code}-src",
                "query_text": title,
                "query_purpose": "fixture",
                "search_rank": 1,
                "title": title,
                "url": f"https://example.com/{page_code}",
                "bocha_summary": "fixture summary",
                "snippet": "fixture summary",
                "read_status": "ready",
                "chunk_status": "ready",
            }
        ],
        page_corpus_digest_json={"document_count": 1},
        artifact_staleness_json={},
    )
    session.add(page)
    session.flush()
    brief = PageBriefVersion(
        project_id=project.id,
        page_id=page.id,
        version_no=1,
        status="ready",
        section_title="章节一",
        title=title,
        content_outline_json=["要点一"],
        content_summary="要点一",
    )
    session.add(brief)
    session.flush()
    page.current_brief_version_id = brief.id
    if with_draft:
        draft = DraftVersion(
            project_id=project.id,
            page_id=page.id,
            version_no=1,
            status="ready",
            draft_svg_markup='<svg viewBox="0 0 1280 720"></svg>',
        )
        session.add(draft)
        session.flush()
        page.current_draft_version_id = draft.id
    if with_design:
        design = DesignVersion(
            project_id=project.id,
            page_id=page.id,
            version_no=1,
            status="ready",
            style_pack_id="minimalism",
            design_svg_markup='<svg viewBox="0 0 1280 720"></svg>',
        )
        session.add(design)
        session.flush()
        page.current_design_version_id = design.id
    session.commit()
    session.refresh(page)
    return page


def add_page_chunk(
    session,
    project: Project,
    page: ProjectPage,
    *,
    uri: str,
    title: str,
    content: str,
) -> SourceChunk:
    collection = ResearchService(session).get_or_create_page_collection(project, page)
    document = SourceDocument(
        collection_id=collection.id,
        source_type="url",
        source_uri=uri,
        title=title,
        metadata_json={"search_rank": 1},
        markdown_content=content,
        status="ready",
    )
    session.add(document)
    session.flush()
    chunk = SourceChunk(
        source_document_id=document.id,
        chunk_index=0,
        content_md=content,
        content_for_match=content,
        token_count=len(content),
    )
    session.add(chunk)
    session.commit()
    session.refresh(chunk)
    return chunk
