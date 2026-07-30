/**
 * Execution-trace timeline assembler — merges a turn's SSE frames into an
 * ordered, typed item list for the step-timeline view. Reuses the already-parsed
 * fields (tool calls, per-message reasoning/usage/finish, AgentState channels,
 * retry/compaction events); the new work here is *ordering + typing*, not new
 * field parsing. See docs/.../2026-07-10-batch3-wireframe.html for the render.
 */
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
  stepCount: number | null;
  node: string;
  model: string | null;
  finishReason: string | null;
  reasoning: string | null;
  content: string | null;
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  tools: ToolCallEntry[];
  hasError: boolean;
  durationMs: number | null;
}
export interface AuxNodeItem {
  kind: "memory_recall" | "planner" | "reflect" | "memory_writeback" | "workspace_ingest";
  seq: number;
  receivedAt: string;
  serverMs?: number | null;
  node: string;
  summary: string;
  detail: Record<string, unknown>;
  tone: "normal" | "warn";
  durationMs: number | null;
}
export interface MarkerItem {
  kind: "compaction" | "retry" | "error" | "approval" | "guard" | "end";
  seq: number;
  receivedAt: string;
  serverMs?: number | null;
  text: string;
  tone: "warn" | "bad" | "good" | "pause";
}
export type TimelineItem = AgentStep | AuxNodeItem | MarkerItem;

const AI = "ai";
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
function durationOf(ch: Record<string, unknown>): number | null {
  const v = ch._duration_ms;
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

export function parseTimeline(events: readonly SseEvent[]): TimelineItem[] {
  const items: TimelineItem[] = [];
  const byId = new Map<string, ToolCallEntry>();
  for (const e of parseToolCalls(events)) byId.set(e.id, e);
  const workersByCall = parseWorkerFrames(events);
  let seq = 0;
  // Current evt's frame-id ms — set once per loop iteration below, visible
  // to every push() call made while processing that evt (single-threaded,
  // no concurrent iterations).
  let evtServerMs: number | null = null;
  const push = (it: Record<string, unknown>): void => {
    items.push({ ...it, seq: seq++, serverMs: evtServerMs } as TimelineItem);
  };

  for (const evt of events) {
    const at = evt.receivedAt;
    evtServerMs = serverMsOf(evt.id);
    if (evt.event === "compaction") {
      const d = obj(evt.data);
      push({ kind: "compaction", receivedAt: at, tone: "warn",
        text: `压缩 ${int(d.passes)} 遍 · ${int(d.tokens_before)} → ${int(d.tokens_after)} tok` });
      continue;
    }
    if (evt.event === "retry") {
      const d = obj(evt.data);
      push({ kind: "retry", receivedAt: at, tone: "warn",
        text: `重试 #${int(d.attempt)} · ${str(d.error_class)} · 退避 ${int(d.backoff_s)}s` });
      continue;
    }
    if (evt.event === "error") {
      const d = obj(evt.data);
      push({ kind: "error", receivedAt: at, tone: "bad", text: str(d.message) || str(d.name) || "运行错误" });
      continue;
    }
    if (evt.event === "approval") {
      push({ kind: "approval", receivedAt: at, tone: "pause", text: "等待人工审批" });
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
            ? `token 预算已用 ${Math.round((int(detail.spent) / Math.max(1, int(detail.limit))) * 100)}%(${int(detail.spent)}/${int(detail.limit)})`
            : `token 预算耗尽(${int(detail.spent)}/${int(detail.limit)})→ 收尾轮`
          : g === "max_steps"
            ? `步数耗尽(${int(detail.steps)}/${int(detail.max)})→ 收尾轮`
            : `无进展 ${int(detail.streak)}/${int(detail.max)} → 收尾轮`;
      push({ kind: "guard", receivedAt: at, tone: warn ? "warn" : "bad", text });
      continue;
    }
    if (evt.event === "end") {
      push({ kind: "end", receivedAt: at, tone: "good", text: "运行完成" });
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
          tools,
          hasError: tools.some((t) => t.status === "error"),
          durationMs: durationOf(ch),
        });
      }

      // aux node channels — positioned where they arrive
      if (Array.isArray(ch.recalled_memories) && ch.recalled_memories.length > 0) {
        push({ kind: "memory_recall", receivedAt: at, node, tone: "normal",
          summary: `记忆召回 · ${ch.recalled_memories.length} 条`,
          detail: { memories: ch.recalled_memories }, durationMs: durationOf(ch) });
      }
      if (ch.plan !== undefined && ch.plan !== null) {
        const p = obj(ch.plan);
        const steps = Array.isArray(p.steps) ? p.steps : [];
        push({ kind: "planner", receivedAt: at, node, tone: "normal",
          summary: `制定计划 · 目标 + ${steps.length} 步`, detail: { plan: p },
          durationMs: durationOf(ch) });
      }
      if (Array.isArray(ch.reflections) && ch.reflections.length > 0) {
        for (const r of ch.reflections) {
          const rr = obj(r);
          const verdict = str(rr.verdict);
          push({ kind: "reflect", receivedAt: at, node,
            tone: verdict === "revise" ? "warn" : "normal",
            summary: `反思 · ${verdict === "revise" ? "修订" : "通过"}`,
            detail: { verdict, critique: str(rr.critique) }, durationMs: durationOf(ch) });
        }
      }
      if (Array.isArray(ch.written_memories) && ch.written_memories.length > 0) {
        push({ kind: "memory_writeback", receivedAt: at, node, tone: "normal",
          summary: `记忆写回 · ${ch.written_memories.length} 条`,
          detail: { memories: ch.written_memories }, durationMs: durationOf(ch) });
      }
    }
  }
  return items;
}
