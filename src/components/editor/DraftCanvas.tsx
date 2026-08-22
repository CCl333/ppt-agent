import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react';
import type { LayoutBox, LayoutPlan, PageSummary, SceneNode, ScenePatchRequest, VisualSlot } from '../../lib/ppt-api';
import { SvgCanvas } from './EditorBits';

const CANVAS_WIDTH = 1280;
const CANVAS_HEIGHT = 720;
const MIN_BOX = 8;
const HANDLES = ['nw', 'ne', 'sw', 'se'] as const;
type Handle = (typeof HANDLES)[number];

function clonePlan(plan: LayoutPlan | null): LayoutPlan {
  return {
    schema_version: plan?.schema_version,
    canvas: plan?.canvas ? {...plan.canvas} : {width: CANVAS_WIDTH, height: CANVAS_HEIGHT},
    safe_area: plan?.safe_area ? {...plan.safe_area} : undefined,
    nodes: (plan?.nodes ?? []).map((node) => ({
      ...node,
      box: node.box ? {...node.box} : null,
      children: node.children ? [...node.children] : [],
    })),
    reading_order: [...(plan?.reading_order ?? [])],
  };
}

function readLayoutPlan(page: PageSummary | null): LayoutPlan | null {
  const raw = page?.draft?.layout_plan_json;
  if (!raw || typeof raw !== 'object') return null;
  const plan = raw as LayoutPlan;
  if (!Array.isArray(plan.nodes) || plan.nodes.length === 0) return null;
  return plan;
}

function readSlots(page: PageSummary | null): VisualSlot[] {
  const raw = page?.draft?.visual_plan_json;
  if (!raw || typeof raw !== 'object') return [];
  const slots = (raw as {slots?: unknown}).slots;
  if (!Array.isArray(slots)) return [];
  return slots.filter((item): item is VisualSlot => Boolean(item && typeof item === 'object' && 'slot_id' in item));
}

function boxesEqual(left?: LayoutBox | null, right?: LayoutBox | null): boolean {
  if (!left || !right) return left === right;
  return left.x === right.x && left.y === right.y && left.w === right.w && left.h === right.h;
}

function mapChildBox(oldGroup: LayoutBox, nextGroup: LayoutBox, child: LayoutBox): LayoutBox {
  const sx = oldGroup.w ? nextGroup.w / oldGroup.w : 1;
  const sy = oldGroup.h ? nextGroup.h / oldGroup.h : 1;
  return {
    x: Math.round((nextGroup.x + (child.x - oldGroup.x) * sx) * 100) / 100,
    y: Math.round((nextGroup.y + (child.y - oldGroup.y) * sy) * 100) / 100,
    w: Math.max(MIN_BOX, Math.round(child.w * sx * 100) / 100),
    h: Math.max(MIN_BOX, Math.round(child.h * sy * 100) / 100),
  };
}

function clampBox(box: LayoutBox): LayoutBox {
  const w = Math.min(Math.max(box.w, MIN_BOX), CANVAS_WIDTH);
  const h = Math.min(Math.max(box.h, MIN_BOX), CANVAS_HEIGHT);
  return {
    x: Math.min(Math.max(box.x, 0), CANVAS_WIDTH - w),
    y: Math.min(Math.max(box.y, 0), CANVAS_HEIGHT - h),
    w,
    h,
  };
}

function nodeTone(node: SceneNode): string {
  if (node.kind === 'group') return 'border-dashed border-sky-400/80 bg-sky-400/5';
  if (node.kind === 'visual-slot') return 'border-violet-400 bg-violet-400/10';
  if (node.role === 'page-title' || node.role === 'display') return 'border-blue-500 bg-blue-500/5';
  return 'border-amber-400 bg-amber-400/5';
}

function nodeLabel(node: SceneNode): string {
  if (node.role) return `${node.role} · ${node.node_id}`;
  return `${node.kind} · ${node.node_id}`;
}

export default function DraftCanvas({
  page,
  readOnly,
  saving,
  onSave,
  onOpenRaw,
}: {
  page: PageSummary | null;
  readOnly?: boolean;
  saving?: boolean;
  onSave: (payload: ScenePatchRequest) => Promise<void> | void;
  onOpenRaw: () => void;
}) {
  const markup = page?.draft_preview_svg_markup ?? page?.draft?.draft_svg_markup ?? null;
  const sourcePlan = readLayoutPlan(page);
  const sourceSlots = readSlots(page);
  const [plan, setPlan] = useState<LayoutPlan>(() => clonePlan(sourcePlan));
  const [hiddenSlots, setHiddenSlots] = useState<Record<string, boolean>>({});
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const dragCleanupRef = useRef<(() => void) | null>(null);
  const frameRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<{
    nodeId: string;
    handle: Handle | 'move';
    startX: number;
    startY: number;
    origin: Record<string, LayoutBox>;
  } | null>(null);

  useEffect(() => {
    setPlan(clonePlan(sourcePlan));
    const hidden: Record<string, boolean> = {};
    for (const slot of sourceSlots) {
      if (slot.kind === 'none') continue;
      hidden[slot.slot_id] = slot.status === 'skipped';
    }
    setHiddenSlots(hidden);
    setSelectedId(null);
    setError(null);
  }, [page?.draft?.draft_version_id]);

  useEffect(() => {
    return () => {
      dragCleanupRef.current?.();
    };
  }, []);

  const optionalSlots = sourceSlots.filter((slot) => slot.kind !== 'none' && slot.priority !== 'required');
  const nodes = plan.nodes ?? [];
  const selected = nodes.find((node) => node.node_id === selectedId) ?? null;

  const original = useMemo(() => clonePlan(sourcePlan), [page?.draft?.draft_version_id]);
  const dirty = useMemo(() => {
    const origNodes = new Map<string, SceneNode>((original.nodes ?? []).map((node) => [node.node_id, node]));
    if ((plan.nodes ?? []).length !== origNodes.size) return true;
    for (const node of plan.nodes ?? []) {
      const before = origNodes.get(node.node_id);
      if (!before) return true;
      if ((node.text ?? '') !== (before.text ?? '')) return true;
      if (!boxesEqual(node.box, before.box)) return true;
    }
    for (const slot of optionalSlots) {
      const wasHidden = slot.status === 'skipped';
      if (Boolean(hiddenSlots[slot.slot_id]) !== wasHidden) return true;
    }
    return false;
  }, [hiddenSlots, optionalSlots, original.nodes, plan.nodes]);

  const applyBox = (nodeId: string, nextBox: LayoutBox, origin: Record<string, LayoutBox>) => {
    setPlan((current) => {
      const currentNodes = current.nodes ?? [];
      const target = currentNodes.find((node) => node.node_id === nodeId);
      const oldBox = origin[nodeId];
      if (!target || !oldBox) return current;
      const mapped = new Map<string, LayoutBox>();
      mapped.set(nodeId, clampBox(nextBox));
      if (target.kind === 'group') {
        for (const childId of target.children ?? []) {
          const childOrigin = origin[childId];
          if (!childOrigin) continue;
          mapped.set(childId, clampBox(mapChildBox(oldBox, mapped.get(nodeId)!, childOrigin)));
        }
      }
      return {
        ...current,
        nodes: currentNodes.map((node) => (mapped.has(node.node_id) ? {...node, box: mapped.get(node.node_id)} : node)),
      };
    });
  };

  const toCanvasPoint = (event: {clientX: number; clientY: number}) => {
    const rect = frameRef.current?.getBoundingClientRect();
    if (!rect || rect.width === 0) return {x: 0, y: 0};
    return {
      x: ((event.clientX - rect.left) / rect.width) * CANVAS_WIDTH,
      y: ((event.clientY - rect.top) / rect.height) * CANVAS_HEIGHT,
    };
  };

  const onPointerDown = (event: ReactPointerEvent<HTMLElement>, nodeId: string, handle: Handle | 'move') => {
    if (readOnly) return;
    event.preventDefault();
    event.stopPropagation();
    const point = toCanvasPoint(event);
    const origin: Record<string, LayoutBox> = {};
    for (const node of nodes) {
      if (node.box) origin[node.node_id] = {...node.box};
    }
    dragCleanupRef.current?.();
    dragRef.current = {nodeId, handle, startX: point.x, startY: point.y, origin};
    setSelectedId(nodeId);
    const move = (next: PointerEvent) => {
      const drag = dragRef.current;
      if (!drag) return;
      const cursor = toCanvasPoint(next);
      const dx = cursor.x - drag.startX;
      const dy = cursor.y - drag.startY;
      const originBox = drag.origin[drag.nodeId];
      if (!originBox) return;
      let nextBox = {...originBox};
      if (drag.handle === 'move') {
        nextBox = {x: originBox.x + dx, y: originBox.y + dy, w: originBox.w, h: originBox.h};
      } else {
        if (drag.handle.includes('e')) nextBox.w = originBox.w + dx;
        if (drag.handle.includes('s')) nextBox.h = originBox.h + dy;
        if (drag.handle.includes('w')) {
          nextBox.x = originBox.x + dx;
          nextBox.w = originBox.w - dx;
        }
        if (drag.handle.includes('n')) {
          nextBox.y = originBox.y + dy;
          nextBox.h = originBox.h - dy;
        }
      }
      applyBox(drag.nodeId, nextBox, drag.origin);
    };
    const up = () => {
      dragRef.current = null;
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      window.removeEventListener('pointercancel', up);
      if (dragCleanupRef.current === up) {
        dragCleanupRef.current = null;
      }
    };
    dragCleanupRef.current = up;
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', up);
  };

  const handleSave = async () => {
    if (!page?.draft?.draft_version_id || !dirty) return;
    const origNodes = new Map<string, SceneNode>((original.nodes ?? []).map((node) => [node.node_id, node]));
    const textEdits = (plan.nodes ?? [])
      .filter((node) => node.kind === 'text' && (node.text ?? '') !== (origNodes.get(node.node_id)?.text ?? ''))
      .map((node) => ({node_id: node.node_id, text: (node.text ?? '').trim()}));
    if (textEdits.some((item) => !item.text)) {
      setError('文案不能为空');
      return;
    }
    const boxEdits = (plan.nodes ?? [])
      .filter((node) => node.box && !boxesEqual(node.box, origNodes.get(node.node_id)?.box))
      .map((node) => ({node_id: node.node_id, box: node.box as LayoutBox}));
    const slotVisibility = optionalSlots
      .filter((slot) => Boolean(hiddenSlots[slot.slot_id]) !== (slot.status === 'skipped'))
      .map((slot) => ({slot_id: slot.slot_id, visible: !hiddenSlots[slot.slot_id]}));
    setError(null);
    try {
      await onSave({
        base_version_id: page.draft.draft_version_id,
        text_edits: textEdits,
        box_edits: boxEdits,
        slot_visibility: slotVisibility,
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '策划稿保存失败');
    }
  };

  const handleReset = () => {
    setPlan(clonePlan(sourcePlan));
    const hidden: Record<string, boolean> = {};
    for (const slot of sourceSlots) {
      if (slot.kind === 'none') continue;
      hidden[slot.slot_id] = slot.status === 'skipped';
    }
    setHiddenSlots(hidden);
    setError(null);
  };

  if (!markup) {
    return <SvgCanvas markup={null} placeholder="当前页策划稿尚未生成" />;
  }
  if (!sourcePlan) {
    return (
      <div className="space-y-3">
        <div className="rounded-2xl border border-amber-100 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          这一页还没有 LayoutPlan，不能拖拽。请重新生成策划稿，或用原始 SVG 排障。
        </div>
        <SvgCanvas markup={markup} placeholder="当前页策划稿尚未生成" />
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="text-sm text-slate-500">
          {readOnly ? '回放模式只读。' : '拖拽或缩放布局盒会写回策划稿 LayoutPlan，作为后续设计稿的新基准。'}
        </div>
        <div className="flex flex-wrap gap-2">
          <button type="button" onClick={onOpenRaw} className="rounded-xl border border-slate-200 px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50">
            原始 SVG
          </button>
          <button type="button" disabled={readOnly || !dirty || saving} onClick={handleReset} className="rounded-xl border border-slate-200 px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-40">
            放弃更改
          </button>
          <button type="button" disabled={readOnly || !dirty || saving} onClick={() => void handleSave()} className="rounded-xl bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-40">
            {saving ? '保存中...' : '保存布局'}
          </button>
        </div>
      </div>
      {error ? <div className="text-sm text-red-600">{error}</div> : null}
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_240px]">
        <div className="flex min-h-[28rem] items-center justify-center rounded-[2rem] bg-[radial-gradient(circle_at_top,_rgba(148,163,184,0.14),_transparent_55%)] p-4">
          <div
            ref={frameRef}
            className="relative overflow-hidden rounded-xl border border-slate-200 bg-white shadow-xl"
            style={{aspectRatio: '16 / 9', width: '100%'}}
            onClick={() => setSelectedId(null)}
          >
            <div className="pointer-events-none absolute inset-0 [&_svg]:h-full [&_svg]:w-full" dangerouslySetInnerHTML={{__html: markup}} />
            {(plan.nodes ?? [])
              .filter((node) => node.box && !hiddenSlots[node.node_id] && !hiddenSlots[node.visual_slot_id ?? ''])
              .map((node) => {
                const box = node.box as LayoutBox;
                const selectedNode = selectedId === node.node_id;
                return (
                  <div
                    key={node.node_id}
                    className={`absolute rounded-sm border-2 ${nodeTone(node)} ${selectedNode ? 'ring-2 ring-blue-500 ring-offset-1' : ''}`}
                    style={{
                      left: `${(box.x / CANVAS_WIDTH) * 100}%`,
                      top: `${(box.y / CANVAS_HEIGHT) * 100}%`,
                      width: `${(box.w / CANVAS_WIDTH) * 100}%`,
                      height: `${(box.h / CANVAS_HEIGHT) * 100}%`,
                      cursor: readOnly ? 'default' : 'move',
                    }}
                    onPointerDown={(event) => onPointerDown(event, node.node_id, 'move')}
                    onClick={(event) => {
                      event.stopPropagation();
                      setSelectedId(node.node_id);
                    }}
                  >
                    <div className="pointer-events-none absolute left-1 top-1 rounded bg-white/80 px-1 text-[10px] font-medium text-slate-600">
                      {node.role || node.kind}
                    </div>
                    {selectedNode && !readOnly
                      ? HANDLES.map((handle) => (
                          <span
                            key={handle}
                            className={`absolute h-2.5 w-2.5 rounded-sm border border-white bg-blue-600 ${
                              handle === 'nw'
                                ? '-left-1 -top-1 cursor-nwse-resize'
                                : handle === 'ne'
                                ? '-right-1 -top-1 cursor-nesw-resize'
                                : handle === 'sw'
                                ? '-left-1 -bottom-1 cursor-nesw-resize'
                                : '-right-1 -bottom-1 cursor-nwse-resize'
                            }`}
                            onPointerDown={(event) => onPointerDown(event, node.node_id, handle)}
                          />
                        ))
                      : null}
                  </div>
                );
              })}
          </div>
        </div>
        <div className="rounded-[1.5rem] border border-slate-200 bg-white p-4 shadow-sm space-y-4">
          <div>
            <div className="text-xs uppercase tracking-wide text-slate-400">选中节点</div>
            {selected ? (
              <div className="mt-2 space-y-3">
                <div className="text-sm font-semibold text-slate-800">{nodeLabel(selected)}</div>
                {selected.kind === 'text' ? (
                  <textarea
                    value={selected.text ?? ''}
                    disabled={readOnly}
                    rows={4}
                    onChange={(event) => {
                      const value = event.target.value;
                      setPlan((current) => ({
                        ...current,
                        nodes: (current.nodes ?? []).map((node) => (node.node_id === selected.node_id ? {...node, text: value} : node)),
                      }));
                    }}
                    className="w-full rounded-xl border border-slate-200 px-3 py-2 text-sm text-slate-700 outline-none resize-none focus:border-blue-500 disabled:bg-slate-50"
                  />
                ) : (
                  <div className="text-sm text-slate-500">v1 不能增删核心节点，只能移动或缩放这个盒子。</div>
                )}
              </div>
            ) : (
              <div className="mt-2 text-sm text-slate-400">点选画布上的盒子后，可以改文案或调整位置。</div>
            )}
          </div>
          {optionalSlots.length ? (
            <div className="space-y-2">
              <div className="text-xs uppercase tracking-wide text-slate-400">optional 视觉槽</div>
              {optionalSlots.map((slot) => (
                <label key={slot.slot_id} className="flex items-center justify-between gap-2 rounded-xl border border-slate-100 bg-slate-50 px-3 py-2 text-sm">
                  <span className="text-slate-700">{slot.intent || slot.slot_id}</span>
                  <input
                    type="checkbox"
                    disabled={readOnly}
                    checked={!hiddenSlots[slot.slot_id]}
                    onChange={(event) => setHiddenSlots((current) => ({...current, [slot.slot_id]: !event.target.checked}))}
                  />
                </label>
              ))}
            </div>
          ) : (
            <div className="text-xs text-slate-400">这一页没有 optional 视觉槽。</div>
          )}
        </div>
      </div>
    </div>
  );
}
