/**
 * B-269 / B-270 —— 排队 / 运行中气泡的进度文案。
 *
 * 运行中:气泡手里已经有这一轮的事件流,进度就地从帧算(``progressFromEvents``,
 * 口径与服务端 ``RunEventStore.progress_for_runs`` 相同),不读库。排队中:
 * 排队帧是受理那一刻的快照,之后由 run 详情的 ``queued_behind`` 刷新。
 */
import type { TFunction } from "i18next";

import type { RunProgress } from "../../api/runs";
import type { SseEvent } from "../../api/sessions";
import { serverMsOf } from "../../api/sse_id";

/** 气泡只说到分钟:``{prefix}_minutes``(``n``)或 ``{prefix}_under_minute``。 */
function minutesText(prefix: string, seconds: number, t: TFunction): string {
  const minutes = Math.floor(Math.max(0, seconds) / 60);
  return minutes < 1 ? t(`${prefix}_under_minute`) : t(`${prefix}_minutes`, { n: minutes });
}

/** 「已进行 N 分钟 · 第 S 步 · 调用工具 T 次 · 子任务 W 个在跑」;
 *  零或未知的部分不出现,全都没有时 ``null``。 */
export function progressLine(progress: RunProgress, t: TFunction): string | null {
  const parts: string[] = [];
  if (progress.elapsed_s !== null) {
    parts.push(minutesText("playground.progress_elapsed", progress.elapsed_s, t));
  }
  if (progress.step > 0) parts.push(t("playground.progress_step", { n: progress.step }));
  if (progress.tool_calls > 0) {
    parts.push(t("playground.progress_tool_calls", { n: progress.tool_calls }));
  }
  if (progress.active_workers > 0) {
    parts.push(t("playground.progress_workers", { n: progress.active_workers }));
  }
  return parts.length > 0 ? parts.join(" · ") : null;
}

/** 排队气泡:知道上一轮何时开始执行就说它跑了多久,否则原文案。 */
export function queuedText(aheadStartedAt: string | null, nowMs: number, t: TFunction): string {
  const started = aheadStartedAt === null ? Number.NaN : Date.parse(aheadStartedAt);
  if (Number.isNaN(started)) return t("playground.turn_queued");
  return minutesText("playground.turn_queued_behind", (nowMs - started) / 1000, t);
}

function queuedField(events: readonly SseEvent[], key: string): string | null {
  const queued = events.find((e) => e.event === "queued");
  const data = asRecord(queued?.data);
  const value = data?.[key];
  return typeof value === "string" ? value : null;
}

/** live 轮:排队帧里领头那一轮的开始时刻(还没开始 / 没有排队帧 = ``null``)。 */
export function aheadStartedAtOf(events: readonly SseEvent[]): string | null {
  return queuedField(events, "ahead_started_at");
}

/** live 轮:排队帧里这一轮自己的 run id(``metadata`` 帧之前只有它带)。 */
export function queuedRunIdOf(events: readonly SseEvent[]): string | null {
  return queuedField(events, "run_id");
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function toolResults(node: Record<string, unknown> | null): number {
  const messages = node?.messages;
  if (!Array.isArray(messages)) return 0;
  return messages.filter((m) => asRecord(m)?.type === "tool").length;
}

/** 本次执行(最后一个 ``metadata`` 帧之后)开了还没收的子任务个数。 */
function activeWorkers(events: readonly SseEvent[]): number {
  let attempt = -1;
  events.forEach((e, i) => {
    if (e.event === "metadata") attempt = i;
  });
  const started = new Set<string>();
  const ended = new Set<string>();
  for (const e of events.slice(attempt + 1)) {
    const data = e.event === "worker" ? asRecord(e.data) : null;
    const id = data?.worker_id;
    if (typeof id !== "string") continue;
    if (data?.kind === "start") started.add(id);
    if (data?.kind === "end") ended.add(id);
  }
  return [...started].filter((id) => !ended.has(id)).length;
}

/** 运行中这一轮的进度,从它的帧算;还没开始执行(没有 ``metadata``)= ``null``。
 *  已进行时间从第一个 ``metadata`` 帧的服务端时刻算起(接管不清零)。 */
export function progressFromEvents(
  events: readonly SseEvent[],
  nowMs: number,
): RunProgress | null {
  const first = events.find((e) => e.event === "metadata");
  if (first === undefined) return null;
  const startedMs = serverMsOf(first.id);
  let step = 0;
  let toolCalls = 0;
  for (const e of events) {
    const data = e.event === "updates" ? asRecord(e.data) : null;
    if (data === null) continue;
    const count = asRecord(data.agent)?.step_count;
    if (typeof count === "number") step = Math.max(step, count);
    toolCalls += toolResults(asRecord(data.tools));
  }
  return {
    elapsed_s: startedMs === null ? null : Math.max(0, Math.floor((nowMs - startedMs) / 1000)),
    step,
    tool_calls: toolCalls,
    active_workers: activeWorkers(events),
    current: null,
    last_event_at: null,
  };
}
