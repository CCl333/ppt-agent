import { useEffect, useMemo, useRef, useState } from 'react';
import { listProjectEvents, type ProjectEvent, type ProjectMessage } from '../lib/ppt-api';
import { reduceAgentRunMap, type AgentRunView } from './AgentActivity';

/** 暂时隐藏回放入口；改 `true` 即可恢复顶栏 1x/2x/4x。 */
export const REPLAY_UI_ENABLED = false;

export type ReplaySpeed = 1 | 2 | 4;

export interface ProjectReplay {
  active: boolean;
  playing: boolean;
  speed: ReplaySpeed;
  cursor: number;
  total: number;
  runs: Record<string, AgentRunView>;
  visibleMessages: ProjectMessage[];
  error: string | null;
  start: () => Promise<void>;
  stop: () => void;
  setSpeed: (speed: ReplaySpeed) => void;
}

function clampDelay(ms: number): number {
  return Math.min(900, Math.max(40, ms));
}

export function useProjectReplay(projectId: string, messages: ProjectMessage[]): ProjectReplay {
  const [active, setActive] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<ReplaySpeed>(1);
  const [cursor, setCursor] = useState(0);
  const [events, setEvents] = useState<ProjectEvent[]>([]);
  const [runs, setRuns] = useState<Record<string, AgentRunView>>({});
  const [error, setError] = useState<string | null>(null);
  const appliedRef = useRef(-1);

  const start = async () => {
    const collected: ProjectEvent[] = [];
    let afterId = 0;
    for (let page = 0; page < 20; page += 1) {
      const response = await listProjectEvents(projectId, {afterId, limit: 200});
      if (!response.items.length) {
        break;
      }
      collected.push(...response.items);
      afterId = response.items[response.items.length - 1].stream_id;
      if (response.items.length < 200) {
        break;
      }
    }
    appliedRef.current = -1;
    setEvents(collected);
    setRuns({});
    setCursor(0);
    setActive(true);
    setPlaying(collected.length > 0);
    setError(collected.length ? null : '还没有可回放的事件');
  };

  const stop = () => {
    setActive(false);
    setPlaying(false);
    setCursor(0);
    setRuns({});
    setEvents([]);
    appliedRef.current = -1;
    setError(null);
  };

  useEffect(() => {
    if (!active || !playing) {
      return;
    }
    if (cursor >= events.length) {
      setPlaying(false);
      return;
    }
    if (appliedRef.current !== cursor) {
      setRuns((current) => reduceAgentRunMap(current, events[cursor]));
      appliedRef.current = cursor;
    }
    const next = events[cursor + 1];
    if (!next) {
      setPlaying(false);
      return;
    }
    const delta = Date.parse(next.created_at) - Date.parse(events[cursor].created_at);
    const delay = clampDelay((Number.isFinite(delta) ? Math.max(delta, 40) : 240) / speed);
    const timer = window.setTimeout(() => {
      setCursor((current) => current + 1);
    }, delay);
    return () => {
      window.clearTimeout(timer);
    };
  }, [active, playing, cursor, events, speed]);

  const visibleMessages = useMemo(() => {
    if (!active) {
      return messages;
    }
    const currentEvent = events[Math.min(cursor, Math.max(events.length - 1, 0))];
    if (!currentEvent) {
      return [];
    }
    const stamp = Date.parse(currentEvent.created_at);
    return messages.filter((message) => Date.parse(message.created_at) <= stamp);
  }, [active, cursor, events, messages]);

  return {
    active,
    playing,
    speed,
    cursor,
    total: events.length,
    runs,
    visibleMessages,
    error,
    start,
    stop,
    setSpeed,
  };
}

export function ReplayBar({replay}: {replay: ProjectReplay}) {
  if (!REPLAY_UI_ENABLED) {
    return null;
  }
  return (
    <div className="flex items-center justify-between gap-4 border-b border-slate-200 bg-white px-6 py-2 text-xs">
      <div className="flex items-center gap-2">
        {([1, 2, 4] as const).map((item) => (
          <button
            key={item}
            type="button"
            onClick={() => {
              replay.setSpeed(item);
            }}
            className={`rounded-lg px-2.5 py-1 font-semibold ${
              replay.speed === item
                ? 'bg-slate-900 text-white'
                : 'border border-slate-200 text-slate-500 hover:text-slate-800'
            }`}
          >
            {item}x
          </button>
        ))}
        {replay.active ? (
          <span className="font-medium text-amber-700">
            回放模式：仅查看（已禁用输入）{replay.playing ? ` · ${Math.min(replay.cursor + 1, replay.total)}/${replay.total}` : replay.total ? ' · 已结束' : ''}
          </span>
        ) : (
          <span className="text-slate-400">回放可按事件流重看 agent 过程</span>
        )}
      </div>
      <div className="flex items-center gap-2">
        {replay.error ? <span className="text-rose-600">{replay.error}</span> : null}
        {replay.active ? (
          <button
            type="button"
            onClick={replay.stop}
            className="rounded-lg border border-slate-200 px-3 py-1 font-semibold text-slate-600 hover:bg-slate-50"
          >
            退出回放
          </button>
        ) : (
          <button
            type="button"
            onClick={() => {
              void replay.start();
            }}
            className="rounded-lg border border-slate-200 px-3 py-1 font-semibold text-slate-600 hover:bg-slate-50"
          >
            回放
          </button>
        )}
      </div>
    </div>
  );
}
