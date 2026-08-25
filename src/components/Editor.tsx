import { useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowLeft,
  Database,
  Download,
  Eye,
  FileText,
  LoaderCircle,
  Pencil,
  Play,
  RefreshCw,
  Search,
  Send,
  Sparkles,
  Square,
  StickyNote,
  Wand2,
} from 'lucide-react';
import {
  ApiError,
  confirmOutline,
  connectProjectEventStream,
  createExport,
  createMessage,
  createQualityEval,
  cancelProjectTasks,
  estimateQualityEval,
  generatePageDesign,
  generatePageDraft,
  generatePageSearchQueries,
  generatePageSummary,
  generateStyleCards,
  getBatchRun,
  getExportDownloadUrl,
  getOutline,
  getPage,
  getProject,
  getStyleCards,
  confirmStyleCard,
  saveStyleCardToLibrary,
  listMessages,
  listPages,
  patchPageOutline,
  patchStoryboard,
  patchPageSummary,
  patchPageDraft,
  patchPageScene,
  retryFailedBatch,
  retryOutline,
  retryPageSearchResult,
  runBatchAction,
  runPageSearch,
  waitForExport,
  waitForQualityEval,
  type BatchRun,
  type OutlineResponse,
  type PageSummary,
  type ProjectMessage,
  type ProjectSummary,
  type QualityEvalEstimate,
  type QualityEvalJob,
  type StoryboardPatchRequest,
  type StyleCardState,
  type UiSurface,
} from '../lib/ppt-api';
import { createSingleFlightRunner } from '../lib/single-flight';
import { mergeMessageList, summarizeSourcePipeline, shouldRefreshFromEvent } from '../lib/workflow-ui';
import { AgentActivityCard, agentRunFromMessage, reduceAgentRunMap, type AgentRunView } from './AgentActivity';
import { ReplayBar, useProjectReplay } from './ReplayBar';
import { DataModal, PageThumbnail, QueryDimensionList, renderStageBadge, renderStatusPill, SearchResultCard, StyleCardPanel, SvgCanvas, type EditorSurface } from './editor/EditorBits';
import DraftCanvas from './editor/DraftCanvas';
import PresentationPlayer, { type PresentationSlide, type PresentationSurface } from './editor/PresentationPlayer';
import StoryboardPanel from './editor/StoryboardPanel';

function getErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return fallback;
}

function toTimestamp(value?: string): number {
  if (!value) {
    return Number.MAX_SAFE_INTEGER;
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : Number.MAX_SAFE_INTEGER;
}

function replacePageSummary(items: PageSummary[], updatedPage: PageSummary): PageSummary[] {
  return items.map((item) => (item.page_id === updatedPage.page_id ? {...item, ...updatedPage} : item));
}

function isOutlineGenerateRun(run: AgentRunView): boolean {
  const actionType = typeof run.router_decision?.action_type === 'string' ? run.router_decision.action_type : '';
  return run.title === '生成大纲并等待确认' || actionType === 'outline_generate';
}

export default function Editor({
  project,
  onBack,
  onProjectUpdated,
}: {
  project: ProjectSummary;
  onBack: () => void;
  onProjectUpdated: (project: ProjectSummary) => void;
}) {
  const [surface, setSurface] = useState<EditorSurface>('search');
  const [outline, setOutline] = useState<OutlineResponse | null>(null);
  const [pages, setPages] = useState<PageSummary[]>([]);
  const [activePage, setActivePage] = useState<PageSummary | null>(null);
  const [messages, setMessages] = useState<ProjectMessage[]>([]);
  const [liveRuns, setLiveRuns] = useState<Record<string, AgentRunView>>({});
  const replay = useProjectReplay(project.project_id, messages);
  const sourceMessages = replay.active ? replay.visibleMessages : messages;
  const sourceRuns = replay.active ? replay.runs : liveRuns;
  const [chatInput, setChatInput] = useState('');
  const [titleDraft, setTitleDraft] = useState('');
  const [bulletDraft, setBulletDraft] = useState('');
  const [summaryDraft, setSummaryDraft] = useState('');
  const [draftSvgDraft, setDraftSvgDraft] = useState('');
  const [isLoading, setIsLoading] = useState(true);
  const [isSavingOutline, setIsSavingOutline] = useState(false);
  const [isSavingSummary, setIsSavingSummary] = useState(false);
  const [isSavingDraft, setIsSavingDraft] = useState(false);
  const [isSavingScene, setIsSavingScene] = useState(false);
  const [isSendingMessage, setIsSendingMessage] = useState(false);
  const [isExporting, setIsExporting] = useState(false);
  const [isDataModalOpen, setIsDataModalOpen] = useState(false);
  const [isDraftEditing, setIsDraftEditing] = useState(false);
  const [isStoryboardOpen, setIsStoryboardOpen] = useState(false);
  const [isSavingStoryboard, setIsSavingStoryboard] = useState(false);
  const [isPreparingPresentation, setIsPreparingPresentation] = useState(false);
  const [isPresentationOpen, setIsPresentationOpen] = useState(false);
  const [presentationSurface, setPresentationSurface] = useState<PresentationSurface>('design');
  const [presentationSlides, setPresentationSlides] = useState<PresentationSlide[]>([]);
  const [presentationIndex, setPresentationIndex] = useState(0);
  const [retryingSearchSourceIds, setRetryingSearchSourceIds] = useState<Record<string, boolean>>({});
  const [isRetryingOutline, setIsRetryingOutline] = useState(false);
  const [styleCards, setStyleCards] = useState<StyleCardState | null>(null);
  const [isStyleBusy, setIsStyleBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [exportNotice, setExportNotice] = useState<string | null>(null);
  const [exportPhase, setExportPhase] = useState<string | null>(null);
  const [evalNotice, setEvalNotice] = useState<string | null>(null);
  const [evalPhase, setEvalPhase] = useState<string | null>(null);
  const [evalEstimate, setEvalEstimate] = useState<QualityEvalEstimate | null>(null);
  const [isEvaluating, setIsEvaluating] = useState(false);
  const [activeBatch, setActiveBatch] = useState<BatchRun | null>(null);
  const activePageIdRef = useRef<string | null>(null);
  const activeBatchIdRef = useRef<string | null>(null);
  const exportAbortRef = useRef<AbortController | null>(null);
  const evalAbortRef = useRef<AbortController | null>(null);

  const visibleMessages = useMemo(
    () => sourceMessages.filter((message) => message.scope_type === 'project' || message.target_page_id === activePage?.page_id),
    [activePage?.page_id, sourceMessages],
  );
  const visibleMessageAgentRunIds = useMemo(
    () =>
      new Set(
        visibleMessages
          .map((message) => agentRunFromMessage(message)?.agent_run_id)
          .filter((value): value is string => Boolean(value)),
      ),
    [visibleMessages],
  );
  const liveRunList = useMemo(
    () =>
      (Object.values(sourceRuns) as AgentRunView[])
        .filter((item) => (replay.active || item.live) && (!item.target_page_id || item.target_page_id === activePage?.page_id))
        .filter((item) => !visibleMessageAgentRunIds.has(item.agent_run_id)),
    [activePage?.page_id, replay.active, sourceRuns, visibleMessageAgentRunIds],
  );
  const searchStats = useMemo(() => summarizeSourcePipeline(activePage?.page_search_results ?? []), [activePage?.page_search_results]);
  const timelineItems = useMemo(() => {
    const items = [
      ...visibleMessages.map((message, index) => ({
        key: `message-${message.id}`,
        type: 'message' as const,
        sortAt: toTimestamp(message.created_at),
        sortRank: 0,
        index,
        message,
      })),
      ...liveRunList.map((run, index) => ({
        key: `run-${run.agent_run_id}`,
        type: 'run' as const,
        sortAt: toTimestamp(run.started_at),
        sortRank: 1,
        index,
        run,
      })),
    ];
    items.sort((left, right) => {
      if (left.sortAt !== right.sortAt) {
        return left.sortAt - right.sortAt;
      }
      if (left.sortRank !== right.sortRank) {
        return left.sortRank - right.sortRank;
      }
      return left.index - right.index;
    });
    return items;
  }, [liveRunList, visibleMessages]);
  const isOutlineGenerating = useMemo(
    () => (Object.values(sourceRuns) as AgentRunView[]).some((run) => Boolean(run.live) && isOutlineGenerateRun(run)),
    [sourceRuns],
  );
  const isOutlineFailed = useMemo(() => {
    if (outline || isOutlineGenerating || isRetryingOutline) {
      return false;
    }
    const fromMessages = messages.some((message) => {
      const run = agentRunFromMessage(message);
      return Boolean(run && run.run_status === 'failed' && isOutlineGenerateRun(run));
    });
    const fromRuns = (Object.values(sourceRuns) as AgentRunView[]).some(
      (run) => run.run_status === 'failed' && isOutlineGenerateRun(run),
    );
    return fromMessages || fromRuns;
  }, [isOutlineGenerating, isRetryingOutline, messages, outline, sourceRuns]);
  const isPageSearchRunning = useMemo(
    () =>
      (Object.values(liveRuns) as AgentRunView[]).some((item) => {
        if (!item.live || item.target_page_id !== activePage?.page_id) {
          return false;
        }
        const actionType = typeof item.router_decision?.action_type === 'string' ? item.router_decision.action_type : '';
        return actionType === 'page_search_run' || actionType === 'page_search_refresh';
      }),
    [activePage?.page_id, liveRuns],
  );

  useEffect(() => {
    activePageIdRef.current = activePage?.page_id ?? null;
  }, [activePage?.page_id]);

  useEffect(() => {
    setIsDraftEditing(false);
  }, [activePage?.page_id, surface]);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const [projectResponse, messageResponse, pagesResponse, outlineResponse, styleResponse] = await Promise.all([
          getProject(project.project_id),
          listMessages(project.project_id),
          listPages(project.project_id),
          getOutline(project.project_id).catch(() => null),
          getStyleCards(project.project_id).catch(() => null),
        ]);
        if (cancelled) return;
        onProjectUpdated(projectResponse);
        setMessages((current) => mergeMessageList(current, messageResponse.items));
        setPages(pagesResponse.items);
        setOutline(outlineResponse);
        setStyleCards(styleResponse);
        const nextId = activePageIdRef.current && pagesResponse.items.some((item) => item.page_id === activePageIdRef.current)
          ? activePageIdRef.current
          : pagesResponse.items[0]?.page_id ?? null;
        activePageIdRef.current = nextId;
        setActivePage(nextId ? await getPage(project.project_id, nextId) : null);
        setError(null);
      } catch (caughtError) {
        if (!cancelled) setError(getErrorMessage(caughtError, '工作区读取失败'));
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    };
    const refresh = createSingleFlightRunner(load);
    refresh.schedule();
    const disconnect = connectProjectEventStream(project.project_id, {
      onEvent: (event) => {
        setLiveRuns((current) => reduceAgentRunMap(current, event));
        if (shouldRefreshFromEvent(event)) refresh.schedule();
        const batchId = activeBatchIdRef.current;
        if (batchId && (event.event_type === 'batch.updated' || event.event_type === 'task.succeeded' || event.event_type === 'task.failed')) {
          void getBatchRun(project.project_id, batchId).then((batch) => {
            activeBatchIdRef.current = batch.batch_run_id;
            setActiveBatch(batch);
          }).catch(() => undefined);
        }
      },
      onError: () => {
        setError((current) => current ?? '事件流已断开，稍后会自动重连。');
        refresh.schedule();
      },
    });
    return () => {
      cancelled = true;
      refresh.dispose();
      disconnect();
      exportAbortRef.current?.abort();
      evalAbortRef.current?.abort();
    };
  }, [onProjectUpdated, project.project_id]);

  useEffect(() => {
    if (project.current_stage === 'outline') {
      setSurface('outline');
    } else if (surface === 'outline') {
      setSurface('search');
    }
  }, [project.current_stage]);

  useEffect(() => {
    if (!activePage) {
      setTitleDraft('');
      setBulletDraft('');
      setSummaryDraft('');
      setDraftSvgDraft('');
      return;
    }
    setTitleDraft(activePage.title);
    setBulletDraft(activePage.content_outline.join('\n'));
    setSummaryDraft(activePage.page_summary_md);
    setDraftSvgDraft(activePage.draft?.draft_svg_markup ?? '');
  }, [activePage]);

  const handleSaveOutline = async () => {
    if (!activePage || isSavingOutline) return;
    setIsSavingOutline(true);
    try {
      const nextPage = await patchPageOutline(project.project_id, activePage.page_id, {
        title: titleDraft.trim(),
        content_outline: bulletDraft.split('\n').map((item) => item.trim()).filter(Boolean),
        section_title: activePage.part_title,
      });
      setActivePage(nextPage);
      setPages((current) => replacePageSummary(current, nextPage));
      setError(null);
    } catch (caughtError) {
      setError(getErrorMessage(caughtError, '页面结构保存失败'));
    } finally {
      setIsSavingOutline(false);
    }
  };

  const handleSaveSummary = async () => {
    if (!activePage || isSavingSummary) return;
    setIsSavingSummary(true);
    try {
      const nextPage = await patchPageSummary(project.project_id, activePage.page_id, summaryDraft);
      setActivePage(nextPage);
      setPages((current) => replacePageSummary(current, nextPage));
      setError(null);
    } catch (caughtError) {
      setError(getErrorMessage(caughtError, 'summary 保存失败'));
    } finally {
      setIsSavingSummary(false);
    }
  };

  const handleSaveDraft = async () => {
    if (!activePage || isSavingDraft) return;
    setIsSavingDraft(true);
    try {
      const nextPage = await patchPageDraft(project.project_id, activePage.page_id, draftSvgDraft);
      setActivePage(nextPage);
      setPages((current) => replacePageSummary(current, nextPage));
      setError(null);
    } catch (caughtError) {
      setError(getErrorMessage(caughtError, '策划稿保存失败'));
    } finally {
      setIsSavingDraft(false);
    }
  };

  const handleSaveScene = async (payload: Parameters<typeof patchPageScene>[2]) => {
    if (!activePage || isSavingScene) return;
    setIsSavingScene(true);
    try {
      const nextPage = await patchPageScene(project.project_id, activePage.page_id, payload);
      setActivePage(nextPage);
      setPages((current) => replacePageSummary(current, nextPage));
      setDraftSvgDraft(nextPage.draft?.draft_svg_markup ?? '');
      setError(null);
    } catch (caughtError) {
      setError(getErrorMessage(caughtError, '策划稿画布保存失败'));
      throw caughtError;
    } finally {
      setIsSavingScene(false);
    }
  };

  const sendMessage = async (content: string, options?: { clearInput?: boolean }) => {
    const normalizedContent = content.trim();
    const isOutlineSurface = surface === 'outline' || project.current_stage === 'outline';
    if (!normalizedContent || isSendingMessage || (!isOutlineSurface && !activePage)) return;
    setIsSendingMessage(true);
    try {
      const message = await createMessage(project.project_id, {
        scope_type: isOutlineSurface ? 'project' : 'page',
        target_page_id: isOutlineSurface ? null : activePage?.page_id ?? null,
        ui_surface: (isOutlineSurface ? 'outline' : surface) as UiSurface,
        content_md: normalizedContent,
      });
      setMessages((current) => mergeMessageList(current, [message]));
      if (options?.clearInput) {
        setChatInput((current) => (current.trim() === normalizedContent ? '' : current));
      }
      setError(null);
    } catch (caughtError) {
      setError(getErrorMessage(caughtError, '消息发送失败'));
    } finally {
      setIsSendingMessage(false);
    }
  };

  const handleSendMessage = async () => {
    await sendMessage(chatInput, {clearInput: true});
  };

  const runAction = async (runner: () => Promise<unknown>) => {
    try {
      await runner();
      setError(null);
    } catch (caughtError) {
      setError(getErrorMessage(caughtError, '动作执行失败'));
    }
  };

  const rememberBatch = (batch: BatchRun) => {
    activeBatchIdRef.current = batch.batch_run_id;
    setActiveBatch(batch);
  };

  const handleBatchAction = async (
    actionType: 'project_batch_search' | 'project_batch_summary' | 'project_batch_draft' | 'project_batch_design',
  ) => {
    await runAction(async () => {
      rememberBatch(await runBatchAction(project.project_id, actionType));
    });
  };

  const handleRetryFailedBatch = async () => {
    if (!activeBatch) return;
    await runAction(async () => {
      rememberBatch(await retryFailedBatch(project.project_id, activeBatch.batch_run_id));
    });
  };

  const handleExport = async () => {
    exportAbortRef.current?.abort();
    const controller = new AbortController();
    exportAbortRef.current = controller;
    setIsExporting(true);
    setExportPhase('snapshot');
    try {
      const queued = await createExport(project.project_id);
      const job = await waitForExport(project.project_id, queued.export_id, (current) => {
        setExportPhase(current.phase || current.status);
      }, controller.signal);
      if (job.status !== 'completed' || !job.download_ready) {
        const detail = job.error_detail as {message?: string} | undefined;
        throw new Error(detail?.message || job.error_code || '导出失败');
      }
      window.open(getExportDownloadUrl(project.project_id, job.export_id), '_blank', 'noopener,noreferrer');
      const notices = [...(job.font_report?.notices ?? [])];
      if (job.input_stale) notices.push('该文件对应导出时的旧版本，当前页面已更新。');
      if (job.render_mode === 'image') notices.push('整页图不可逐字编辑。');
      setExportNotice(notices.join('\n') || null);
      setError(null);
    } catch (caughtError) {
      if (caughtError instanceof DOMException && caughtError.name === 'AbortError') {
        return;
      }
      setError(getErrorMessage(caughtError, '导出失败'));
    } finally {
      if (exportAbortRef.current === controller) {
        exportAbortRef.current = null;
      }
      setIsExporting(false);
      setExportPhase(null);
    }
  };

  const summarizeEvalJob = (job: QualityEvalJob) => {
    const pages = job.report?.pages ?? [];
    const hardFails = pages.filter((item) => item.hard_fail || item.tracks?.verdict === 'hard_fail').length;
    const scored = pages.filter((item) => item.tracks?.subjective_score != null).length;
    if (job.status === 'failed') {
      const detail = job.error_detail as {message?: string} | undefined;
      return detail?.message || job.error_code || '主观评估失败；页面设计状态未改动。';
    }
    if (hardFails) {
      return `主观评估完成：${hardFails} 页仍有硬失败，分数不能覆盖。${scored ? `另有 ${scored} 页仅作诊断。` : '未给出可展示的主观分。'}`;
    }
    if (!scored) {
      return '主观评估完成，但没有给出分数（未运行、跳过或缺少渲染图）。页面仍按硬检查决定是否 ready。';
    }
    return `主观评估完成：${scored} 页有诊断分数，不改变硬失败或 ready 状态。`;
  };

  const handleQualityEval = async () => {
    evalAbortRef.current?.abort();
    if (!evalEstimate) {
      try {
        const estimate = await estimateQualityEval(project.project_id, {mode: 'quick'});
        setEvalEstimate(estimate);
        setEvalNotice(
          `预计评估 ${estimate.page_count} 页，约 ${estimate.estimated_calls} 次调用 / ${estimate.estimated_tokens} tokens / ${estimate.estimated_duration_s}s。再次点击确认开始；主观分数不会覆盖硬失败。`,
        );
      } catch (caughtError) {
        setError(getErrorMessage(caughtError, '无法预估主观评估'));
      }
      return;
    }
    const controller = new AbortController();
    evalAbortRef.current = controller;
    setIsEvaluating(true);
    setEvalPhase('snapshot');
    try {
      const queued = await createQualityEval(project.project_id, {mode: evalEstimate.mode, scope: evalEstimate.scope});
      const job = await waitForQualityEval(project.project_id, queued.eval_id, (current) => {
        setEvalPhase(current.phase || current.status);
      }, controller.signal);
      setEvalNotice(summarizeEvalJob(job));
      setEvalEstimate(null);
      setError(null);
    } catch (caughtError) {
      if (caughtError instanceof DOMException && caughtError.name === 'AbortError') {
        return;
      }
      setError(getErrorMessage(caughtError, '主观评估失败'));
    } finally {
      if (evalAbortRef.current === controller) {
        evalAbortRef.current = null;
      }
      setIsEvaluating(false);
      setEvalPhase(null);
    }
  };

  const refreshStyleCards = async (runner: () => Promise<StyleCardState>) => {
    setIsStyleBusy(true);
    try {
      const next = await runner();
      setStyleCards(next);
      onProjectUpdated(await getProject(project.project_id));
      setError(null);
    } catch (caughtError) {
      setError(getErrorMessage(caughtError, '风格卡操作失败'));
    } finally {
      setIsStyleBusy(false);
    }
  };

  const hasRunningTask = pages.some((page) =>
    [page.search_status, page.summary_status, page.draft_status, page.design_status].includes('running'),
  );

  const handleOpenPage = async (pageId: string, nextSurface?: EditorSurface) => {
    activePageIdRef.current = pageId;
    if (nextSurface) {
      setSurface(nextSurface);
    }
    setActivePage(await getPage(project.project_id, pageId));
  };

  const handleOpenPresentation = async () => {
    if (surface === 'search' || !pages.length || isPreparingPresentation) {
      return;
    }

    const targetSurface: PresentationSurface = surface;
    const orderedPages = [...pages].sort((left, right) => left.sort_order - right.sort_order);
    const nextIndex = Math.max(
      orderedPages.findIndex((page) => page.page_id === activePage?.page_id),
      0,
    );

    setIsPreparingPresentation(true);
    try {
      const detailedPages = await Promise.all(
        orderedPages.map(async (page) => {
          if (page.page_id === activePage?.page_id && activePage) {
            return activePage;
          }
          if (targetSurface === 'draft' && !page.current_draft_version_id) {
            return null;
          }
          if (targetSurface === 'design' && !page.current_design_version_id) {
            return null;
          }
          try {
            return await getPage(project.project_id, page.page_id);
          } catch {
            return null;
          }
        }),
      );

      const slides = orderedPages.map((page, index) => {
        const detail = detailedPages[index];
        const fallbackMarkup = page.preview_surface === targetSurface ? page.preview_svg_markup ?? null : null;
        const markup =
          targetSurface === 'draft'
            ? detail?.draft?.draft_svg_markup ?? fallbackMarkup
            : detail?.design?.design_svg_markup ?? fallbackMarkup;

        return {
          pageId: page.page_id,
          sortOrder: page.sort_order,
          title: detail?.title ?? page.title,
          pageRole: page.page_role,
          partTitle: detail?.part_title ?? page.part_title,
          markup,
        } satisfies PresentationSlide;
      });

      setPresentationSurface(targetSurface);
      setPresentationSlides(slides);
      setPresentationIndex(nextIndex);
      setIsPresentationOpen(true);
      setError(null);
    } catch (caughtError) {
      setError(getErrorMessage(caughtError, '放映准备失败'));
    } finally {
      setIsPreparingPresentation(false);
    }
  };

  const handleStoryboardReorder = async (
    parts: StoryboardPatchRequest['parts'],
  ) => {
    if (isSavingStoryboard) return;
    setIsSavingStoryboard(true);
    try {
      const response = await patchStoryboard(project.project_id, {parts});
      setPages(response.items);
      setOutline(response.outline);
      if (activePageIdRef.current) {
        const nextSummary = response.items.find((item) => item.page_id === activePageIdRef.current);
        if (nextSummary) {
          setActivePage((current) => (current ? {...current, ...nextSummary} : current));
        } else {
          const fallbackPage = response.items[0] ?? null;
          activePageIdRef.current = fallbackPage?.page_id ?? null;
          setActivePage(fallbackPage ? await getPage(project.project_id, fallbackPage.page_id) : null);
        }
      } else if (!activePage && response.items.length) {
        const fallbackPage = response.items[0];
        activePageIdRef.current = fallbackPage.page_id;
        setActivePage(await getPage(project.project_id, fallbackPage.page_id));
      }
      setError(null);
    } catch (caughtError) {
      setError(getErrorMessage(caughtError, '结构保存失败'));
    } finally {
      setIsSavingStoryboard(false);
    }
  };

  const handleRetrySearchResult = async (sourceId: string) => {
    if (!activePage || retryingSearchSourceIds[sourceId]) {
      return;
    }
    setRetryingSearchSourceIds((current) => ({
      ...current,
      [sourceId]: true,
    }));
    try {
      const nextPage = await retryPageSearchResult(project.project_id, activePage.page_id, sourceId);
      setActivePage(nextPage);
      setPages((current) => replacePageSummary(current, nextPage));
      setError(null);
    } catch (caughtError) {
      setError(getErrorMessage(caughtError, '资料重试失败'));
    } finally {
      setRetryingSearchSourceIds((current) => {
        const next = {...current};
        delete next[sourceId];
        return next;
      });
    }
  };

  const handleConfirmOutline = async () => {
    const nextProject = await confirmOutline(project.project_id);
    onProjectUpdated(nextProject);
    setSurface('search');
  };

  const handleRetryOutline = async () => {
    if (isRetryingOutline || isOutlineGenerating) {
      return;
    }
    setIsRetryingOutline(true);
    try {
      const nextProject = await retryOutline(project.project_id);
      onProjectUpdated(nextProject);
      setError(null);
    } catch (caughtError) {
      setError(getErrorMessage(caughtError, '重新生成大纲失败'));
    } finally {
      setIsRetryingOutline(false);
    }
  };

  const previewMarkup = surface === 'design' ? activePage?.design?.design_svg_markup ?? null : surface === 'draft' ? activePage?.draft_preview_svg_markup ?? activePage?.draft?.draft_svg_markup ?? null : null;
  const searchDisabled = activePage?.page_role !== 'content' || project.current_stage !== 'search';
  const canPresent = surface !== 'search' && surface !== 'outline' && pages.length > 0;
  const draftStats = useMemo(() => {
    const total = pages.length;
    let preview = 0;
    let ready = 0;
    let failed = 0;
    let empty = 0;
    for (const page of pages) {
      if (page.draft_status === 'ready' || page.draft_status === 'confirmed') {
        ready += 1;
      } else if (page.draft_status === 'failed') {
        failed += 1;
      } else {
        empty += 1;
      }
      if (page.draft_preview_svg_markup || page.draft_status === 'ready' || page.draft_status === 'confirmed' || page.draft_status === 'failed') {
        preview += 1;
      }
    }
    return {total, preview, ready, failed, empty};
  }, [pages]);

  if (project.current_stage === 'outline' && !outline) {
    const retryBusy = isRetryingOutline || isOutlineGenerating;
    return (
      <div className="h-screen flex flex-col bg-[#f8f9fa]">
        <header className="h-14 bg-white border-b border-slate-200 flex items-center px-6 justify-between shrink-0">
          <button onClick={onBack} className="flex items-center gap-2 text-slate-500 hover:text-slate-800 text-sm font-medium"><ArrowLeft size={18} />返回</button>
          <div className="font-semibold text-slate-800">{isOutlineFailed ? '大纲生成失败' : '大纲生成中'}</div>
          {isOutlineFailed ? (
            <button
              type="button"
              onClick={() => void handleRetryOutline()}
              disabled={retryBusy}
              className="flex items-center gap-2 px-4 py-2 text-sm font-semibold text-white bg-blue-600 hover:bg-blue-700 rounded-xl disabled:opacity-40"
            >
              <RefreshCw size={16} />
              {retryBusy ? '正在重新入队...' : '重新生成大纲'}
            </button>
          ) : (
            <div className="text-sm text-slate-400">正在处理</div>
          )}
        </header>
        <ReplayBar replay={replay} />
        <div className="flex-1 flex overflow-hidden p-6 gap-6">
          <div className="flex-1 rounded-[2rem] border border-slate-200 bg-white shadow-sm p-8 space-y-4">
            <div className="text-2xl font-semibold text-slate-800">{isOutlineFailed ? '大纲生成失败' : '正在生成大纲'}</div>
            <div className="text-sm text-slate-500 leading-relaxed">
              {isOutlineFailed
                ? '生成大纲时出错。可以查看右侧失败卡片，确认后重新生成。'
                : '右侧卡片会实时显示当前步骤和异常。生成完成后可以修改章节，再确认进入资料阶段。'}
            </div>
            {error ? <div className="text-sm text-rose-600">{error}</div> : null}
            {isOutlineFailed ? (
              <button
                type="button"
                onClick={() => void handleRetryOutline()}
                disabled={retryBusy}
                className="inline-flex items-center gap-2 px-5 py-2.5 text-sm font-semibold text-white bg-blue-600 hover:bg-blue-700 rounded-xl disabled:opacity-40"
              >
                <RefreshCw size={16} />
                {retryBusy ? '正在重新入队...' : '重新生成大纲'}
              </button>
            ) : null}
          </div>
          <div className="w-[440px] bg-white border border-slate-200 rounded-[2rem] flex flex-col overflow-hidden shadow-sm">
            <div className="flex-1 overflow-y-auto p-5 space-y-6 bg-slate-50/50">
              {liveRunList.map((run) => (
                <div key={run.agent_run_id} className="flex justify-start">
                  <AgentActivityCard
                    run={run}
                    onFailedRetry={() => { void handleRetryOutline(); }}
                    failedRetrying={retryBusy}
                    failedRetryLabel="重新生成大纲"
                  />
                </div>
              ))}
              {messages.map((message) => message.role === 'assistant' ? (
                <div key={message.id} className="flex justify-start">
                  {agentRunFromMessage(message) ? (
                    <AgentActivityCard
                      run={{...agentRunFromMessage(message)!, content_md: message.content_md}}
                      accent="emerald"
                      onFailedRetry={() => { void handleRetryOutline(); }}
                      failedRetrying={retryBusy}
                      failedRetryLabel="重新生成大纲"
                    />
                  ) : (
                    <div className="bg-white border border-slate-200 shadow-sm px-5 py-4 rounded-2xl rounded-tl-sm max-w-[95%] w-full"><p className="text-sm text-slate-600 whitespace-pre-wrap">{message.content_md}</p></div>
                  )}
                </div>
              ) : null)}
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (project.current_stage === 'outline') {
    return (
      <div className="h-screen flex flex-col bg-[#f8f9fa] text-slate-800">
        <header className="h-16 bg-white border-b border-slate-200 flex items-center justify-between shrink-0 shadow-sm px-6">
          <button onClick={onBack} className="flex items-center gap-2 px-4 py-2.5 text-sm font-semibold text-slate-700 hover:bg-slate-100 rounded-xl border border-slate-200"><ArrowLeft size={18} />返回</button>
          <div className="font-semibold text-slate-800">确认大纲</div>
          <button onClick={() => void runAction(handleConfirmOutline)} disabled={replay.active} className="flex items-center gap-2 px-4 py-2.5 text-sm font-semibold text-white bg-blue-600 hover:bg-blue-700 rounded-xl disabled:opacity-40">确认大纲，开始按页找资料</button>
        </header>
        <ReplayBar replay={replay} />
        <div className="flex-1 min-h-0 flex overflow-hidden">
          <div className="relative min-h-0 flex-1 overflow-hidden">
            <StoryboardPanel
              outline={outline}
              pages={pages}
              activePageId={activePage?.page_id ?? null}
              surface="outline"
              isSaving={isSavingStoryboard}
              onJump={(pageId) => {
                void runAction(() => handleOpenPage(pageId, 'outline'));
              }}
              onReorder={handleStoryboardReorder}
            />
          </div>
          <div className="w-[440px] bg-white border-l border-slate-200 flex flex-col shrink-0 shadow-sm">
            <div className="flex-1 overflow-y-auto p-5 space-y-6 bg-slate-50/50">
              {liveRunList.map((run) => <div key={run.agent_run_id} className="flex justify-start"><AgentActivityCard run={run} /></div>)}
              {messages.map((message) => message.role === 'assistant' ? (
                <div key={message.id} className="flex justify-start">
                  {agentRunFromMessage(message) ? <AgentActivityCard run={{...agentRunFromMessage(message)!, content_md: message.content_md}} accent="emerald" /> : <div className="bg-white border border-slate-200 shadow-sm px-5 py-4 rounded-2xl rounded-tl-sm max-w-[95%] w-full"><p className="text-sm text-slate-600 whitespace-pre-wrap">{message.content_md}</p></div>}
                </div>
              ) : (
                <div key={message.id} className="flex justify-end">
                  <div className="bg-blue-600 text-white px-5 py-4 rounded-2xl rounded-tr-sm max-w-[95%]"><p className="text-sm whitespace-pre-wrap">{message.content_md}</p></div>
                </div>
              ))}
            </div>
            <div className="p-5 bg-white border-t border-slate-100 space-y-3">
              {error ? <div className="text-sm text-red-600">{error}</div> : null}
              <div className="bg-slate-50 rounded-2xl flex items-end p-2.5 border border-slate-200">
                <textarea value={chatInput} placeholder={replay.active ? '回放模式已禁用输入' : '例如：把第三章改成风险与对策，或增加一页案例'} className="flex-1 bg-transparent border-none outline-none resize-none max-h-32 min-h-[44px] py-2.5 px-3 text-sm text-slate-700" rows={1} readOnly={replay.active} onChange={(event) => setChatInput(event.target.value)} onKeyDown={(event) => { if (replay.active) return; if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void handleSendMessage(); } }} />
                <button disabled={replay.active || isSendingMessage || !chatInput.trim()} onClick={() => void handleSendMessage()} className="p-2.5 text-blue-600 hover:text-blue-700 disabled:text-slate-300">{isSendingMessage ? <LoaderCircle size={20} className="animate-spin" /> : <Send size={20} />}</button>
              </div>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="h-screen flex flex-col bg-[#f8f9fa] text-slate-800">
      <header className="h-16 bg-white border-b border-slate-200 flex items-center justify-between shrink-0 shadow-sm">
        <div className="w-56 h-full flex items-center justify-center border-r border-slate-200 shrink-0">
          <div className="flex bg-slate-100 p-1 rounded-xl border border-slate-200/50 w-[220px]">
            {(['search', 'draft', 'design'] as const).map((item) => (
              <button key={item} onClick={() => setSurface(item)} className={`flex-1 py-1.5 rounded-lg text-xs font-semibold transition-all ${surface === item ? 'bg-white shadow-sm text-slate-800 border border-slate-200/50' : 'text-slate-500 hover:text-slate-700'}`}>
                {item === 'search' ? '资料' : item === 'draft' ? '策划稿' : '设计稿'}
              </button>
            ))}
          </div>
        </div>
        <div className="flex-1 px-6 font-semibold text-slate-800 flex items-center gap-3 flex-wrap">
          {activePage?.title || project.title}
          {activePage ? renderStageBadge('search', activePage.search_status) : null}
          {activePage ? renderStageBadge('summary', activePage.summary_status) : null}
          {activePage ? renderStageBadge('draft', activePage.draft_status) : null}
          {activePage ? renderStageBadge('design', activePage.design_status) : null}
        </div>
        <div className="flex items-center gap-3 px-6 shrink-0">
          <button onClick={() => setIsStoryboardOpen((current) => !current)} className={`flex items-center gap-2 px-4 py-2.5 text-sm font-semibold rounded-xl border transition-all ${isStoryboardOpen ? 'border-blue-200 bg-blue-50 text-blue-700' : 'border-slate-200 text-slate-700 hover:bg-slate-100'}`}><StickyNote size={18} />便利贴</button>
          <button onClick={onBack} className="flex items-center gap-2 px-4 py-2.5 text-sm font-semibold text-slate-700 hover:bg-slate-100 rounded-xl border border-slate-200"><ArrowLeft size={18} />返回</button>
          <button onClick={() => { void handleOpenPresentation(); }} disabled={!canPresent || isPreparingPresentation} className="flex items-center gap-2 px-4 py-2.5 text-sm font-semibold text-slate-700 hover:bg-slate-100 rounded-xl border border-slate-200 disabled:opacity-40">{isPreparingPresentation ? <LoaderCircle size={18} className="animate-spin" /> : <Play size={18} />}放映</button>
          <button onClick={() => void handleBatchAction(surface === 'search' ? 'project_batch_search' : surface === 'draft' ? 'project_batch_draft' : 'project_batch_design')} className="flex items-center gap-2 px-4 py-2.5 text-sm font-semibold text-slate-700 hover:bg-slate-100 rounded-xl border border-slate-200"><Sparkles size={18} />{surface === 'search' ? '批量搜索' : surface === 'draft' ? '批量策划稿' : '批量设计'}</button>
          {surface === 'search' ? <button onClick={() => void handleBatchAction('project_batch_summary')} className="flex items-center gap-2 px-4 py-2.5 text-sm font-semibold text-slate-700 hover:bg-slate-100 rounded-xl border border-slate-200"><Wand2 size={18} />批量 summary</button> : null}
          <button disabled={!hasRunningTask} onClick={() => void runAction(() => cancelProjectTasks(project.project_id))} className="flex items-center gap-2 px-4 py-2.5 text-sm font-semibold text-rose-700 hover:bg-rose-50 rounded-xl border border-rose-200 disabled:opacity-40"><Square size={16} />取消</button>
          <button onClick={() => void handleQualityEval()} disabled={isEvaluating || replay.active} className="flex items-center gap-2 px-4 py-2.5 text-sm font-semibold text-slate-700 hover:bg-slate-100 rounded-xl border border-slate-200 disabled:opacity-40">{isEvaluating ? <LoaderCircle size={18} className="animate-spin" /> : <Eye size={18} />}{isEvaluating ? (evalPhase === 'vlm' ? '评审中' : evalPhase === 'render' ? '渲染中' : '评估中') : evalEstimate ? '确认评估' : '视觉评估'}</button>
          <button onClick={() => void handleExport()} disabled={isExporting || replay.active} className="flex items-center gap-2 px-4 py-2.5 text-sm font-semibold text-white bg-blue-600 hover:bg-blue-700 rounded-xl disabled:bg-blue-300">{isExporting ? <LoaderCircle size={18} className="animate-spin" /> : <Download size={18} />}{isExporting ? (exportPhase === 'preflight' ? '预检中' : exportPhase === 'build' ? '构建中' : exportPhase === 'verify' ? '校验中' : '导出中') : '导出'}</button>
        </div>
      </header>
      <ReplayBar replay={replay} />
      {activeBatch ? (
        <div className="px-6 py-2 text-sm text-slate-700 bg-white border-b border-slate-100 flex items-center justify-between gap-4">
          <span>
            批量 {activeBatch.status}：成功 {activeBatch.success_count} / 失败 {activeBatch.failed_count} / 运行 {activeBatch.running_count} / 取消 {activeBatch.canceled_count}（排队 {activeBatch.queued_count}，跳过 {activeBatch.skipped_count}）
          </span>
          {activeBatch.failed_count > 0 && (activeBatch.status === 'failed' || activeBatch.status === 'partial_success') ? (
            <button onClick={() => void handleRetryFailedBatch()} className="px-3 py-1 text-xs font-semibold text-blue-700 bg-blue-50 border border-blue-200 rounded-lg">
              只重试失败页
            </button>
          ) : null}
        </div>
      ) : null}
      {exportNotice ? <div className="px-6 py-2 text-sm text-amber-800 bg-amber-50 border-b border-amber-100">{exportNotice}</div> : null}
      {evalNotice ? <div className="px-6 py-2 text-sm text-slate-700 bg-slate-50 border-b border-slate-100">{evalNotice}</div> : null}

      {isStoryboardOpen ? (
        <StoryboardPanel
          outline={outline}
          pages={pages}
          activePageId={activePage?.page_id ?? null}
          surface={surface}
          isSaving={isSavingStoryboard}
          onJump={(pageId, nextSurface) => {
            setIsStoryboardOpen(false);
            void runAction(() => handleOpenPage(pageId, nextSurface));
          }}
          onReorder={handleStoryboardReorder}
        />
      ) : (
      <div className="flex-1 flex overflow-hidden">
        <div className="w-56 bg-white border-r border-slate-200 flex flex-col shrink-0 shadow-sm">
          <div className="p-4 flex justify-between items-center border-b border-slate-100 text-sm"><span className="font-semibold text-slate-800">幻灯片</span><span className="text-slate-400 font-medium">共 {pages.length} 张</span></div>
          <div className="flex-1 overflow-y-auto p-3 space-y-3">
            {pages.map((page) => <PageThumbnail key={page.page_id} page={page} surface={surface} active={page.page_id === activePage?.page_id} onClick={() => void runAction(() => handleOpenPage(page.page_id))} />)}
          </div>
        </div>

        <div className="flex-1 flex flex-col overflow-hidden bg-[#f3f4f6]">
          <div className="border-b border-slate-200 bg-white px-8 py-5 flex items-center justify-between gap-6">
            <div><div className="text-xs uppercase tracking-wide text-slate-400">{activePage?.page_role} / {activePage?.part_title || '未分组'}</div><div className="text-2xl font-semibold text-slate-800">{activePage?.title || '未选择页面'}</div></div>
            {surface === 'search' ? <div className="flex flex-wrap gap-2 justify-end">{renderStatusPill('搜索结果', `${searchStats.total} 条`, 'slate')}{activePage?.search_coverage?.latest_round ? renderStatusPill('最新轮次', `R${activePage.search_coverage.latest_round} · ${activePage.search_coverage.latest_round_hits} 条`, 'blue') : null}{renderStatusPill('全文完成', `${searchStats.readReady}/${searchStats.total}`, searchStats.readReady ? 'emerald' : 'amber')}{renderStatusPill('入库完成', `${searchStats.chunkReady}/${searchStats.total}`, searchStats.chunkReady ? 'blue' : 'amber')}</div> : null}
          </div>
          <div className="flex-1 overflow-auto p-8">
            {surface === 'search' ? (
              <div className="space-y-6">
                <div className="rounded-[2rem] border border-slate-200 bg-white p-6 shadow-sm space-y-4">
                  <div className="flex items-center justify-between"><div><div className="text-lg font-semibold text-slate-800">当前页资料池</div><div className="text-sm text-slate-500 mt-1">搜索摘要、整理稿和全文抓取状态会持续写回这里。补充检索会保留上一轮并打上新的轮次标签。</div></div><div className="text-sm text-slate-500">文档 {activePage?.page_corpus_digest.document_count ?? 0} / chunk {activePage?.page_corpus_digest.chunk_count ?? 0} / 正文 {activePage?.page_corpus_digest.content_chars ?? 0} 字</div></div>
                  {activePage?.page_search_queries.length ? <QueryDimensionList queries={activePage.page_search_queries} /> : null}
                  {activePage?.page_images?.length ? (
                    <div className="space-y-2">
                      <div className="text-sm font-semibold text-slate-700">本页配图 {activePage.page_images.length} 张</div>
                      <div className="flex flex-wrap gap-3">
                        {activePage.page_images.map((image) => (
                          <a
                            key={image.image_id}
                            href={image.source_url}
                            target="_blank"
                            rel="noreferrer"
                            className="rounded-2xl border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600 hover:border-blue-200 hover:text-blue-700"
                          >
                            <div className="font-semibold">{image.image_id}</div>
                            <div className="mt-1 line-clamp-2">{image.caption || image.source_title || '检索配图'}</div>
                          </a>
                        ))}
                      </div>
                    </div>
                  ) : null}
                  {searchDisabled ? <div className="text-sm text-slate-500">固定页不参与页级搜索，直接使用大纲结构进入 draft/design。</div> : activePage?.page_search_results.length ? <div className="space-y-4">{activePage.page_search_results.map((item) => <SearchResultCard key={item.id} item={item} onRetry={(sourceId) => { void handleRetrySearchResult(sourceId); }} retrying={Boolean(retryingSearchSourceIds[item.id])} allowRetry={!isPageSearchRunning} />)}</div> : <div className="text-sm text-slate-400">当前页还没有资料池结果。右侧聊天栏会实时显示 agent 进度。</div>}
                </div>
                <div className="rounded-[2rem] border border-slate-200 bg-white p-6 shadow-sm space-y-4">
                  <div className="flex items-center justify-between"><div className="text-lg font-semibold text-slate-800">当前页 summary 预览</div><button onClick={() => setIsDataModalOpen(true)} className="rounded-xl border border-slate-200 px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"><FileText size={14} className="inline mr-1" />在弹窗中编辑</button></div>
                  {activePage?.page_summary_md ? <div className="rounded-2xl bg-slate-50 border border-slate-100 p-4 text-sm text-slate-600 leading-relaxed whitespace-pre-wrap">{activePage.page_summary_md}</div> : <div className="text-sm text-slate-400">当前页 summary 尚未生成。</div>}
                </div>
              </div>
            ) : (
              <div className="space-y-4">
                {surface === 'draft' ? (
                  <div className="rounded-[2rem] border border-slate-200 bg-white px-6 py-4 shadow-sm flex items-center justify-between gap-4">
                    <div>
                      <div className="text-lg font-semibold text-slate-800">策划稿</div>
                      {isDraftEditing ? (
                        <div className="text-sm text-slate-500 mt-1">改文案、开关 optional 槽、拖拽布局盒。保存后会生成新的 LayoutPlan，设计稿按这版对齐。</div>
                      ) : (
                        <div className="text-sm text-slate-500 mt-1">
                          默认只展示和左侧缩略图同一份预览。已有预览 {draftStats.preview}/{draftStats.total} 张，闸门通过 {draftStats.ready} 张，失败 {draftStats.failed} 张，未生成 {draftStats.empty} 张。
                        </div>
                      )}
                    </div>
                    <div className="flex flex-wrap gap-2 shrink-0">
                      {isDraftEditing ? (
                        <>
                          <button onClick={() => setIsDraftEditing(false)} className="rounded-xl border border-slate-200 px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50">退出编辑</button>
                          <button onClick={() => setIsDataModalOpen(true)} className="rounded-xl border border-slate-200 px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"><FileText size={14} className="inline mr-1" />原始数据</button>
                        </>
                      ) : (
                        <button
                          type="button"
                          disabled={!activePage || replay.active}
                          onClick={() => setIsDraftEditing(true)}
                          className="rounded-xl bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-40"
                        >
                          <Pencil size={14} className="inline mr-1" />修改策划稿
                        </button>
                      )}
                    </div>
                  </div>
                ) : null}
                {surface === 'design' ? (
                  <StyleCardPanel
                    frozen={styleCards?.frozen ?? project.style_card ?? null}
                    candidates={styleCards?.candidates ?? []}
                    library={styleCards?.library ?? []}
                    busy={isStyleBusy}
                    onGenerate={() => void refreshStyleCards(() => generateStyleCards(project.project_id))}
                    onConfirm={(styleId, source) => void refreshStyleCards(() => confirmStyleCard(project.project_id, styleId, source))}
                    onSave={() => void refreshStyleCards(() => saveStyleCardToLibrary(project.project_id))}
                  />
                ) : null}
                {surface === 'draft' ? (
                  isDraftEditing ? (
                    <DraftCanvas
                      page={activePage}
                      readOnly={replay.active}
                      saving={isSavingScene}
                      onSave={handleSaveScene}
                      onOpenRaw={() => setIsDataModalOpen(true)}
                    />
                  ) : (
                    <SvgCanvas markup={previewMarkup} placeholder={activePage?.draft_status === 'failed' ? '策划稿生成失败' : activePage?.draft_status === 'running' ? '策划稿生成中' : '策划稿尚未生成'} />
                  )
                ) : (
                  <SvgCanvas markup={previewMarkup} placeholder="当前页设计稿尚未生成" />
                )}
              </div>
            )}
          </div>
        </div>

        <div className="w-[440px] bg-white border-l border-slate-200 flex flex-col shrink-0 shadow-sm">
          <div className="border-b border-slate-100 bg-white px-4 py-4 space-y-3 shrink-0">
            <div className="text-xs uppercase tracking-wide text-slate-400">页面动作</div>
            <div className="flex flex-wrap gap-2">
              {surface === 'search' ? (
                <>
                  <button disabled={!activePage || searchDisabled} onClick={() => activePage && void runAction(() => generatePageSearchQueries(project.project_id, activePage.page_id))} className="rounded-xl border border-slate-200 px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-40">生成搜索词</button>
                  <button disabled={!activePage || searchDisabled} onClick={() => activePage && void runAction(() => runPageSearch(project.project_id, activePage.page_id, 'page_search_run'))} className="rounded-xl border border-slate-200 px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-40">{activePage?.page_search_results.length ? '补充检索' : '搜索'}</button>
                  <button disabled={!activePage || searchDisabled} onClick={() => activePage && void runAction(() => runPageSearch(project.project_id, activePage.page_id, 'page_search_refresh'))} className="rounded-xl border border-slate-200 px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-40"><RefreshCw size={14} className="inline mr-1" />覆盖重搜</button>
                  <button disabled={!activePage || searchDisabled} onClick={() => activePage && void runAction(() => generatePageSummary(project.project_id, activePage.page_id))} className="rounded-xl bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-40">生成 summary</button>
                </>
              ) : surface === 'draft' ? (
                <button disabled={!activePage} onClick={() => activePage && void runAction(() => generatePageDraft(project.project_id, activePage.page_id))} className="rounded-xl bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-40">生成当前页策划稿</button>
              ) : (
                <button disabled={!activePage} onClick={() => activePage && void runAction(() => generatePageDesign(project.project_id, activePage.page_id))} className="rounded-xl bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-40">生成当前页设计稿</button>
              )}
              <button disabled={!activePage} onClick={() => setIsDataModalOpen(true)} className="rounded-xl border border-slate-200 px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-40"><FileText size={14} className="inline mr-1" />手动编辑原数据</button>
            </div>
          </div>
          <div className="flex-1 overflow-y-auto p-5 space-y-6 bg-slate-50/50">
            {timelineItems.map((item) =>
              item.type === 'run' ? (
                <div key={item.key} className="flex justify-start">
                  <AgentActivityCard
                    run={item.run}
                    onRecommendationClick={(recommendation) => {
                      void sendMessage(recommendation.label);
                    }}
                    recommendationsDisabled={replay.active || isSendingMessage || !activePage}
                  />
                </div>
              ) : item.message.role === 'user' ? (
                <div key={item.key} className="flex justify-end">
                  <div className="bg-blue-50 border border-blue-100 text-blue-800 px-4 py-3 rounded-2xl rounded-tr-sm max-w-[85%] text-sm whitespace-pre-wrap shadow-sm">{item.message.content_md}</div>
                </div>
              ) : (
                <div key={item.key} className="flex justify-start">
                  {agentRunFromMessage(item.message) ? <AgentActivityCard run={{...agentRunFromMessage(item.message)!, content_md: item.message.content_md}} accent="emerald" onRecommendationClick={(recommendation) => { void sendMessage(recommendation.label); }} recommendationsDisabled={replay.active || isSendingMessage || !activePage} /> : <div className="bg-white border border-slate-200 shadow-sm px-5 py-4 rounded-2xl rounded-tl-sm max-w-[95%] w-full"><p className="text-sm text-slate-600 leading-relaxed whitespace-pre-wrap">{item.message.content_md}</p></div>}
                </div>
              ),
            )}
            {!timelineItems.length && !isLoading ? <div className="text-sm text-slate-400 text-center pt-6">当前阶段还没有对话内容。</div> : null}
          </div>
          <div className="p-5 bg-white border-t border-slate-100 space-y-3">
            {error ? <div className="text-sm text-red-600">{error}</div> : null}
            <div className="bg-slate-50 rounded-2xl flex items-end p-2.5 border border-slate-200 focus-within:border-blue-500 focus-within:ring-2 focus-within:ring-blue-100 transition-all">
              <textarea value={chatInput} placeholder={replay.active ? '回放模式已禁用输入' : surface === 'search' ? '例如：把标题改成...，或者先只生成搜索词' : surface === 'draft' ? '例如：重生成这一页策划稿，强调数据对比' : '例如：重生成设计稿，保留结构但增强层次'} className="flex-1 bg-transparent border-none outline-none resize-none max-h-32 min-h-[44px] py-2.5 px-3 text-sm text-slate-700" rows={1} readOnly={replay.active} onChange={(event) => setChatInput(event.target.value)} onKeyDown={(event) => { if (replay.active) return; if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void handleSendMessage(); } }} />
              <button disabled={replay.active || isSendingMessage || !chatInput.trim() || !activePage} onClick={() => void handleSendMessage()} className="p-2.5 text-blue-600 hover:text-blue-700 disabled:text-slate-300 disabled:cursor-not-allowed">{isSendingMessage ? <LoaderCircle size={20} className="animate-spin" /> : <Send size={20} />}</button>
            </div>
            <div className="flex items-center justify-between text-[11px] text-slate-400"><div className="flex items-center gap-1"><Database size={12} />页面上下文已绑定</div><div className="flex items-center gap-1"><Search size={12} />按 Enter 发送，Shift + Enter 换行</div></div>
          </div>
        </div>
      </div>
      )}

      <DataModal
        open={isDataModalOpen}
        surface={surface}
        page={activePage}
        titleDraft={titleDraft}
        bulletDraft={bulletDraft}
        summaryDraft={summaryDraft}
        draftSvgDraft={draftSvgDraft}
        onTitleChange={setTitleDraft}
        onBulletChange={setBulletDraft}
        onSummaryChange={setSummaryDraft}
        onDraftSvgChange={setDraftSvgDraft}
        onSaveOutline={() => void handleSaveOutline()}
        onSaveSummary={() => void handleSaveSummary()}
        onSaveDraft={() => void handleSaveDraft()}
        onClose={() => setIsDataModalOpen(false)}
        isSavingOutline={isSavingOutline}
        isSavingSummary={isSavingSummary}
        isSavingDraft={isSavingDraft}
      />
      {isPresentationOpen ? (
        <PresentationPlayer
          slides={presentationSlides}
          index={presentationIndex}
          surface={presentationSurface}
          onIndexChange={setPresentationIndex}
          onClose={() => setIsPresentationOpen(false)}
        />
      ) : null}
    </div>
  );
}
