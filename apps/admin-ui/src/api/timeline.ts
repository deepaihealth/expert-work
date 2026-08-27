/**
 * Execution-trace timeline assembler — merges a turn's SSE frames into an
 * ordered, typed item list for the step-timeline view. Reuses the already-parsed
 * fields (tool calls, per-message reasoning/usage/finish, AgentState channels,
 * retry/compaction events); the new work here is *ordering + typing*, not new
 * field parsing. See docs/.../2026-07-10-batch3-wireframe.html for the render.
 */
import i18n from "../i18n";
import type { SseEvent } from "./sessions";
import { serverMsOf } from "./sse_id";
import { parseToolCalls, type ToolCallEntry } from "./tool_timeline";
import { parseWorkerFrames } from "./worker_timeline";

export interface AgentStep {
  kind: "agent";
  seq: number;
  receivedAt: string;
  /** SSE frame id's millisecond segment (``serverMsOf``) — ``null`` if the
   *  frame's ``id`` is missing/malformed. Gantt data layer's absolute-time
   *  anchor; never derive wall-clock position from ``receivedAt``. */
  serverMs?: number | null;
  /** 来源帧在 events 里的下标(Raw tab / 轨迹行用);老调用方不依赖。 */
  eventIndex?: number;
  stepCount: number | null;
  node: string;
  model: string | null;
  finishReason: string | null;
  reasoning: string | null;
  content: string | null;
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  /** ``usage_metadata.output_token_details.reasoning`` —— 厂商没报就是
   *  ``undefined``(≠ 0:0 是「报了、确实没有思考 token」)。行表「思考」列用。 */
  reasoningTokens?: number;
  /** ``usage_metadata.input_token_details.cache_read`` —— 同上,没报是 ``undefined``。 */
  cacheReadTokens?: number;
  /** ``additional_kwargs.first_token_ms`` —— LLM 调用起点到第一个非空 delta 的毫秒;后端没写(无流式 / judge 开着)就是 undefined。 */
  firstTokenMs?: number;
  tools: ToolCallEntry[];
  hasError: boolean;
  durationMs: number | null;
}
export interface AuxNodeItem {
  kind: "memory_recall" | "planner" | "reflect" | "memory_writeback" | "workspace_ingest";
  seq: number;
  receivedAt: string;
  serverMs?: number | null;
  /** 来源帧在 events 里的下标(Raw tab / 轨迹行用);老调用方不依赖。 */
  eventIndex?: number;
  node: string;
  summary: string;
  detail: Record<string, unknown>;
  tone: "normal" | "warn";
  durationMs: number | null;
}
export interface MarkerItem {
  /** ``gap``(P3 PR-1)= 这一段帧在**这条连接**上补不到了 —— 只在 live 分支出现;
   *  回放遇到落库空洞是静默跳过的,两条分支的缺口契约不同。 */
  kind: "compaction" | "retry" | "error" | "approval" | "guard" | "end" | "gap";
  seq: number;
  receivedAt: string;
  serverMs?: number | null;
  /** 来源帧在 events 里的下标(Raw tab / 轨迹行用);老调用方不依赖。 */
  eventIndex?: number;
  text: string;
  tone: "warn" | "bad" | "good" | "pause";
}
export type TimelineItem = AgentStep | AuxNodeItem | MarkerItem;

const AI = "ai";
/** ``end.status`` → 结束标记的文案与色调。**``paused`` 不是失败** —— 等人审批,
 *  对话还会继续,所以用 pause 而不是 bad。 */
const END_MARKER: Record<string, { key: string; tone: MarkerItem["tone"] }> = {
  success: { key: "console_runtime.status_success", tone: "good" },
  paused: { key: "console_runtime.status_paused", tone: "pause" },
  interrupted: { key: "console_runtime.status_interrupted", tone: "warn" },
  error: { key: "console_runtime.status_error", tone: "bad" },
};
function obj(v: unknown): Record<string, unknown> {
  return v !== null && typeof v === "object" ? (v as Record<string, unknown>) : {};
}
function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}
function int(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}
function textOf(v: unknown): string | null {
  if (typeof v === "string") return v.trim() === "" ? null : v;
  // #9c — block-list content (Anthropic-style array): join the text parts,
  // same handling as api/turn_summary.ts's textOf.
  if (Array.isArray(v)) {
    const parts = v
      .filter(
        (b): b is { text: string } =>
          b !== null &&
          typeof b === "object" &&
          typeof (b as { text?: unknown }).text === "string",
      )
      .map((b) => b.text);
    const joined = parts.join("");
    return joined.trim() === "" ? null : joined;
  }
  return null;
}
/** 可选计数:没报 / 不是有限数字 → ``undefined``(``int`` 那套会把缺省抹成 0)。 */
function optInt(v: unknown): number | undefined {
  return typeof v === "number" && Number.isFinite(v) ? v : undefined;
}
function durationOf(ch: Record<string, unknown>): number | null {
  const v = ch._duration_ms;
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

// events 数组在 console 全链只替换不变异(PR-A.2 已依赖此不变式),按引用缓存
// 是安全的;照 session_stats.ts 的 CONTRIBUTION_CACHE / ledger_timing.ts 的
// SPAN_CACHE 先例。返回的 TimelineItem 对象跨调用方共享,消费者不得改写
// (与 events 只替换不变异同级不变式)。
const _cache = new WeakMap<readonly SseEvent[], TimelineItem[]>();

export function parseTimeline(events: readonly SseEvent[]): TimelineItem[] {
  const cached = _cache.get(events);
  if (cached !== undefined) return cached;
  const items: TimelineItem[] = [];
  const byId = new Map<string, ToolCallEntry>();
  for (const e of parseToolCalls(events)) byId.set(e.id, e);
  const workersByCall = parseWorkerFrames(events);
  let seq = 0;
  // Current evt's frame-id ms — set once per loop iteration below, visible
  // to every push() call made while processing that evt (single-threaded,
  // no concurrent iterations).
  let evtServerMs: number | null = null;
  let evtIndex = 0;
  const push = (it: Record<string, unknown>): void => {
    items.push({ ...it, seq: seq++, serverMs: evtServerMs, eventIndex: evtIndex } as TimelineItem);
  };

  for (const [i, evt] of events.entries()) {
    evtIndex = i;
    const at = evt.receivedAt;
    evtServerMs = serverMsOf(evt.id);
    if (evt.event === "compaction") {
      const d = obj(evt.data);
      push({ kind: "compaction", receivedAt: at, tone: "warn",
        text: i18n.t("console_runtime.compaction", {
          n: int(d.passes), before: int(d.tokens_before), after: int(d.tokens_after),
        }) });
      continue;
    }
    if (evt.event === "retry") {
      const d = obj(evt.data);
      push({ kind: "retry", receivedAt: at, tone: "warn",
        text: i18n.t("console_runtime.retry", {
          n: int(d.attempt), error: str(d.error_class), backoff: int(d.backoff_s),
        }) });
      continue;
    }
    if (evt.event === "error") {
      const d = obj(evt.data);
      push({ kind: "error", receivedAt: at, tone: "bad",
        text: str(d.message) || str(d.name) || i18n.t("console_runtime.error_generic") });
      continue;
    }
    if (evt.event === "approval") {
      push({ kind: "approval", receivedAt: at, tone: "pause", text: i18n.t("console_runtime.status_paused") });
      continue;
    }
    if (evt.event === "guard") {
      const d = obj(evt.data);
      const detail = obj(d.detail);
      const warn = str(d.kind) === "warning";
      const g = str(d.guard);
      const text =
        g === "token_budget"
          ? warn
            ? i18n.t("console_runtime.guard_token_budget_warn", {
                pct: Math.round((int(detail.spent) / Math.max(1, int(detail.limit))) * 100),
                spent: int(detail.spent), limit: int(detail.limit),
              })
            : i18n.t("console_runtime.guard_token_budget_exhausted", {
                spent: int(detail.spent), limit: int(detail.limit),
              })
          : g === "max_steps"
            ? i18n.t("console_runtime.guard_max_steps", { steps: int(detail.steps), max: int(detail.max) })
            : i18n.t("console_runtime.guard_no_progress", { streak: int(detail.streak), max: int(detail.max) });
      push({ kind: "guard", receivedAt: at, tone: warn ? "warn" : "bad", text });
      continue;
    }
    if (evt.event === "gap") {
      // 服务端缓冲已经滚过这一段、而它们又还没落盘,所以**这条连接**补不到了 ——
      // 不代表这些帧不存在,run 结束后重新回放通常能拿到。不是错误(别渲染成
      // 红的),但也不能当未知帧丢掉:时间线上真的缺了一段。
      const d = obj(evt.data);
      push({ kind: "gap", receivedAt: at, tone: "warn",
        text: i18n.t("console_runtime.gap_frames", { from: int(d.from), to: int(d.to) }) });
      continue;
    }
    if (evt.event === "end") {
      // ``end`` 的 data 从 null 变成 {status, run_id}(P3 PR-1)。四值见契约实况
      // 表 F;``timeout`` 后端已归到 ``error``。老流(data 为 null)落回"运行完成"。
      const m = END_MARKER[str(obj(evt.data).status)] ?? END_MARKER.success;
      push({ kind: "end", receivedAt: at, tone: m.tone, text: i18n.t(m.key) });
      continue;
    }
    if (evt.event === "worker") continue; // 已由 parseWorkerFrames 预扫,挂在工具卡上
    if (evt.event !== "updates") continue;

    const data = obj(evt.data);
    for (const [node, raw] of Object.entries(data)) {
      const ch = obj(raw);

      // agent LLM step(s) — one per AI message
      const msgs = Array.isArray(ch.messages) ? ch.messages : [];
      for (const m of msgs) {
        const mm = obj(m);
        if (mm.type !== AI) continue;
        const rm = obj(mm.response_metadata);
        const um = obj(mm.usage_metadata);
        const ak = obj(mm.additional_kwargs);
        const calls = Array.isArray(mm.tool_calls) ? mm.tool_calls : [];
        const tools: ToolCallEntry[] = [];
        for (const tc of calls) {
          const id = str(obj(tc).id);
          const entry = id === "" ? undefined : byId.get(id);
          if (entry) {
            const workers = workersByCall.get(id);
            if (workers) entry.workers = workers;
            tools.push(entry);
          }
        }
        push({
          kind: "agent", receivedAt: at, node,
          stepCount: typeof ch.step_count === "number" ? ch.step_count : null,
          model: str(rm.model_name) || null,
          finishReason: str(rm.finish_reason) || null,
          reasoning: textOf(ak.reasoning_content),
          content: textOf(mm.content),
          inputTokens: int(um.input_tokens),
          outputTokens: int(um.output_tokens),
          totalTokens: int(um.total_tokens),
          reasoningTokens: optInt(obj(um.output_token_details).reasoning),
          cacheReadTokens: optInt(obj(um.input_token_details).cache_read),
          firstTokenMs: optInt(ak.first_token_ms),
          tools,
          hasError: tools.some((t) => t.status === "error"),
          durationMs: durationOf(ch),
        });
      }

      // aux node channels — positioned where they arrive
      if (Array.isArray(ch.recalled_memories) && ch.recalled_memories.length > 0) {
        push({ kind: "memory_recall", receivedAt: at, node, tone: "normal",
          summary: i18n.t("console_runtime.memory_recall", { n: ch.recalled_memories.length }),
          detail: { memories: ch.recalled_memories }, durationMs: durationOf(ch) });
      }
      if (ch.plan !== undefined && ch.plan !== null) {
        const p = obj(ch.plan);
        const steps = Array.isArray(p.steps) ? p.steps : [];
        push({ kind: "planner", receivedAt: at, node, tone: "normal",
          summary: i18n.t("console_runtime.planner", { n: steps.length }), detail: { plan: p },
          durationMs: durationOf(ch) });
      }
      if (Array.isArray(ch.reflections) && ch.reflections.length > 0) {
        for (const r of ch.reflections) {
          const rr = obj(r);
          const verdict = str(rr.verdict);
          push({ kind: "reflect", receivedAt: at, node,
            tone: verdict === "revise" ? "warn" : "normal",
            summary: i18n.t(verdict === "revise" ? "console_runtime.reflect_revise" : "console_runtime.reflect_pass"),
            detail: { verdict, critique: str(rr.critique) }, durationMs: durationOf(ch) });
        }
      }
      if (Array.isArray(ch.written_memories) && ch.written_memories.length > 0) {
        push({ kind: "memory_writeback", receivedAt: at, node, tone: "normal",
          summary: i18n.t("console_runtime.memory_writeback", { n: ch.written_memories.length }),
          detail: { memories: ch.written_memories }, durationMs: durationOf(ch) });
      }
    }
  }
  _cache.set(events, items);
  return items;
}
