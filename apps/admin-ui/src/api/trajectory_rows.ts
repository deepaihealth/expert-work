/**
 * Trajectory-row projections — turns a turn's SSE events (via
 * ``parseTimeline``) into flat row lists that share one id scheme:
 * ``compactRowsOf`` (中栏紧凑行, no forced think rows) and ``ledgerRowsOf``
 * (账本行: user + one **assistant** per agent step, spec §九 D2). The old
 * right-rail ``trajectoryRowsOf`` (user + one think per step + a trailing
 * synthetic assistant) retired with the inspect panel in PR-A.2 Task 11.
 * All pure; no rendering, no state. 投影模型参照 deepseek-harness
 * ui-trajectory(MIT)重写。See
 * .superpowers/sdd/2026-08-18-debug-console-pr-a-console/task-4-brief.md
 * 与 .superpowers/sdd/2026-08-19-debug-console-pr-a2-trajectory/task-2-brief.md.
 */
import type { PlanStep, PlanStepStatus, ThreadPlan } from "./plan";
import type { SseEvent } from "./sessions";
import { parseTimeline } from "./timeline";
import { messagesOf, type ToolCallEntry } from "./tool_timeline";
import type { WorkerTimeline } from "./worker_timeline";

export type RowStatus = "running" | "ok" | "error" | "warn" | "pause";

interface RowBase {
  /** 轮内稳定 id:`${kind}:${seq}[:${toolIdx}[:${workerIdx}]]`;中栏与右栏同一套,右栏行集 ⊇ 中栏(中栏「检查」按 id 定位右栏行)。user / assistant 行 id 固定 `"user"` / `"assistant"`。 */
  id: string;
  /** 来源 `TimelineItem.seq`;user / assistant 行为 -1。 */
  seq: number;
  /** 所属 agent 步号(`AgentStep.stepCount`);aux / marker / user / assistant 为 null。 */
  step: number | null;
  status: RowStatus;
  durationMs: number | null;
  /** 该行对应的原始帧在 `events` 里的下标(Raw tab 用);来源 item 没带 `eventIndex` 时为 []。 */
  eventIndexes: number[];
  /** 帧 id 里的服务端毫秒(≈ 该单元结束时刻;Timing tab SSE 列用):agent / aux / marker 行取 `item.serverMs`,tool / plan(update_plan)行取 `entry.serverMs`;subagent / user / assistant 为 null。 */
  serverMs: number | null;
}
export type ThinkRow = RowBase & { kind: "think"; text: string; content: string | null; model: string | null; inputTokens: number; outputTokens: number; reasoningTokens?: number; cacheReadTokens?: number; finishReason: string | null };
export type ToolRow = RowBase & { kind: "tool"; entry: ToolCallEntry };
export type SubagentRow = RowBase & { kind: "subagent"; worker: WorkerTimeline; parentEntryId: string };
export type PlanRow = RowBase & {
  kind: "plan"; source: "update_plan" | "planner";
  /** `update_plan` 的 tool_call_id;planner 行 null。 */
  callId: string | null;
  /** 被合并进来的 planner item 的 seq(`resolveGanttKey` 用);没合并 null。 */
  plannerSeq: number | null;
  stepsTotal: number; goal: string | null; reason: string | null; plan: ThreadPlan | null;
};
export type MemoryRow = RowBase & { kind: "memory"; direction: "recall" | "writeback"; count: number; detail: Record<string, unknown> };
export type ReflectRow = RowBase & { kind: "reflect"; verdict: "pass" | "revise"; detail: Record<string, unknown> };
export type MarkerRow = RowBase & { kind: "compaction" | "retry" | "error" | "approval" | "guard" | "gap"; text: string };
export type CompactRow = ThinkRow | ToolRow | SubagentRow | PlanRow | MemoryRow | ReflectRow | MarkerRow;

export type UserRow = RowBase & { kind: "user"; text: string; attachmentNames: string[]; inputs: Record<string, string> };
export type AssistantRow = RowBase & {
  kind: "assistant";
  /** 该步正文;没有 ""。 */
  text: string;
  /** 该步思考;没有 ""。 */
  reasoning: string;
  model: string | null;
  inputTokens: number; outputTokens: number;
  reasoningTokens?: number; cacheReadTokens?: number;
  finishReason: string | null;
  /** 该步发起的工具调用数(含 update_plan)。 */
  toolCallCount: number;
};
export type TrajectoryRow = UserRow | CompactRow | AssistantRow;

export interface TrajectoryInput { text: string; attachmentNames: string[]; inputs: Record<string, string> }

/** Rule 4 — the frame index an item came from, or `[]` when the item predates `eventIndex`. */
function idx(item: { eventIndex?: number }): number[] {
  return item.eventIndex === undefined ? [] : [item.eventIndex];
}
function uniq(xs: readonly number[]): number[] {
  return Array.from(new Set(xs));
}
function toolStatus(status: ToolCallEntry["status"]): RowStatus {
  switch (status) {
    case "pending":
      return "running";
    case "error":
      return "error";
    case "pending_approval":
      return "pause";
    default:
      return "ok";
  }
}
/** `resultIdx` — the first `updates` frame carrying this call's RESULT
 *  message (`type === "tool" && tool_call_id === callId`); `[]` if none
 *  arrived yet. */
function resultIdx(events: readonly SseEvent[], callId: string): number[] {
  for (let i = 0; i < events.length; i += 1) {
    const evt = events[i];
    if (evt.event !== "updates") continue;
    const hit = messagesOf(evt.data).some((m) => m.type === "tool" && m.tool_call_id === callId);
    if (hit) return [i];
  }
  return [];
}
/** Every `event === "worker"` frame belonging to this worker id. */
function workerIdx(events: readonly SseEvent[], workerId: string): number[] {
  const out: number[] = [];
  events.forEach((evt, i) => {
    if (evt.event !== "worker") return;
    const d = evt.data;
    if (d !== null && typeof d === "object" && (d as Record<string, unknown>).worker_id === workerId) out.push(i);
  });
  return out;
}

/** Controller ruling — `plan_reducer.ts` (Task 3) isn't available in this
 *  worktree; a private narrowing guard stands in for its `isPlan`. Accepts a
 *  non-null object whose `steps` is an array and whose `goal` is a string
 *  (missing/invalid `goal` → the whole plan is rejected, `null`); each step
 *  is coerced to `{ id, description, status }` with string coercion and an
 *  unrecognised `status` falling back to `"pending"`. */
function asThreadPlan(v: unknown): ThreadPlan | null {
  if (v === null || typeof v !== "object") return null;
  const o = v as Record<string, unknown>;
  if (!Array.isArray(o.steps)) return null;
  if (typeof o.goal !== "string") return null;
  const steps: PlanStep[] = o.steps.map((s: unknown) => {
    const so = s !== null && typeof s === "object" ? (s as Record<string, unknown>) : {};
    const status: PlanStepStatus = so.status === "in_progress" || so.status === "completed" ? so.status : "pending";
    return {
      id: typeof so.id === "string" ? so.id : String(so.id ?? ""),
      description: typeof so.description === "string" ? so.description : String(so.description ?? ""),
      status,
    };
  });
  return { goal: o.goal, steps };
}

/** 每个 agent 步投影成什么行 —— 两个投影唯一的行为旋钮:
 *  - `compact`(中栏紧凑行):`reasoning` 非空才出一条 think 行(Rule 1);
 *  - `ledger`(账本,spec §九 D2):每步一条 assistant 行,不出 think 行。 */
type Projection = "compact" | "ledger";

/** Shared builder behind the two projections. Aux / tool / marker rows are
 *  identical across both of them (同 id 同顺序);只有 agent 步的投影不同。 */
function rowsOf(events: readonly SseEvent[], opts: { projection: Projection }): TrajectoryRow[] {
  const rows: TrajectoryRow[] = [];
  for (const item of parseTimeline(events)) {
    // A `switch` over the discriminant (rather than sequential `if (item.kind
    // === ...) { …; continue; }`) is required here — TS's control-flow
    // narrowing does not fully exclude a union member whose `kind` is itself
    // a multi-literal union (AuxNodeItem) via `if`/`||` checks split across
    // statements, even after every one of its literals has been handled;
    // only `switch`'s case-label exhaustiveness does. Verified in isolation
    // before relying on it here.
    switch (item.kind) {
      case "agent": {
        if (opts.projection === "ledger") {
          rows.push({
            id: `assistant:${item.seq}`, kind: "assistant", seq: item.seq, step: item.stepCount,
            text: item.content ?? "", reasoning: item.reasoning ?? "", model: item.model,
            inputTokens: item.inputTokens, outputTokens: item.outputTokens,
            reasoningTokens: item.reasoningTokens, cacheReadTokens: item.cacheReadTokens,
            finishReason: item.finishReason, toolCallCount: item.tools.length,
            status: item.hasError ? "error" : "ok", durationMs: item.durationMs,
            eventIndexes: idx(item), serverMs: item.serverMs ?? null,
          });
        } else if (item.reasoning !== null) {
          rows.push({
            id: `think:${item.seq}`, kind: "think", seq: item.seq, step: item.stepCount,
            text: item.reasoning ?? "", content: item.content, model: item.model,
            inputTokens: item.inputTokens, outputTokens: item.outputTokens,
            reasoningTokens: item.reasoningTokens, cacheReadTokens: item.cacheReadTokens,
            finishReason: item.finishReason,
            status: item.hasError ? "error" : "ok", durationMs: item.durationMs,
            eventIndexes: idx(item), serverMs: item.serverMs ?? null,
          });
        }
        item.tools.forEach((entry, ti) => {
          const status = toolStatus(entry.status);
          const eventIndexes = [...idx(item), ...resultIdx(events, entry.id)];
          if (entry.toolName === "update_plan") {
            const a = entry.args;
            rows.push({
              id: `plan:${item.seq}:${ti}`, kind: "plan", seq: item.seq, step: item.stepCount,
              status, durationMs: entry.durationMs, eventIndexes, serverMs: entry.serverMs ?? null,
              source: "update_plan", callId: entry.id, plannerSeq: null,
              stepsTotal: Array.isArray(a.steps) ? a.steps.length : 0,
              goal: typeof a.goal === "string" ? a.goal : null,
              reason: typeof a.reason === "string" ? a.reason : null,
              plan: null,
            });
          } else {
            rows.push({
              id: `tool:${item.seq}:${ti}`, kind: "tool", seq: item.seq, step: item.stepCount,
              status, durationMs: entry.durationMs, eventIndexes, serverMs: entry.serverMs ?? null, entry,
            });
          }
          entry.workers?.forEach((worker, wi) => {
            rows.push({
              id: `subagent:${item.seq}:${ti}:${wi}`, kind: "subagent", seq: item.seq, step: item.stepCount,
              status: worker.status === "running" ? "running" : worker.status === "success" ? "ok" : "warn",
              durationMs: worker.summary?.wallClockMs ?? null,
              eventIndexes: workerIdx(events, worker.workerId), serverMs: null,
              worker, parentEntryId: entry.id,
            });
          });
        });
        continue;
      }
      case "memory_recall":
      case "memory_writeback": {
        const direction: MemoryRow["direction"] = item.kind === "memory_recall" ? "recall" : "writeback";
        const memories = item.detail.memories;
        rows.push({
          id: `memory:${item.seq}`, kind: "memory", seq: item.seq, step: null, status: "ok",
          durationMs: item.durationMs, eventIndexes: idx(item), serverMs: item.serverMs ?? null,
          direction, count: Array.isArray(memories) ? memories.length : 0, detail: item.detail,
        });
        continue;
      }
      case "reflect": {
        const verdict: ReflectRow["verdict"] = item.detail.verdict === "revise" ? "revise" : "pass";
        rows.push({
          id: `reflect:${item.seq}`, kind: "reflect", seq: item.seq, step: null,
          status: verdict === "revise" ? "warn" : "ok", durationMs: item.durationMs,
          eventIndexes: idx(item), serverMs: item.serverMs ?? null, verdict, detail: item.detail,
        });
        continue;
      }
      case "planner": {
        const plan = asThreadPlan(item.detail.plan);
        const stepsTotal = plan ? plan.steps.length : 0;
        const goal = plan ? plan.goal : null;
        let merged = false;
        if (item.node === "tools") {
          for (let i = rows.length - 1; i >= 0; i -= 1) {
            const r = rows[i];
            if (r.kind === "plan" && r.source === "update_plan" && r.plan === null) {
              rows[i] = { ...r, plan, stepsTotal, goal, plannerSeq: item.seq, eventIndexes: uniq([...r.eventIndexes, ...idx(item)]) };
              merged = true;
              break;
            }
          }
        }
        if (!merged) {
          rows.push({
            id: `plan:${item.seq}`, kind: "plan", seq: item.seq, step: null, status: "ok",
            durationMs: item.durationMs, eventIndexes: idx(item), serverMs: item.serverMs ?? null,
            source: "planner", callId: null, plannerSeq: null, stepsTotal, goal, reason: null, plan,
          });
        }
        continue;
      }
      case "workspace_ingest":
      case "end":
        continue;
    }
    rows.push({
      id: `${item.kind}:${item.seq}`, kind: item.kind, seq: item.seq, step: null, text: item.text,
      status: item.tone === "bad" ? "error" : item.tone === "warn" ? "warn" : item.tone === "pause" ? "pause" : "ok",
      durationMs: null, eventIndexes: idx(item), serverMs: item.serverMs ?? null,
    });
  }
  return rows;
}

/** `TrajectoryRow` → `CompactRow` 的收窄谓词 —— `compact` 投影不会产出
 *  user / assistant 行,靠它把类型收回去(而不是断言)。 */
function isCompactRow(row: TrajectoryRow): row is CompactRow {
  return row.kind !== "user" && row.kind !== "assistant";
}

/** 中栏紧凑行:顺序 = parseTimeline 顺序;`end` 不出行(脚注表达状态);think 只在 reasoning 非空时出。 */
export function compactRowsOf(events: readonly SseEvent[]): CompactRow[] {
  return rowsOf(events, { projection: "compact" }).filter(isCompactRow);
}

/** 一行 user 行 —— 三个投影共用。 */
function userRowOf(input: TrajectoryInput): UserRow {
  return {
    id: "user", kind: "user", seq: -1, step: null, status: "ok", durationMs: null,
    eventIndexes: [], serverMs: null,
    text: input.text, attachmentNames: input.attachmentNames, inputs: input.inputs,
  };
}

/** 账本投影(spec §九 D2):`user` + 每个 agent 步一条 `assistant`(id
 *  `assistant:${seq}`,正文 / 思考 / 用量 / finishReason / 工具调用数都在行上)
 *  + 其余紧凑行(tool / plan / subagent / memory / reflect / marker,与
 *  `compactRowsOf` 同源同 id)。**不再有** think 行,也没有末尾合成 assistant 行。 */
export function ledgerRowsOf(events: readonly SseEvent[], input: TrajectoryInput): TrajectoryRow[] {
  return [userRowOf(input), ...rowsOf(events, { projection: "ledger" })];
}

/** `GanttRow.key` → 轨迹行 id(泳道块点击定位用);找不到 → null。 */
export function resolveGanttKey(rows: readonly TrajectoryRow[], key: string): string | null {
  const itemMatch = /^item-(\d+)$/.exec(key);
  if (itemMatch) {
    const seq = Number(itemMatch[1]);
    const direct = rows.find((r) => r.seq === seq && r.kind !== "tool" && r.kind !== "subagent");
    if (direct) return direct.id;
    const viaPlanner = rows.find((r) => r.kind === "plan" && r.plannerSeq === seq);
    return viaPlanner ? viaPlanner.id : null;
  }
  const toolMatch = /^tool-(.+)$/.exec(key);
  if (toolMatch) {
    const id = toolMatch[1];
    const direct = rows.find((r) => r.kind === "tool" && r.entry.id === id);
    if (direct) return direct.id;
    const viaPlan = rows.find((r) => r.kind === "plan" && r.callId === id);
    return viaPlan ? viaPlan.id : null;
  }
  const workerMatch = /^worker-(.+)-\d+$/.exec(key);
  if (workerMatch) {
    const workerId = workerMatch[1];
    const row = rows.find((r) => r.kind === "subagent" && r.worker.workerId === workerId);
    return row ? row.id : null;
  }
  return null;
}
