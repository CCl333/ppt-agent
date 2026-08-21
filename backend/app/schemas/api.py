from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ProjectCreateRequest(BaseModel):
    title: str | None = None
    request_text: str = Field(min_length=3)


class MessageCreateRequest(BaseModel):
    scope_type: str = "project"
    target_page_id: str | None = None
    ui_surface: str = "init"
    content_md: str = Field(min_length=1)
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class RequirementAnswer(BaseModel):
    question_code: str
    value: Any


class RequirementAnswersBatchRequest(BaseModel):
    answers: list[RequirementAnswer]


class RequirementAnswerPatchRequest(BaseModel):
    value: Any


class RequirementOptionInput(BaseModel):
    option_code: str
    label: str
    description: str | None = None
    value: Any | None = None


class RequirementQuestionCreateRequest(BaseModel):
    question_code: str
    label: str
    description: str | None = None
    options: list[RequirementOptionInput] = Field(default_factory=list)
    allow_custom: bool = True


class RequirementQuestionPatchRequest(BaseModel):
    label: str | None = None
    description: str | None = None
    options: list[RequirementOptionInput] | None = None
    allow_custom: bool | None = None


class ConfirmRequest(BaseModel):
    note_md: str | None = None


class PageOutlinePatchRequest(BaseModel):
    title: str = Field(min_length=1)
    content_outline: list[str] = Field(default_factory=list)
    section_title: str | None = None


class SummaryPatchRequest(BaseModel):
    summary_md: str = Field(min_length=1)


class DraftPatchRequest(BaseModel):
    svg_markup: str = Field(min_length=1)


class StoryboardPagePatchRequest(BaseModel):
    page_id: str | None = None
    title: str = Field(min_length=1)
    content_outline: list[str] = Field(default_factory=list)


class StoryboardSectionPatchRequest(BaseModel):
    part_title: str = Field(min_length=1)
    pages: list[StoryboardPagePatchRequest] = Field(default_factory=list)


class StoryboardPatchRequest(BaseModel):
    parts: list[StoryboardSectionPatchRequest] = Field(default_factory=list)


class PageActionRequest(BaseModel):
    action_type: str
    replace_existing: bool = True


class BatchActionRequest(BaseModel):
    action_type: str


class ExportCreateRequest(BaseModel):
    export_format: str = "pptx"


class StyleCardConfirmRequest(BaseModel):
    style_id: str = Field(min_length=1)
    source: str = "any"


class StyleCardSaveRequest(BaseModel):
    style_id: str | None = None


class CancelTasksRequest(BaseModel):
    page_id: str | None = None


class ModelProviderCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_path: str = "/chat/completions"
    timeout_seconds: int = Field(default=120, ge=5, le=600)


class ModelProviderPatchRequest(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    api_path: str | None = None
    timeout_seconds: int | None = Field(default=None, ge=5, le=600)


class ExpertBindingsPayload(BaseModel):
    search: str | None = None
    content: str | None = None
    draft: str | None = None
    design: str | None = None
    context: str | None = None
    svg: str | None = None


class ModelBindingsPutRequest(BaseModel):
    context: str | None = None
    svg: str | None = None
    search: str | None = None
    content: str | None = None
    draft: str | None = None
    design: str | None = None
    expert_enabled: bool | None = None
    expert: ExpertBindingsPayload | None = None


class SearchSettingsPutRequest(BaseModel):
    mode: str | None = None
    bocha_auth_header: str | None = None
    tavily_api_key: str | None = None
    tavily_api_url: str | None = None


class ReaderSettingsPutRequest(BaseModel):
    mode: str | None = None
    tavily_api_key: str | None = None
    tavily_api_url: str | None = None
    firecrawl_api_key: str | None = None
    firecrawl_api_url: str | None = None


class ModelCatalogRequest(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    provider_id: str | None = None
    api_path: str = "/chat/completions"
    timeout_seconds: int = Field(default=30, ge=5, le=120)
