export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

export type ProjectStage = 'init' | 'outline' | 'search' | 'draft' | 'design' | 'export';
export type ScopeType = 'project' | 'page';
export type UiSurface = 'init' | 'outline' | 'search' | 'draft' | 'design';
export type PreviewSurface = 'design' | 'draft' | 'document' | 'fallback';

export interface WorkflowConstraint {
  code: string;
  label: string;
  detail: string;
}

export interface StyleOption {
  style_id: string;
  style_name: string;
  description: string;
  palette: Record<string, string>;
}

export interface StyleCard {
  style_id: string;
  name: string;
  name_en: string;
  rationale: string;
  tags: string[];
  palette: Record<string, string>;
  colors: string[];
  source: string;
  texture?: string;
  fixed_chrome?: string[];
}

export interface StyleCardState {
  project_id: string;
  frozen: StyleCard | null;
  candidates: StyleCard[];
  library: StyleCard[];
  saved?: StyleCard;
}

export interface ProjectSummary {
  project_id: string;
  title: string;
  request_text: string;
  current_stage: ProjectStage;
  page_count_target: number | null;
  style_preset: string | null;
  style_card?: StyleCard | null;
  background_asset_path: string | null;
  workflow_constraints: WorkflowConstraint[];
  page_count: number;
  preview_surface?: PreviewSurface;
  preview_svg_markup?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProjectListResponse {
  items: ProjectSummary[];
}

export interface RequirementOption {
  option_code: string;
  label: string;
  page_count?: number;
  reason?: string;
}

export interface RequirementQuestionOption {
  option_code: string;
  label: string;
  description?: string;
  value?: string | number;
}

export interface RequirementQuestion {
  question_code: string;
  label: string;
  description?: string;
  options: RequirementQuestionOption[];
  allow_custom: boolean;
}

export interface InitSearchQuery {
  query_text: string;
  query_purpose: string;
  dimension?: string;
  dimension_id?: string;
}

export interface InitSearchResult {
  id: string;
  query_text: string;
  query_purpose: string;
  search_rank: number;
  round?: number;
  dimension?: string;
  dimension_id?: string;
  title: string;
  url: string;
  bocha_summary: string;
  snippet?: string;
  content_excerpt_md?: string;
  read_status?: string;
  chunk_status?: string;
  source_document_id?: string | null;
  source_kind?: string;
  image_url?: string;
}

export interface CorpusDigest {
  collection_id?: string;
  document_count: number;
  chunk_count: number;
  content_chars?: number;
  latest_document_title?: string;
  updated_at?: string | null;
}

export interface RequirementFormData {
  project_id: string;
  status: string;
  workflow_constraints: WorkflowConstraint[];
  init_search_queries: InitSearchQuery[];
  init_search_results: InitSearchResult[];
  init_corpus_digest: CorpusDigest;
  page_count_options: RequirementOption[];
  fixed_items: {
    page_count: {
      question_code: string;
      allow_custom: boolean;
    };
    style_preset: {
      question_code: string;
      options: StyleOption[];
      allow_custom?: boolean;
    };
    background_asset: {
      question_code: string;
      allow_upload: boolean;
      required: boolean;
    };
  };
  ai_questions: RequirementQuestion[];
  answers: Record<string, string | number>;
  suggested_actions: Array<{
    code: string;
    label: string;
    reason: string;
  }>;
}

export interface RequirementFormResponse {
  requirement_form: RequirementFormData;
}

export interface PageSearchQuery {
  query_text: string;
  query_purpose: string;
  dimension?: string;
  dimension_id?: string;
}

export interface Citation {
  citation_id?: string;
  source_document_id?: string;
  chunk_id?: string;
  title: string;
  url: string;
  excerpt_md?: string;
  citation_label?: string;
  rank_no?: number;
  relevance_score?: number;
  usage_note?: string;
}

export interface PageSearchResult {
  id: string;
  query_text: string;
  query_purpose?: string;
  search_rank: number;
  round?: number;
  dimension?: string;
  dimension_id?: string;
  title: string;
  url: string;
  snippet?: string;
  content_excerpt_md?: string;
  read_status?: string;
  chunk_status?: string;
  source_document_id?: string | null;
  source_kind?: string;
  image_url?: string;
}

export interface SearchCoverageDimension {
  dimension_id: string;
  name: string;
  query_count: number;
  hit_count?: number;
}

export interface SearchCoverage {
  dimension_count: number;
  query_count: number;
  result_count: number;
  rounds: number[];
  latest_round: number;
  latest_round_hits: number;
  dimensions: SearchCoverageDimension[];
}

export interface PageImage {
  image_id: string;
  caption: string;
  source_title: string;
  source_url: string;
  available: string;
}

export interface PageSummary {
  page_id: string;
  project_id: string;
  page_code: string;
  page_role: string;
  part_id: string | null;
  part_title: string | null;
  sort_order: number;
  title: string;
  content_outline: string[];
  outline_status: string;
  search_status: string;
  summary_status: string;
  draft_status: string;
  design_status: string;
  page_search_queries: PageSearchQuery[];
  page_search_results: PageSearchResult[];
  page_images?: PageImage[];
  search_coverage?: SearchCoverage;
  page_corpus_digest: CorpusDigest;
  page_summary_md: string;
  page_summary_citations: Citation[];
  current_artifact_staleness: Record<string, boolean>;
  current_brief_version_id: string | null;
  current_draft_version_id: string | null;
  current_design_version_id: string | null;
  draft_status_reason?: string | null;
  design_status_reason?: string | null;
  draft_retryability?: string | null;
  design_retryability?: string | null;
  draft_svg_hash?: string | null;
  design_svg_hash?: string | null;
  draft_preview_svg_markup?: string | null;
  design_preview_svg_markup?: string | null;
  preview_surface?: PreviewSurface;
  preview_svg_markup?: string | null;
  created_at: string;
  updated_at: string;
  draft?: DraftVersion | null;
  design?: DesignVersion | null;
}

export interface PageListResponse {
  items: PageSummary[];
}

export interface OutlineResponse {
  outline_version_id: string;
  project_id: string;
  version_no: number;
  status: string;
  outline: {
    ppt_outline: {
      cover: {
        title: string;
        sub_title: string;
        content: string[];
      };
      table_of_contents: {
        title: string;
        content: string[];
      };
      parts: Array<{
        part_id?: string;
        part_title: string;
        section_page?: {
          enabled?: boolean;
          page_id?: string;
          title?: string;
          subtitle?: string;
          preview_items?: string[];
          visual_intent?: string;
        };
        pages: Array<{
          title: string;
          content: string[];
        }>;
      }>;
      end_page: {
        title: string;
        content: string[];
      };
    };
  };
  created_at: string;
  updated_at: string;
  storyboard?: StoryboardTree;
}

export interface StoryboardPageRef {
  page_id: string;
  page_role: string;
  title: string;
  part_id?: string | null;
  part_title?: string | null;
}

export interface StoryboardTree {
  fixed_prefix: StoryboardPageRef[];
  sections: Array<{
    part_id: string;
    part_title: string;
    section_page: StoryboardPageRef | null;
    content_pages: StoryboardPageRef[];
  }>;
  fixed_suffix: StoryboardPageRef[];
  play_order: string[];
  composition?: {
    cover: number;
    toc: number;
    section: number;
    content: number;
    end: number;
    total: number;
    label: string;
  };
}

export interface StoryboardPatchRequest {
  parts: Array<{
    part_id?: string | null;
    part_title: string;
    section_page?: {
      enabled?: boolean | null;
      page_id?: string | null;
      title?: string | null;
      subtitle?: string | null;
      preview_items?: string[];
      visual_intent?: string | null;
    } | null;
    pages: Array<{
      page_id?: string | null;
      title: string;
      content_outline: string[];
    }>;
  }>;
}

export interface StoryboardPatchResponse {
  items: PageSummary[];
  outline: OutlineResponse;
  storyboard?: StoryboardTree;
}

export interface LayoutBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface SceneNode {
  node_id: string;
  kind: string;
  role?: string | null;
  box?: LayoutBox | null;
  children?: string[];
  text?: string;
  text_ref?: string;
  visual_slot_id?: string;
}

export interface LayoutPlan {
  schema_version?: string;
  canvas?: {width: number; height: number};
  safe_area?: LayoutBox;
  nodes?: SceneNode[];
  reading_order?: string[];
  layout_replanned?: boolean;
}

export interface VisualSlot {
  slot_id: string;
  kind: string;
  priority?: string;
  status?: string;
  intent?: string;
  asset_id?: string;
  box?: LayoutBox;
}

export interface ScenePatchRequest {
  base_version_id: string;
  text_edits?: Array<{node_id: string; text: string}>;
  box_edits?: Array<{node_id: string; box: LayoutBox}>;
  slot_visibility?: Array<{slot_id: string; visible: boolean}>;
}

export interface DraftVersion {
  draft_version_id: string;
  project_id: string;
  page_id: string;
  version_no: number;
  status: string;
  page_brief_version_id: string | null;
  research_session_id: string | null;
  draft_svg_markup: string;
  content_plan_json?: Record<string, unknown>;
  visual_plan_json?: Record<string, unknown>;
  layout_plan_json?: Record<string, unknown>;
  quality_report?: Record<string, unknown>;
  svg_hash?: string | null;
  status_reason?: string | null;
  retryability?: string | null;
  contract_version?: string | null;
  created_at: string;
  updated_at: string;
}

export interface DesignVersion {
  design_version_id: string;
  project_id: string;
  page_id: string;
  version_no: number;
  status: string;
  draft_version_id: string | null;
  style_pack_id: string | null;
  background_asset_path: string | null;
  design_svg_markup: string;
  visual_plan_json?: Record<string, unknown>;
  layout_plan_json?: Record<string, unknown>;
  quality_report?: Record<string, unknown>;
  export_preflight?: Record<string, unknown>;
  svg_hash?: string | null;
  status_reason?: string | null;
  retryability?: string | null;
  contract_version?: string | null;
  style_pack: StyleOption;
  created_at: string;
  updated_at: string;
}

export interface FontReport {
  fonts: Array<{
    name: string;
    license_id: string;
    action: string;
    embed: boolean;
    notice?: string | null;
    note?: string;
  }>;
  notices: string[];
  install_required: boolean;
}

export interface ExportJob {
  export_id: string;
  project_id: string;
  export_format: string;
  render_mode?: string | null;
  status: string;
  phase?: string;
  progress?: {current?: number; total?: number; phase?: string};
  input_hash?: string | null;
  input_stale?: boolean;
  file_path: string;
  file_sha256?: string | null;
  file_size?: number | null;
  slide_count?: number | null;
  manifest?: Record<string, unknown>;
  font_report?: FontReport;
  error_code?: string | null;
  error_detail?: Record<string, unknown>;
  download_ready?: boolean;
  created_at: string;
  updated_at: string;
}

export interface QualityEvalEstimate {
  mode: string;
  scope: string;
  scope_capped: boolean;
  page_count: number;
  page_ids: string[];
  estimated_calls: number;
  estimated_tokens: number;
  estimated_cost: number | null;
  estimated_duration_s: number;
  pairwise_pairs: number;
  require_confirmation: boolean;
  vlm_required: boolean;
}

export interface QualityEvalJob {
  eval_id: string;
  project_id: string;
  mode: string;
  scope: string;
  status: string;
  phase?: string;
  page_ids: string[];
  estimated_tokens: number;
  actual_input_tokens: number;
  actual_output_tokens: number;
  sample_count: number;
  completed_count: number;
  progress?: {current?: number; total?: number; phase?: string};
  input_stale?: boolean;
  report?: {
    pages?: Array<{
      page_id: string;
      hard_fail?: boolean;
      tracks?: {
        verdict?: string;
        subjective_score?: number | null;
        raw_subjective_score?: number | null;
        ready_eligible?: boolean;
      };
    }>;
  };
  error_code?: string | null;
  error_detail?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface BatchRun {
  batch_run_id: string;
  agent_run_id: string | null;
  action_type: string;
  status: string;
  expected_count: number;
  queued_count: number;
  skipped_count: number;
  running_count: number;
  success_count: number;
  failed_count: number;
  canceled_count: number;
  failure_summary: Record<string, unknown>;
  retry_reason?: string | null;
  parent_batch_run_id?: string | null;
  task_ids: string[];
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface ProjectMessage {
  id: string;
  project_id: string;
  stage: ProjectStage;
  scope_type: ScopeType;
  target_page_id: string | null;
  role: 'user' | 'assistant';
  content_md: string;
  structured_payload_json: Record<string, unknown>;
  created_at: string;
}

export interface MessageListResponse {
  items: ProjectMessage[];
}

export interface ActionJobResponse {
  status: string;
  agent_run_id: string;
}

export type ModelRole = 'search' | 'content' | 'draft' | 'design';
export type SearchMode = 'bocha' | 'llm' | 'tavily';
export type ReaderMode = 'tavily' | 'firecrawl' | 'web_fetch';

export interface ModelProvider {
  provider_id: string;
  name: string;
  base_url: string;
  model: string;
  api_path: string;
  timeout_seconds: number;
  api_key_masked: string;
  created_at: string;
  updated_at: string;
}

export interface ModelBindingItem {
  role: ModelRole;
  label: string;
  hint?: string;
  provider_id: string | null;
  provider: ModelProvider | null;
}

export interface ModelBindingsResponse {
  items: ModelBindingItem[];
  needs_setup: boolean;
  expert_enabled: boolean;
  expert_items: ModelBindingItem[];
}

export interface SearchSettings {
  mode: SearchMode;
  bocha_configured: boolean;
  bocha_auth_header_masked: string;
  bocha_from_env: boolean;
  tavily_configured: boolean;
  tavily_api_key_masked: string;
  tavily_api_url: string;
  tavily_from_env: boolean;
}

export interface ReaderSettings {
  mode: ReaderMode;
  tavily_configured: boolean;
  tavily_api_key_masked: string;
  tavily_api_url: string;
  tavily_from_env: boolean;
  firecrawl_configured: boolean;
  firecrawl_api_key_masked: string;
  firecrawl_api_url: string;
  firecrawl_from_env: boolean;
  ready: boolean;
}

export interface ModelProviderPayload {
  name: string;
  base_url: string;
  api_key?: string;
  model: string;
  api_path?: string;
  timeout_seconds?: number;
}

export interface ModelTestResult {
  ok: boolean;
  latency_ms: number;
  model: string;
  detail?: string;
}

export interface ModelCatalogPayload {
  base_url?: string;
  api_key?: string;
  provider_id?: string;
  api_path?: string;
}

export interface ProjectEvent {
  stream_id: number;
  event_id: string;
  event_type: string;
  project_id: string;
  stage: string;
  scope_type: ScopeType;
  target_page_id: string | null;
  agent_run_id: string | null;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface EventListResponse {
  items: ProjectEvent[];
}

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '/api/v1').replace(/\/$/, '');

function buildUrl(path: string): string {
  const cleanPath = path.startsWith('/') ? path : `/${path}`;
  return `${API_BASE}${cleanPath}`;
}

function apiDetailMessage(payload: unknown): string {
  if (!payload || typeof payload !== 'object') {
    return '';
  }
  const detail = (payload as {detail?: unknown}).detail;
  if (typeof detail === 'string') {
    return detail;
  }
  if (detail && typeof detail === 'object') {
    const item = detail as {message?: unknown; error_code?: unknown};
    if (typeof item.message === 'string' && item.message.trim()) {
      return item.message;
    }
    if (typeof item.error_code === 'string' && item.error_code.trim()) {
      return item.error_code;
    }
  }
  return JSON.stringify(payload);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (!(init?.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }

  const response = await fetch(buildUrl(path), {
    ...init,
    headers,
  });

  if (!response.ok) {
    let message = response.statusText;
    try {
      const payload = await response.json();
      message = apiDetailMessage(payload) || response.statusText;
    } catch {
      message = response.statusText;
    }
    throw new ApiError(message, response.status);
  }

  const contentType = response.headers.get('content-type') ?? '';
  if (contentType.includes('application/json')) {
    return response.json() as Promise<T>;
  }

  return response.text() as Promise<T>;
}

export async function listProjects(limit = 20): Promise<ProjectListResponse> {
  return request<ProjectListResponse>(`/projects?limit=${limit}`);
}

export async function createProject(requestText: string, title?: string): Promise<ProjectSummary> {
  return request<ProjectSummary>('/projects', {
    method: 'POST',
    body: JSON.stringify({
      request_text: requestText,
      title,
    }),
  });
}

export async function deleteProject(projectId: string): Promise<{status: string; project_id: string}> {
  return request<{status: string; project_id: string}>(`/projects/${projectId}`, {
    method: 'DELETE',
  });
}

export async function getProject(projectId: string): Promise<ProjectSummary> {
  return request<ProjectSummary>(`/projects/${projectId}`);
}

export async function retryBootstrap(projectId: string): Promise<ProjectSummary> {
  return request<ProjectSummary>(`/projects/${projectId}/bootstrap:retry`, {
    method: 'POST',
  });
}

export async function listMessages(projectId: string): Promise<MessageListResponse> {
  return request<MessageListResponse>(`/projects/${projectId}/messages`);
}

export async function listProjectEvents(
  projectId: string,
  options?: {afterId?: number; limit?: number},
): Promise<EventListResponse> {
  const params = new URLSearchParams();
  if (options?.afterId) {
    params.set('after_id', String(options.afterId));
  }
  if (options?.limit) {
    params.set('limit', String(options.limit));
  }
  const query = params.toString();
  return request<EventListResponse>(`/projects/${projectId}/events${query ? `?${query}` : ''}`);
}

export async function createMessage(
  projectId: string,
  payload: {
    scope_type: ScopeType;
    target_page_id: string | null;
    ui_surface: UiSurface;
    content_md: string;
    attachments?: Array<Record<string, unknown>>;
  },
): Promise<ProjectMessage> {
  return request<ProjectMessage>(`/projects/${projectId}/messages`, {
    method: 'POST',
    body: JSON.stringify({
      ...payload,
      attachments: payload.attachments ?? [],
    }),
  });
}

export async function getRequirementForm(projectId: string): Promise<RequirementFormResponse> {
  return request<RequirementFormResponse>(`/projects/${projectId}/requirements/form`);
}

export async function submitRequirementAnswers(
  projectId: string,
  answers: Array<{question_code: string; value: string | number}>,
): Promise<RequirementFormResponse> {
  return request<RequirementFormResponse>(`/projects/${projectId}/requirements/answers:batch`, {
    method: 'POST',
    body: JSON.stringify({answers}),
  });
}

export async function retryInitSearchResult(projectId: string, sourceId: string): Promise<RequirementFormResponse> {
  return request<RequirementFormResponse>(`/projects/${projectId}/requirements/search-results/${sourceId}:retry`, {
    method: 'POST',
  });
}

export async function confirmRequirements(projectId: string, noteMd?: string): Promise<ProjectSummary> {
  return request<ProjectSummary>(`/projects/${projectId}/requirements/confirm`, {
    method: 'POST',
    body: JSON.stringify({note_md: noteMd}),
  });
}

export async function uploadBackground(projectId: string, file: File): Promise<{background_asset_path: string}> {
  const formData = new FormData();
  formData.append('file', file);
  return request<{background_asset_path: string}>(`/projects/${projectId}/assets/backgrounds`, {
    method: 'POST',
    body: formData,
  });
}

export async function confirmOutline(projectId: string): Promise<ProjectSummary> {
  return request<ProjectSummary>(`/projects/${projectId}/outline/confirm`, {
    method: 'POST',
  });
}

export async function retryOutline(projectId: string): Promise<ProjectSummary> {
  return request<ProjectSummary>(`/projects/${projectId}/outline:retry`, {
    method: 'POST',
  });
}

export async function getOutline(projectId: string): Promise<OutlineResponse> {
  return request<OutlineResponse>(`/projects/${projectId}/outline`);
}

export async function patchStoryboard(projectId: string, payload: StoryboardPatchRequest): Promise<StoryboardPatchResponse> {
  return request<StoryboardPatchResponse>(`/projects/${projectId}/outline/storyboard`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
}

export async function listPages(projectId: string): Promise<PageListResponse> {
  return request<PageListResponse>(`/projects/${projectId}/pages`);
}

export async function getPage(projectId: string, pageId: string): Promise<PageSummary> {
  return request<PageSummary>(`/projects/${projectId}/pages/${pageId}`);
}

export async function getPageQuality(projectId: string, pageId: string): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(`/projects/${projectId}/pages/${pageId}/quality`);
}

export async function retryPageSearchResult(projectId: string, pageId: string, sourceId: string): Promise<PageSummary> {
  return request<PageSummary>(`/projects/${projectId}/pages/${pageId}/search-results/${sourceId}:retry`, {
    method: 'POST',
  });
}

export async function patchPageOutline(
  projectId: string,
  pageId: string,
  payload: {
    title: string;
    content_outline: string[];
    section_title?: string | null;
  },
): Promise<PageSummary> {
  return request<PageSummary>(`/projects/${projectId}/pages/${pageId}/outline`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
}

export async function generatePageSearchQueries(projectId: string, pageId: string): Promise<ActionJobResponse> {
  return request<ActionJobResponse>(`/projects/${projectId}/pages/${pageId}/search-queries:generate`, {
    method: 'POST',
  });
}

export async function runPageSearch(
  projectId: string,
  pageId: string,
  actionType: 'page_search_run' | 'page_search_refresh',
  replaceExisting?: boolean,
): Promise<ActionJobResponse> {
  return request<ActionJobResponse>(`/projects/${projectId}/pages/${pageId}/search:run`, {
    method: 'POST',
    body: JSON.stringify({
      action_type: actionType,
      replace_existing: replaceExisting ?? actionType === 'page_search_refresh',
    }),
  });
}

export async function generatePageSummary(projectId: string, pageId: string): Promise<ActionJobResponse> {
  return request<ActionJobResponse>(`/projects/${projectId}/pages/${pageId}/summary:generate`, {
    method: 'POST',
  });
}

export async function patchPageSummary(projectId: string, pageId: string, summaryMd: string): Promise<PageSummary> {
  return request<PageSummary>(`/projects/${projectId}/pages/${pageId}/summary`, {
    method: 'PATCH',
    body: JSON.stringify({summary_md: summaryMd}),
  });
}

export async function patchPageDraft(projectId: string, pageId: string, svgMarkup: string): Promise<PageSummary> {
  return request<PageSummary>(`/projects/${projectId}/pages/${pageId}/draft`, {
    method: 'PATCH',
    body: JSON.stringify({svg_markup: svgMarkup}),
  });
}

export async function patchPageScene(projectId: string, pageId: string, payload: ScenePatchRequest): Promise<PageSummary> {
  return request<PageSummary>(`/projects/${projectId}/pages/${pageId}/scene:patch`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function generatePageDraft(projectId: string, pageId: string): Promise<ActionJobResponse> {
  return request<ActionJobResponse>(`/projects/${projectId}/pages/${pageId}/draft:generate`, {
    method: 'POST',
  });
}

export async function getPageDraft(projectId: string, pageId: string): Promise<DraftVersion> {
  return request<DraftVersion>(`/projects/${projectId}/pages/${pageId}/draft`);
}

export async function generatePageDesign(projectId: string, pageId: string): Promise<ActionJobResponse> {
  return request<ActionJobResponse>(`/projects/${projectId}/pages/${pageId}/design:generate`, {
    method: 'POST',
  });
}

export async function getPageDesign(projectId: string, pageId: string): Promise<DesignVersion> {
  return request<DesignVersion>(`/projects/${projectId}/pages/${pageId}/design`);
}

export async function runBatchAction(
  projectId: string,
  actionType: 'project_batch_search' | 'project_batch_summary' | 'project_batch_draft' | 'project_batch_design',
): Promise<BatchRun> {
  return request<BatchRun>(`/projects/${projectId}/actions/batch`, {
    method: 'POST',
    body: JSON.stringify({action_type: actionType}),
  });
}

export async function getBatchRun(projectId: string, batchRunId: string): Promise<BatchRun> {
  return request<BatchRun>(`/projects/${projectId}/batches/${batchRunId}`);
}

export async function retryFailedBatch(projectId: string, batchRunId: string): Promise<BatchRun> {
  return request<BatchRun>(`/projects/${projectId}/batches/${batchRunId}:retry-failed`, {
    method: 'POST',
  });
}

export async function cancelProjectTasks(
  projectId: string,
  pageId?: string,
): Promise<{status: string; canceled: number}> {
  return request<{status: string; canceled: number}>(`/projects/${projectId}/tasks:cancel`, {
    method: 'POST',
    body: JSON.stringify(pageId ? {page_id: pageId} : {}),
  });
}

export async function listModelProviders(): Promise<{items: ModelProvider[]}> {
  return request<{items: ModelProvider[]}>('/settings/models');
}

export async function createModelProvider(payload: ModelProviderPayload): Promise<ModelProvider> {
  return request<ModelProvider>('/settings/models', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function patchModelProvider(providerId: string, payload: ModelProviderPayload): Promise<ModelProvider> {
  return request<ModelProvider>(`/settings/models/${providerId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
}

export async function deleteModelProvider(providerId: string): Promise<{status: string; provider_id: string}> {
  return request<{status: string; provider_id: string}>(`/settings/models/${providerId}`, {
    method: 'DELETE',
  });
}

export async function testModelProvider(providerId: string): Promise<ModelTestResult> {
  return request<ModelTestResult>(`/settings/models/${providerId}/test`, {
    method: 'POST',
  });
}

export async function catalogRemoteModels(payload: ModelCatalogPayload): Promise<{items: string[]}> {
  return request<{items: string[]}>('/settings/models:catalog', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function getModelBindings(): Promise<ModelBindingsResponse> {
  return request<ModelBindingsResponse>('/settings/model-bindings');
}

export async function putModelBindings(payload: {
  search?: string | null;
  content?: string | null;
  draft?: string | null;
  design?: string | null;
  expert_enabled?: boolean;
  expert?: Partial<Record<ModelRole, string | null>>;
}): Promise<ModelBindingsResponse> {
  return request<ModelBindingsResponse>('/settings/model-bindings', {
    method: 'PUT',
    body: JSON.stringify(payload),
  });
}

export async function getSearchSettings(): Promise<SearchSettings> {
  return request<SearchSettings>('/settings/search');
}

export async function putSearchSettings(payload: {
  mode?: SearchMode;
  bocha_auth_header?: string;
  tavily_api_key?: string;
  tavily_api_url?: string;
}): Promise<SearchSettings> {
  return request<SearchSettings>('/settings/search', {
    method: 'PUT',
    body: JSON.stringify(payload),
  });
}

export async function getReaderSettings(): Promise<ReaderSettings> {
  return request<ReaderSettings>('/settings/reader');
}

export async function putReaderSettings(payload: {
  mode?: ReaderMode;
  tavily_api_key?: string;
  tavily_api_url?: string;
  firecrawl_api_key?: string;
  firecrawl_api_url?: string;
}): Promise<ReaderSettings> {
  return request<ReaderSettings>('/settings/reader', {
    method: 'PUT',
    body: JSON.stringify(payload),
  });
}

export async function getStyleCards(projectId: string): Promise<StyleCardState> {
  return request<StyleCardState>(`/projects/${projectId}/style-cards`);
}

export async function generateStyleCards(projectId: string): Promise<StyleCardState> {
  return request<StyleCardState>(`/projects/${projectId}/style-cards:generate`, {
    method: 'POST',
  });
}

export async function confirmStyleCard(
  projectId: string,
  styleId: string,
  source = 'any',
): Promise<StyleCardState> {
  return request<StyleCardState>(`/projects/${projectId}/style-cards:confirm`, {
    method: 'POST',
    body: JSON.stringify({style_id: styleId, source}),
  });
}

export async function saveStyleCardToLibrary(projectId: string, styleId?: string): Promise<StyleCardState> {
  return request<StyleCardState>(`/projects/${projectId}/style-cards:save`, {
    method: 'POST',
    body: JSON.stringify({style_id: styleId ?? null}),
  });
}

export async function createExport(
  projectId: string,
  options?: {exportFormat?: string; renderMode?: string; idempotencyKey?: string},
): Promise<ExportJob> {
  return request<ExportJob>(`/projects/${projectId}/exports`, {
    method: 'POST',
    body: JSON.stringify({
      export_format: options?.exportFormat ?? 'pptx',
      render_mode: options?.renderMode,
      idempotency_key: options?.idempotencyKey,
    }),
  });
}

export async function getExport(projectId: string, exportId: string): Promise<ExportJob> {
  return request<ExportJob>(`/projects/${projectId}/exports/${exportId}`);
}

export async function waitForExport(
  projectId: string,
  exportId: string,
  onProgress?: (job: ExportJob) => void,
  signal?: AbortSignal,
): Promise<ExportJob> {
  for (let attempt = 0; attempt < 180; attempt += 1) {
    if (signal?.aborted) {
      throw new DOMException('导出已取消', 'AbortError');
    }
    const job = await getExport(projectId, exportId);
    onProgress?.(job);
    if (job.status === 'completed' || job.status === 'failed' || job.status === 'canceled') {
      return job;
    }
    await new Promise((resolve, reject) => {
      const timer = window.setTimeout(resolve, 500);
      signal?.addEventListener('abort', () => {
        window.clearTimeout(timer);
        reject(new DOMException('导出已取消', 'AbortError'));
      }, {once: true});
    });
  }
  throw new Error('导出超时');
}

export function getExportDownloadUrl(projectId: string, exportId: string): string {
  return buildUrl(`/projects/${projectId}/exports/${exportId}/download`);
}

export async function estimateQualityEval(
  projectId: string,
  options?: {mode?: string; scope?: string; pageIds?: string[]},
): Promise<QualityEvalEstimate> {
  return request<QualityEvalEstimate>(`/projects/${projectId}/quality-evals:estimate`, {
    method: 'POST',
    body: JSON.stringify({
      mode: options?.mode ?? 'quick',
      scope: options?.scope ?? 'canary',
      page_ids: options?.pageIds ?? [],
    }),
  });
}

export async function createQualityEval(
  projectId: string,
  options?: {mode?: string; scope?: string; pageIds?: string[]; idempotencyKey?: string},
): Promise<QualityEvalJob> {
  return request<QualityEvalJob>(`/projects/${projectId}/quality-evals`, {
    method: 'POST',
    body: JSON.stringify({
      mode: options?.mode ?? 'quick',
      scope: options?.scope ?? 'canary',
      page_ids: options?.pageIds ?? [],
      idempotency_key: options?.idempotencyKey,
    }),
  });
}

export async function getQualityEval(projectId: string, evalId: string): Promise<QualityEvalJob> {
  return request<QualityEvalJob>(`/projects/${projectId}/quality-evals/${evalId}`);
}

export async function waitForQualityEval(
  projectId: string,
  evalId: string,
  onProgress?: (job: QualityEvalJob) => void,
  signal?: AbortSignal,
): Promise<QualityEvalJob> {
  for (let attempt = 0; attempt < 180; attempt += 1) {
    if (signal?.aborted) {
      throw new DOMException('主观评估已取消', 'AbortError');
    }
    const job = await getQualityEval(projectId, evalId);
    onProgress?.(job);
    if (job.status === 'completed' || job.status === 'failed' || job.status === 'canceled') {
      return job;
    }
    await new Promise((resolve, reject) => {
      const timer = window.setTimeout(resolve, 500);
      signal?.addEventListener('abort', () => {
        window.clearTimeout(timer);
        reject(new DOMException('主观评估已取消', 'AbortError'));
      }, {once: true});
    });
  }
  throw new Error('主观评估超时');
}

const lastStreamIds = new Map<string, number>();

export function connectProjectEventStream(
  projectId: string,
  handlers: {
    onEvent: (event: ProjectEvent) => void;
    onError?: () => void;
  },
): () => void {
  const seen = new Set<string>();
  const afterId = lastStreamIds.get(projectId) ?? 0;
  const path = afterId > 0
    ? `/projects/${projectId}/events/stream?after_id=${afterId}`
    : `/projects/${projectId}/events/stream`;
  const source = new EventSource(buildUrl(path));
  source.onmessage = (event) => {
    const payload = JSON.parse(event.data) as ProjectEvent;
    const key = payload.event_id || String(payload.stream_id);
    if (seen.has(key)) {
      return;
    }
    seen.add(key);
    lastStreamIds.set(projectId, payload.stream_id);
    handlers.onEvent(payload);
  };
  source.onerror = () => {
    handlers.onError?.();
  };
  return () => {
    source.close();
  };
}
