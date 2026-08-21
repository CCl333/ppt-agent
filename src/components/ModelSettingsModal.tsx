import { useEffect, useRef, useState, type ReactNode } from 'react';
import { ChevronDown, Download, LoaderCircle, Plus, Trash2, X } from 'lucide-react';
import {
  ApiError,
  catalogRemoteModels,
  createModelProvider,
  deleteModelProvider,
  getModelBindings,
  getReaderSettings,
  getSearchSettings,
  listModelProviders,
  patchModelProvider,
  putModelBindings,
  putReaderSettings,
  putSearchSettings,
  testModelProvider,
  type ModelBindingItem,
  type ModelProvider,
  type ModelRole,
  type ReaderMode,
  type ReaderSettings,
  type SearchMode,
  type SearchSettings,
} from '../lib/ppt-api';

const EMPTY_FORM = {
  provider_id: '',
  name: '',
  base_url: '',
  api_key: '',
  model: '',
  api_path: '/chat/completions',
  timeout_seconds: 120,
};

const API_COMPAT_OPTIONS = [
  {value: '/chat/completions', label: 'OpenAI Chat Completions /v1/chat/completions'},
  {value: '/responses', label: 'OpenAI Responses API /v1/responses'},
  {value: '/messages', label: 'Anthropic Messages /v1/messages'},
];

type FormState = typeof EMPTY_FORM;

export default function ModelSettingsModal({
  open,
  onClose,
  onBindingsChange,
}: {
  open: boolean;
  onClose: () => void;
  onBindingsChange?: (needsSetup: boolean) => void;
}) {
  const [providers, setProviders] = useState<ModelProvider[]>([]);
  const [bindings, setBindings] = useState<ModelBindingItem[]>([]);
  const [expertItems, setExpertItems] = useState<ModelBindingItem[]>([]);
  const [expertEnabled, setExpertEnabled] = useState(false);
  const [searchSettings, setSearchSettings] = useState<SearchSettings | null>(null);
  const [readerSettings, setReaderSettings] = useState<ReaderSettings | null>(null);
  const [bochaKey, setBochaKey] = useState('');
  const [searchTavilyKey, setSearchTavilyKey] = useState('');
  const [searchTavilyUrl, setSearchTavilyUrl] = useState('https://api.tavily.com');
  const [tavilyKey, setTavilyKey] = useState('');
  const [firecrawlKey, setFirecrawlKey] = useState('');
  const [tavilyUrl, setTavilyUrl] = useState('https://api.tavily.com');
  const [firecrawlUrl, setFirecrawlUrl] = useState('https://api.firecrawl.dev/v2');
  const [needsSetup, setNeedsSetup] = useState(false);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [isLoading, setIsLoading] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [modelIds, setModelIds] = useState<string[]>([]);
  const [catalogOpen, setCatalogOpen] = useState(false);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const catalogBoxRef = useRef<HTMLDivElement | null>(null);

  const editing = Boolean(form.provider_id);
  const searchBinding = bindings.find((item) => item.role === 'search');
  const stageBindings = bindings;

  useEffect(() => {
    if (!open) {
      return;
    }
    let cancelled = false;
    const load = async () => {
      setIsLoading(true);
      setError(null);
      try {
        const [providerResponse, bindingResponse, searchResponse, readerResponse] = await Promise.all([
          listModelProviders(),
          getModelBindings(),
          getSearchSettings(),
          getReaderSettings(),
        ]);
        if (cancelled) {
          return;
        }
        setProviders(providerResponse.items);
        setBindings(bindingResponse.items);
        setExpertItems(bindingResponse.expert_items ?? []);
        setExpertEnabled(Boolean(bindingResponse.expert_enabled));
        setSearchSettings(searchResponse);
        setReaderSettings(readerResponse);
        setSearchTavilyUrl(searchResponse.tavily_api_url || 'https://api.tavily.com');
        setTavilyUrl(readerResponse.tavily_api_url || 'https://api.tavily.com');
        setFirecrawlUrl(readerResponse.firecrawl_api_url || 'https://api.firecrawl.dev/v2');
        setNeedsSetup(bindingResponse.needs_setup);
        onBindingsChange?.(bindingResponse.needs_setup);
      } catch (caughtError) {
        if (!cancelled) {
          setError(caughtError instanceof Error ? caughtError.message : '模型配置读取失败');
        }
      } finally {
        if (!cancelled) {
          setIsLoading(false);
        }
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [open, onBindingsChange]);

  useEffect(() => {
    setModelIds([]);
    setCatalogOpen(false);
  }, [form.base_url, form.api_key, form.api_path, form.provider_id]);

  useEffect(() => {
    if (!catalogOpen) {
      return;
    }
    const onPointerDown = (event: MouseEvent) => {
      if (catalogBoxRef.current && !catalogBoxRef.current.contains(event.target as Node)) {
        setCatalogOpen(false);
      }
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [catalogOpen]);

  if (!open) {
    return null;
  }

  const applyBindingState = (next: {items: ModelBindingItem[]; needs_setup: boolean; expert_enabled?: boolean; expert_items?: ModelBindingItem[]}) => {
    setBindings(next.items);
    setExpertItems(next.expert_items ?? []);
    setExpertEnabled(Boolean(next.expert_enabled));
    setNeedsSetup(next.needs_setup);
    onBindingsChange?.(next.needs_setup);
  };

  const resetForm = () => {
    setForm(EMPTY_FORM);
    setModelIds([]);
    setCatalogOpen(false);
    setNotice(null);
  };

  const handleSaveProvider = async () => {
    if (!form.name.trim() || !form.base_url.trim() || !form.model.trim() || (!editing && !form.api_key.trim())) {
      setError('名称、Base URL、模型 ID 和 API Key 均为必填');
      return;
    }
    setIsSaving(true);
    setError(null);
    setNotice(null);
    try {
      const payload = {
        name: form.name.trim(),
        base_url: form.base_url.trim(),
        model: form.model.trim(),
        api_path: form.api_path,
        timeout_seconds: Number(form.timeout_seconds) || 120,
        ...(form.api_key.trim() ? {api_key: form.api_key.trim()} : {}),
      };
      const saved = editing
        ? await patchModelProvider(form.provider_id, payload)
        : await createModelProvider({...payload, api_key: form.api_key.trim()});
      const nextProviders = await listModelProviders();
      setProviders(nextProviders.items);
      resetForm();
      setNotice(editing ? `已更新 ${saved.name}` : `已导入 ${saved.name}`);
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : '保存模型失败');
    } finally {
      setIsSaving(false);
    }
  };

  const handleEdit = (provider: ModelProvider) => {
    setForm({
      provider_id: provider.provider_id,
      name: provider.name,
      base_url: provider.base_url,
      api_key: '',
      model: provider.model,
      api_path: canonicalApiPath(provider.api_path),
      timeout_seconds: provider.timeout_seconds,
    });
    setNotice(`正在编辑 ${provider.name}，API Key 留空表示不修改`);
    setError(null);
  };

  const handleDelete = async (provider: ModelProvider) => {
    setIsSaving(true);
    setError(null);
    try {
      await deleteModelProvider(provider.provider_id);
      const [providerResponse, bindingResponse] = await Promise.all([listModelProviders(), getModelBindings()]);
      setProviders(providerResponse.items);
      applyBindingState(bindingResponse);
      if (form.provider_id === provider.provider_id) {
        resetForm();
      }
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : '删除模型失败');
    } finally {
      setIsSaving(false);
    }
  };

  const handleTest = async (providerId: string) => {
    setTestingId(providerId);
    setError(null);
    setNotice(null);
    try {
      const result = await testModelProvider(providerId);
      setNotice(`${result.model} 连通正常，${result.latency_ms}ms`);
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : '连通性测试失败');
    } finally {
      setTestingId(null);
    }
  };

  const canFetchCatalog = Boolean(form.base_url.trim() && (form.api_key.trim() || editing));
  const filteredModelIds = modelIds.filter((item) => {
    const query = form.model.trim().toLowerCase();
    return !query || item.toLowerCase().includes(query);
  });

  const handleFetchCatalog = async () => {
    if (!canFetchCatalog) {
      setError('请先填写 Base URL 和 API Key');
      return;
    }
    setCatalogLoading(true);
    setError(null);
    setNotice(null);
    try {
      const result = await catalogRemoteModels({
        base_url: form.base_url.trim(),
        api_path: form.api_path,
        ...(form.api_key.trim() ? {api_key: form.api_key.trim()} : {}),
        ...(editing ? {provider_id: form.provider_id} : {}),
      });
      setModelIds(result.items);
      setCatalogOpen(true);
      if (!form.model.trim() && result.items[0]) {
        setForm((prev) => ({...prev, model: result.items[0]}));
      }
      setNotice(`已拉取 ${result.items.length} 个模型 ID`);
    } catch (caughtError) {
      setModelIds([]);
      setCatalogOpen(false);
      setError(caughtError instanceof ApiError ? caughtError.message : '拉取模型列表失败');
    } finally {
      setCatalogLoading(false);
    }
  };

  const handleBind = async (role: ModelRole, providerId: string) => {
    setError(null);
    try {
      const next = await putModelBindings({[role]: providerId || null});
      applyBindingState(next);
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : '角色绑定失败');
    }
  };

  const handleExpertBind = async (role: ModelRole, providerId: string) => {
    setError(null);
    try {
      const next = await putModelBindings({expert: {[role]: providerId || null}});
      applyBindingState(next);
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : '专家档绑定失败');
    }
  };

  const handleExpertToggle = async (enabled: boolean) => {
    setError(null);
    try {
      const next = await putModelBindings({expert_enabled: enabled});
      applyBindingState(next);
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : '专家档切换失败');
    }
  };

  const handleCopyToExpert = async () => {
    setError(null);
    try {
      const expert = Object.fromEntries(bindings.map((item) => [item.role, item.provider_id])) as Partial<Record<ModelRole, string | null>>;
      const next = await putModelBindings({expert_enabled: true, expert});
      applyBindingState(next);
      setNotice('已用当前常规绑定填充专家档');
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : '填充专家档失败');
    }
  };

  const refreshSetup = async () => {
    const next = await getModelBindings();
    applyBindingState(next);
  };

  const handleSearchMode = async (mode: SearchMode) => {
    setError(null);
    try {
      const next = await putSearchSettings({mode});
      setSearchSettings(next);
      await refreshSetup();
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : '搜索方式保存失败');
    }
  };

  const handleSaveBochaKey = async () => {
    if (!bochaKey.trim()) {
      setError('请输入博查 Key，留空不会修改已保存的值');
      return;
    }
    setIsSaving(true);
    setError(null);
    setNotice(null);
    try {
      const next = await putSearchSettings({
        mode: 'bocha',
        bocha_auth_header: bochaKey.trim(),
      });
      setSearchSettings(next);
      setBochaKey('');
      setNotice('博查 Key 已保存');
      await refreshSetup();
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : '博查 Key 保存失败');
    } finally {
      setIsSaving(false);
    }
  };

  const handleSaveSearchTavily = async () => {
    if (!searchSettings?.tavily_configured && !searchTavilyKey.trim()) {
      setError('请输入 Tavily Key');
      return;
    }
    setIsSaving(true);
    setError(null);
    setNotice(null);
    try {
      const payload: {mode: SearchMode; tavily_api_key?: string; tavily_api_url?: string} = {
        mode: 'tavily',
      };
      if (searchTavilyKey.trim()) {
        payload.tavily_api_key = searchTavilyKey.trim();
      }
      if (searchTavilyUrl.trim()) {
        payload.tavily_api_url = searchTavilyUrl.trim();
      }
      const next = await putSearchSettings(payload);
      setSearchSettings(next);
      setSearchTavilyKey('');
      setSearchTavilyUrl(next.tavily_api_url);
      setNotice('Tavily 搜索配置已保存');
      await refreshSetup();
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : 'Tavily 搜索配置保存失败');
    } finally {
      setIsSaving(false);
    }
  };

  const handleReaderMode = async (mode: ReaderMode) => {
    setError(null);
    try {
      const next = await putReaderSettings({mode});
      setReaderSettings(next);
      await refreshSetup();
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : '解析方式保存失败');
    }
  };

  const handleSaveReaderKeys = async () => {
    const payload: {
      mode?: ReaderMode;
      tavily_api_key?: string;
      tavily_api_url?: string;
      firecrawl_api_key?: string;
      firecrawl_api_url?: string;
    } = {
      mode: readerSettings?.mode,
    };
    if (tavilyKey.trim()) {
      payload.tavily_api_key = tavilyKey.trim();
    }
    if (tavilyUrl.trim()) {
      payload.tavily_api_url = tavilyUrl.trim();
    }
    if (firecrawlKey.trim()) {
      payload.firecrawl_api_key = firecrawlKey.trim();
    }
    if (firecrawlUrl.trim()) {
      payload.firecrawl_api_url = firecrawlUrl.trim();
    }
    if (
      (readerSettings?.mode === 'tavily' && !readerSettings.tavily_configured && !tavilyKey.trim()) ||
      (readerSettings?.mode === 'firecrawl' && !readerSettings.firecrawl_configured && !firecrawlKey.trim()) ||
      (readerSettings?.mode === 'web_fetch' &&
        !readerSettings.tavily_configured &&
        !readerSettings.firecrawl_configured &&
        !tavilyKey.trim() &&
        !firecrawlKey.trim())
    ) {
      setError('请至少填写当前解析方式所需的 Key');
      return;
    }
    setIsSaving(true);
    setError(null);
    setNotice(null);
    try {
      const next = await putReaderSettings(payload);
      setReaderSettings(next);
      setTavilyKey('');
      setFirecrawlKey('');
      setTavilyUrl(next.tavily_api_url);
      setFirecrawlUrl(next.firecrawl_api_url);
      setNotice('解析配置已保存');
      await refreshSetup();
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : '解析配置保存失败');
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 bg-slate-900/35 backdrop-blur-sm flex items-center justify-center p-6">
      <div className="w-full max-w-4xl max-h-[90vh] overflow-hidden rounded-[2rem] bg-white shadow-2xl border border-slate-200 flex flex-col">
        <div className="px-6 py-5 border-b border-slate-100 flex items-center justify-between">
          <div>
            <div className="text-xs uppercase tracking-wide text-slate-400">Settings / Models</div>
            <div className="text-xl font-semibold text-slate-800">模型配置</div>
          </div>
          <button onClick={onClose} className="rounded-full border border-slate-200 p-2 text-slate-500 hover:text-slate-800 hover:bg-slate-50">
            <X size={18} />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-6 space-y-6 bg-slate-50/50">
          {needsSetup ? (
            <div className="rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
              尚未完成内容策划 / 初稿 / 设计{expertEnabled ? '（含专家档）' : ''}、搜索或解析配置。请先导入模型，再完成角色绑定。
            </div>
          ) : null}
          {error ? <div className="rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{error}</div> : null}
          {notice ? <div className="rounded-2xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-700">{notice}</div> : null}

          <section className="rounded-[2rem] border border-slate-200 bg-white p-6 shadow-sm space-y-4">
            <div className="flex items-center justify-between gap-3">
              <div>
                <h3 className="text-lg font-semibold text-slate-800">模型库</h3>
                <p className="text-sm text-slate-400 mt-1">导入后才能绑定到资料检索 / 内容策划 / 初稿布局 / 最终设计。API Key 只显示掩码。</p>
              </div>
              {editing ? (
                <button onClick={resetForm} className="rounded-xl border border-slate-200 px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50">
                  新建导入
                </button>
              ) : null}
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <Field label="名称">
                <input value={form.name} onChange={(event) => setForm((prev) => ({...prev, name: event.target.value}))} placeholder="例如 gpt-4o-mini @ 中转A" className={inputClass} />
              </Field>
              <Field label="模型 ID">
                <div className="flex gap-2">
                  <div ref={catalogBoxRef} className="relative min-w-0 flex-1">
                    <input
                      value={form.model}
                      onChange={(event) => setForm((prev) => ({...prev, model: event.target.value}))}
                      onFocus={() => {
                        if (modelIds.length) {
                          setCatalogOpen(true);
                        }
                      }}
                      placeholder={modelIds.length ? '输入或从下拉列表选择' : 'gpt-4o-mini'}
                      className={`${inputClass} pr-10`}
                    />
                    <button
                      type="button"
                      onClick={() => modelIds.length && setCatalogOpen((openMenu) => !openMenu)}
                      disabled={!modelIds.length}
                      className="absolute right-2 top-1/2 -translate-y-1/2 rounded-md p-1 text-slate-400 hover:text-slate-700 disabled:opacity-30"
                      aria-label="打开模型列表"
                    >
                      <ChevronDown size={16} />
                    </button>
                    {catalogOpen && modelIds.length ? (
                      <ul className="absolute z-20 mt-1 max-h-52 w-full overflow-y-auto rounded-xl border border-slate-200 bg-white py-1 shadow-lg">
                        {(filteredModelIds.length ? filteredModelIds : modelIds).map((item) => (
                          <li key={item}>
                            <button
                              type="button"
                              onClick={() => {
                                setForm((prev) => ({...prev, model: item}));
                                setCatalogOpen(false);
                              }}
                              className={`w-full px-3 py-2 text-left text-sm hover:bg-slate-50 ${item === form.model ? 'bg-blue-50 text-blue-700' : 'text-slate-700'}`}
                            >
                              {item}
                            </button>
                          </li>
                        ))}
                      </ul>
                    ) : null}
                  </div>
                  <button
                    type="button"
                    onClick={() => {
                      void handleFetchCatalog();
                    }}
                    disabled={!canFetchCatalog || catalogLoading}
                    className="shrink-0 h-[42px] rounded-xl border border-slate-200 bg-white px-3 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50 flex items-center gap-1.5"
                  >
                    {catalogLoading ? <LoaderCircle size={14} className="animate-spin" /> : <Download size={14} />}
                    拉取
                  </button>
                </div>
                <p className="text-[11px] text-slate-400">填写 Base URL 和 API Key 后可拉取该服务商的模型 ID 列表。</p>
              </Field>
              <Field label="Base URL">
                <input value={form.base_url} onChange={(event) => setForm((prev) => ({...prev, base_url: event.target.value}))} placeholder="https://api.openai.com/v1" className={inputClass} />
              </Field>
              <Field label={editing ? 'API Key（留空表示不修改）' : 'API Key'}>
                <input type="password" value={form.api_key} onChange={(event) => setForm((prev) => ({...prev, api_key: event.target.value}))} placeholder={editing ? '已保存，输入新值才会覆盖' : 'sk-...'} className={inputClass} />
              </Field>
              <Field label="API 兼容格式">
                <select value={canonicalApiPath(form.api_path)} onChange={(event) => setForm((prev) => ({...prev, api_path: event.target.value}))} className={inputClass}>
                  {API_COMPAT_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="超时（秒）">
                <input type="number" min={5} max={600} value={form.timeout_seconds} onChange={(event) => setForm((prev) => ({...prev, timeout_seconds: Number(event.target.value)}))} className={inputClass} />
              </Field>
            </div>
            <div className="flex justify-end">
              <button
                onClick={() => {
                  void handleSaveProvider();
                }}
                disabled={isSaving}
                className="h-11 rounded-xl bg-blue-600 px-5 text-white font-medium hover:bg-blue-700 disabled:bg-blue-300 flex items-center gap-2"
              >
                {isSaving ? <LoaderCircle size={16} className="animate-spin" /> : <Plus size={16} />}
                {editing ? '保存修改' : '导入模型'}
              </button>
            </div>

            <div className="space-y-3">
              {isLoading ? <div className="text-sm text-slate-500">正在读取模型库...</div> : null}
              {!isLoading && providers.length === 0 ? <div className="text-sm text-slate-500">还没有导入模型。</div> : null}
              {providers.map((provider) => (
                <div key={provider.provider_id} className="rounded-2xl border border-slate-100 bg-slate-50/80 px-4 py-3 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                  <div className="min-w-0">
                    <div className="font-medium text-slate-800 truncate">{provider.name}</div>
                    <div className="text-xs text-slate-400 mt-1 truncate">
                      {provider.model} · {provider.base_url} · {provider.api_key_masked}
                    </div>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <button onClick={() => handleEdit(provider)} className="rounded-xl border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-50">
                      编辑
                    </button>
                    <button
                      onClick={() => {
                        void handleTest(provider.provider_id);
                      }}
                      disabled={testingId === provider.provider_id}
                      className="rounded-xl border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-50"
                    >
                      {testingId === provider.provider_id ? '测试中...' : '测试连接'}
                    </button>
                    <button
                      onClick={() => {
                        void handleDelete(provider);
                      }}
                      className="rounded-xl border border-red-100 bg-white px-3 py-1.5 text-sm text-red-600 hover:bg-red-50"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </section>

          <section className="rounded-[2rem] border border-slate-200 bg-white p-6 shadow-sm space-y-4">
            <div>
              <h3 className="text-lg font-semibold text-slate-800">分阶段模型</h3>
              <p className="text-sm text-slate-400 mt-1">检索可用便宜模型，最终设计用最强模型。切换后立即生效，无需重启后端。</p>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {stageBindings.map((item) => (
                <div key={item.role}>
                  <Field label={item.label}>
                    <select
                      value={item.provider_id ?? ''}
                      onChange={(event) => {
                        void handleBind(item.role, event.target.value);
                      }}
                      className={inputClass}
                    >
                      <option value="">未绑定</option>
                      {providers.map((provider) => (
                        <option key={provider.provider_id} value={provider.provider_id}>
                          {provider.name}
                        </option>
                      ))}
                    </select>
                    {item.hint ? <p className="text-[11px] text-slate-400">{item.hint}</p> : null}
                  </Field>
                </div>
              ))}
            </div>
          </section>

          <section className="rounded-[2rem] border border-slate-200 bg-white p-6 shadow-sm space-y-4">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h3 className="text-lg font-semibold text-slate-800">专家档</h3>
                <p className="text-sm text-slate-400 mt-1">启用后生成改走这套绑定，不再用上面的常规档。适合最终设计上最强模型，部分步骤约 x3 消耗。</p>
              </div>
              <button
                type="button"
                onClick={() => {
                  void handleExpertToggle(!expertEnabled);
                }}
                className={`shrink-0 rounded-full border px-3 py-1.5 text-xs font-semibold ${
                  expertEnabled ? 'border-amber-300 bg-amber-50 text-amber-800' : 'border-slate-200 text-slate-600 hover:bg-slate-50'
                }`}
              >
                {expertEnabled ? '已启用' : '未启用'}
              </button>
            </div>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => {
                  void handleCopyToExpert();
                }}
                className="rounded-xl border border-slate-200 px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
              >
                用当前常规绑定填充
              </button>
            </div>
            {expertEnabled ? (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {expertItems.map((item) => (
                  <div key={`expert-${item.role}`}>
                    <Field label={`专家 · ${item.label}`}>
                      <select
                        value={item.provider_id ?? ''}
                        onChange={(event) => {
                          void handleExpertBind(item.role, event.target.value);
                        }}
                        className={inputClass}
                      >
                        <option value="">未绑定</option>
                        {providers.map((provider) => (
                          <option key={provider.provider_id} value={provider.provider_id}>
                            {provider.name}
                          </option>
                        ))}
                      </select>
                    </Field>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-sm text-slate-400">启用后才会使用专家档；未启用时上面的常规绑定继续生效。</div>
            )}
          </section>

          <section className="rounded-[2rem] border border-slate-200 bg-white p-6 shadow-sm space-y-4">
            <div>
              <h3 className="text-lg font-semibold text-slate-800">搜索配置</h3>
              <p className="text-sm text-slate-400 mt-1">
                博查和 Tavily 走独立搜索 API，返回真实网页链接。大模型搜索只有上游真正执行联网检索时才可用。
              </p>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              <button
                type="button"
                onClick={() => {
                  void handleSearchMode('bocha');
                }}
                className={`rounded-2xl border px-4 py-3 text-left ${
                  searchSettings?.mode === 'bocha'
                    ? 'border-blue-300 bg-blue-50 text-blue-800'
                    : 'border-slate-200 bg-white text-slate-700 hover:bg-slate-50'
                }`}
              >
                <div className="font-medium">博查 Key 搜索</div>
                <div className="text-xs mt-1 opacity-80">调用博查网页搜索接口，不占用文本模型。</div>
              </button>
              <button
                type="button"
                onClick={() => {
                  void handleSearchMode('tavily');
                }}
                className={`rounded-2xl border px-4 py-3 text-left ${
                  searchSettings?.mode === 'tavily'
                    ? 'border-blue-300 bg-blue-50 text-blue-800'
                    : 'border-slate-200 bg-white text-slate-700 hover:bg-slate-50'
                }`}
              >
                <div className="font-medium">Tavily 搜索</div>
                <div className="text-xs mt-1 opacity-80">调用 Tavily Search，返回可核验的网页结果。</div>
              </button>
              <button
                type="button"
                onClick={() => {
                  void handleSearchMode('llm');
                }}
                className={`rounded-2xl border px-4 py-3 text-left ${
                  searchSettings?.mode === 'llm'
                    ? 'border-blue-300 bg-blue-50 text-blue-800'
                    : 'border-slate-200 bg-white text-slate-700 hover:bg-slate-50'
                }`}
              >
                <div className="font-medium">大模型搜索</div>
                <div className="text-xs mt-1 opacity-80">使用模型库中已导入、支持实时网页搜索的模型。</div>
              </button>
            </div>
            {searchSettings?.mode === 'bocha' ? (
              <div className="space-y-3">
                <Field label={searchSettings.bocha_configured ? '博查 Key（留空表示不修改）' : '博查 Key'}>
                  <input
                    type="password"
                    value={bochaKey}
                    onChange={(event) => setBochaKey(event.target.value)}
                    placeholder={
                      searchSettings.bocha_auth_header_masked
                        ? `当前 ${searchSettings.bocha_auth_header_masked}`
                        : 'Bearer sk-... 或直接粘贴 Key'
                    }
                    className={inputClass}
                  />
                </Field>
                <div className="flex items-center justify-between gap-3">
                  <p className="text-[11px] text-slate-400">
                    {searchSettings.bocha_from_env
                      ? '当前使用 .env 中的博查 Key，保存后以页面配置为准。'
                      : searchSettings.bocha_configured
                        ? '已保存博查 Key，输入新值才会覆盖。'
                        : '填写后立即生效，无需重启后端。'}
                  </p>
                  <button
                    type="button"
                    onClick={() => {
                      void handleSaveBochaKey();
                    }}
                    disabled={isSaving}
                    className="h-10 shrink-0 rounded-xl bg-blue-600 px-4 text-sm text-white font-medium hover:bg-blue-700 disabled:bg-blue-300"
                  >
                    保存密钥
                  </button>
                </div>
              </div>
            ) : null}
            {searchSettings?.mode === 'tavily' ? (
              <div className="space-y-3">
                <Field label={searchSettings.tavily_configured ? 'Tavily Key（留空表示不修改）' : 'Tavily Key'}>
                  <input
                    type="password"
                    value={searchTavilyKey}
                    onChange={(event) => setSearchTavilyKey(event.target.value)}
                    placeholder={
                      searchSettings.tavily_api_key_masked
                        ? `当前 ${searchSettings.tavily_api_key_masked}`
                        : 'tvly-...'
                    }
                    className={inputClass}
                  />
                </Field>
                <Field label="Tavily API URL">
                  <input
                    value={searchTavilyUrl}
                    onChange={(event) => setSearchTavilyUrl(event.target.value)}
                    placeholder="https://api.tavily.com"
                    className={inputClass}
                  />
                </Field>
                <div className="flex items-center justify-between gap-3">
                  <p className="text-[11px] text-slate-400">
                    {searchSettings.tavily_from_env
                      ? '当前使用 .env 中的 Tavily Key，保存后以页面配置为准。'
                      : searchSettings.tavily_configured
                        ? '已保存 Tavily Key，输入新值才会覆盖。'
                        : '填写后立即生效，无需重启后端。'}
                  </p>
                  <button
                    type="button"
                    onClick={() => {
                      void handleSaveSearchTavily();
                    }}
                    disabled={isSaving}
                    className="h-10 shrink-0 rounded-xl bg-blue-600 px-4 text-sm text-white font-medium hover:bg-blue-700 disabled:bg-blue-300"
                  >
                    保存密钥
                  </button>
                </div>
              </div>
            ) : null}
            {searchSettings?.mode === 'llm' ? (
              <Field label="搜索模型">
                <select
                  value={searchBinding?.provider_id ?? ''}
                  onChange={(event) => {
                    void handleBind('search', event.target.value);
                  }}
                  className={inputClass}
                >
                  <option value="">未绑定</option>
                  {providers.map((provider) => (
                    <option key={provider.provider_id} value={provider.provider_id}>
                      {provider.name}
                    </option>
                  ))}
                </select>
                <p className="text-[11px] text-slate-400">
                  需要上游真正执行 web_search。当前 Grok 中转通常只会让模型口头搜索，请改用 Tavily 或博查。
                </p>
              </Field>
            ) : null}
          </section>

          <section className="rounded-[2rem] border border-slate-200 bg-white p-6 shadow-sm space-y-4">
            <div>
              <h3 className="text-lg font-semibold text-slate-800">解析配置</h3>
              <p className="text-sm text-slate-400 mt-1">
                搜索只返回链接和摘要。抓取网页全文时不再走 Jina，可选用 Tavily / Firecrawl，或 grok-search 的 web_fetch（Tavily 优先，失败降级 Firecrawl）。
              </p>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              <button
                type="button"
                onClick={() => {
                  void handleReaderMode('tavily');
                }}
                className={`rounded-2xl border px-4 py-3 text-left ${
                  readerSettings?.mode === 'tavily'
                    ? 'border-blue-300 bg-blue-50 text-blue-800'
                    : 'border-slate-200 bg-white text-slate-700 hover:bg-slate-50'
                }`}
              >
                <div className="font-medium">Tavily</div>
                <div className="text-xs mt-1 opacity-80">调用 Tavily Extract 抽取 Markdown 全文。</div>
              </button>
              <button
                type="button"
                onClick={() => {
                  void handleReaderMode('firecrawl');
                }}
                className={`rounded-2xl border px-4 py-3 text-left ${
                  readerSettings?.mode === 'firecrawl'
                    ? 'border-blue-300 bg-blue-50 text-blue-800'
                    : 'border-slate-200 bg-white text-slate-700 hover:bg-slate-50'
                }`}
              >
                <div className="font-medium">Firecrawl</div>
                <div className="text-xs mt-1 opacity-80">调用 Firecrawl Scrape 抽取 Markdown 全文。</div>
              </button>
              <button
                type="button"
                onClick={() => {
                  void handleReaderMode('web_fetch');
                }}
                className={`rounded-2xl border px-4 py-3 text-left ${
                  readerSettings?.mode === 'web_fetch'
                    ? 'border-blue-300 bg-blue-50 text-blue-800'
                    : 'border-slate-200 bg-white text-slate-700 hover:bg-slate-50'
                }`}
              >
                <div className="font-medium">grok-search web_fetch</div>
                <div className="text-xs mt-1 opacity-80">与本机 grok-search MCP 相同：Tavily → Firecrawl。</div>
              </button>
            </div>
            {readerSettings?.mode !== 'firecrawl' ? (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <Field label={readerSettings?.tavily_configured ? 'Tavily Key（留空表示不修改）' : 'Tavily Key'}>
                  <input
                    type="password"
                    value={tavilyKey}
                    onChange={(event) => setTavilyKey(event.target.value)}
                    placeholder={
                      readerSettings?.tavily_api_key_masked
                        ? `当前 ${readerSettings.tavily_api_key_masked}`
                        : 'tvly-...'
                    }
                    className={inputClass}
                  />
                </Field>
                <Field label="Tavily API URL">
                  <input
                    value={tavilyUrl}
                    onChange={(event) => setTavilyUrl(event.target.value)}
                    placeholder="https://api.tavily.com"
                    className={inputClass}
                  />
                </Field>
              </div>
            ) : null}
            {readerSettings?.mode !== 'tavily' ? (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <Field label={readerSettings?.firecrawl_configured ? 'Firecrawl Key（留空表示不修改）' : 'Firecrawl Key'}>
                  <input
                    type="password"
                    value={firecrawlKey}
                    onChange={(event) => setFirecrawlKey(event.target.value)}
                    placeholder={
                      readerSettings?.firecrawl_api_key_masked
                        ? `当前 ${readerSettings.firecrawl_api_key_masked}`
                        : 'fc-...'
                    }
                    className={inputClass}
                  />
                </Field>
                <Field label="Firecrawl API URL">
                  <input
                    value={firecrawlUrl}
                    onChange={(event) => setFirecrawlUrl(event.target.value)}
                    placeholder="https://api.firecrawl.dev/v2"
                    className={inputClass}
                  />
                </Field>
              </div>
            ) : null}
            <div className="flex items-center justify-between gap-3">
              <p className="text-[11px] text-slate-400">
                {readerSettings?.tavily_from_env || readerSettings?.firecrawl_from_env
                  ? '当前有 Key 来自 .env，保存后以页面配置为准。'
                  : readerSettings?.ready
                    ? '已保存解析 Key，输入新值才会覆盖。'
                    : '填写后立即生效，无需重启后端。'}
              </p>
              <button
                type="button"
                onClick={() => {
                  void handleSaveReaderKeys();
                }}
                disabled={isSaving}
                className="h-10 shrink-0 rounded-xl bg-blue-600 px-4 text-sm text-white font-medium hover:bg-blue-700 disabled:bg-blue-300"
              >
                保存解析配置
              </button>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}

const inputClass =
  'w-full rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm text-slate-800 outline-none focus:border-blue-300 focus:ring-2 focus:ring-blue-100';

function canonicalApiPath(path: string): string {
  const value = path.trim();
  if (['/chat/completions', 'chat/completions', '/v1/chat/completions', 'v1/chat/completions'].includes(value)) {
    return '/chat/completions';
  }
  if (['/responses', 'responses', '/v1/responses', 'v1/responses'].includes(value)) {
    return '/responses';
  }
  if (['/messages', 'messages', '/v1/messages', 'v1/messages'].includes(value)) {
    return '/messages';
  }
  return '/chat/completions';
}

function Field({label, children}: {label: string; children: ReactNode}) {
  return (
    <div className="block space-y-1.5">
      <span className="text-xs font-medium text-slate-500">{label}</span>
      {children}
    </div>
  );
}
