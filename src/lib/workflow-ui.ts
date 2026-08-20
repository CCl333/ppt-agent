import type { ProjectEvent, ProjectMessage } from './ppt-api';

function toTimestamp(value?: string): number {
  if (!value) {
    return Number.MAX_SAFE_INTEGER;
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : Number.MAX_SAFE_INTEGER;
}

export function mergeMessageList(
  current: ProjectMessage[],
  incoming: ProjectMessage[],
): ProjectMessage[] {
  const preferredOrder = incoming.length ? incoming : current;
  const firstSeenOrder = new Map<string, number>();
  preferredOrder.forEach((message, index) => {
    firstSeenOrder.set(message.id, index);
  });
  current.forEach((message) => {
    if (!firstSeenOrder.has(message.id)) {
      firstSeenOrder.set(message.id, firstSeenOrder.size);
    }
  });
  const merged = new Map<string, ProjectMessage>();
  [...current, ...incoming].forEach((message) => {
    merged.set(message.id, message);
  });

  return Array.from(merged.values()).sort((left, right) => {
    const timeDiff = toTimestamp(left.created_at) - toTimestamp(right.created_at);
    if (timeDiff !== 0) {
      return timeDiff;
    }
    return (firstSeenOrder.get(left.id) ?? 0) - (firstSeenOrder.get(right.id) ?? 0);
  });
}

export function shouldRefreshFromEvent(event: ProjectEvent): boolean {
  return [
    'agent.message',
    'action.step.completed',
    'action.step.failed',
    'status.changed',
    'workspace.data.updated',
    'requirements.answers_updated',
    'project.created',
  ].includes(event.event_type);
}

export function summarizeSourcePipeline(
  items: Array<{read_status?: string; chunk_status?: string}>,
): {
  total: number;
  readReady: number;
  chunkReady: number;
  failed: number;
} {
  return items.reduce(
    (summary, item) => {
      summary.total += 1;
      if (item.read_status === 'ready' || item.read_status === 'reused') {
        summary.readReady += 1;
      }
      if (item.chunk_status === 'ready') {
        summary.chunkReady += 1;
      }
      if (item.read_status === 'failed' || item.chunk_status === 'failed') {
        summary.failed += 1;
      }
      return summary;
    },
    {total: 0, readReady: 0, chunkReady: 0, failed: 0},
  );
}
