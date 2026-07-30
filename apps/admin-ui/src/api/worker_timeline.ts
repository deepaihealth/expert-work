// B2 PR2 — 把持久化/实时同源的 "worker" SSE 帧折叠成工具卡可挂载的
// WorkerTimeline 树。纯函数,防御式:异常帧丢弃不抛。
// 帧契约见 docs/superpowers/specs/2026-07-19-worker-observability-design.md。

import { serverMsOf } from "./gantt_timeline";
import type { SseEvent } from "./sessions";

export interface WorkerMessageSummary {
  type: string;
  contentExcerpt?: string;
  toolCalls?: { name: string; argsExcerpt: string }[];
  name?: string;
  toolResultExcerpt?: string;
}

export interface WorkerStepSummary {
  wseq: number;
  node: string;
  stepCount: number | null;
  durationMs: number;
  /** This ``update`` frame's SSE id ms segment (``serverMsOf``) — ``null``
   *  if the frame's ``id`` is missing/malformed. Gantt data layer's
   *  absolute-time anchor for this worker step's bar. */
  serverMs?: number | null;
  messages: WorkerMessageSummary[];
}

export type WorkerStatus = "running" | "success" | "max_steps" | "cancelled";

export interface WorkerTimeline {
  workerId: string;
  parentWorkerId: string | null;
  parentToolCallId: string | null;
  label: string;
  agentRef: string;
  depth: number;
  role: string | null;
  taskExcerpt: string;
  maxSteps: number | null;
  status: WorkerStatus;
  steps: WorkerStepSummary[];
  children: WorkerTimeline[];
  summary: { iterationUsed: number; llmCallCount: number; wallClockMs: number } | null;
}

interface RawFrame {
  worker_id: string;
  parent_worker_id: string | null;
  parent_tool_call_id: string | null;
  label: string;
  agent_ref: string;
  depth: number;
  kind: "start" | "update" | "end";
  wseq: number;
  data: Record<string, unknown>;
}

function asFrame(data: unknown): RawFrame | null {
  if (typeof data !== "object" || data === null) return null;
  const d = data as Record<string, unknown>;
  if (typeof d.worker_id !== "string" || typeof d.kind !== "string") return null;
  if (d.kind !== "start" && d.kind !== "update" && d.kind !== "end") return null;
  return {
    worker_id: d.worker_id,
    parent_worker_id: typeof d.parent_worker_id === "string" ? d.parent_worker_id : null,
    parent_tool_call_id: typeof d.parent_tool_call_id === "string" ? d.parent_tool_call_id : null,
    label: typeof d.label === "string" ? d.label : "",
    agent_ref: typeof d.agent_ref === "string" ? d.agent_ref : "",
    depth: typeof d.depth === "number" ? d.depth : 1,
    kind: d.kind,
    wseq: typeof d.wseq === "number" ? d.wseq : 0,
    data: typeof d.data === "object" && d.data !== null ? (d.data as Record<string, unknown>) : {},
  };
}

function ensureWorker(byId: Map<string, WorkerTimeline>, f: RawFrame): WorkerTimeline {
  let w = byId.get(f.worker_id);
  if (!w) {
    w = {
      workerId: f.worker_id,
      parentWorkerId: f.parent_worker_id,
      parentToolCallId: f.parent_tool_call_id,
      label: f.label,
      agentRef: f.agent_ref,
      depth: f.depth,
      role: null,
      taskExcerpt: "",
      maxSteps: null,
      status: "running",
      steps: [],
      children: [],
      summary: null,
    };
    byId.set(f.worker_id, w);
  }
  return w;
}

function summarizeMessages(raw: unknown): WorkerMessageSummary[] {
  if (!Array.isArray(raw)) return [];
  const out: WorkerMessageSummary[] = [];
  for (const m of raw) {
    if (typeof m !== "object" || m === null) continue;
    const r = m as Record<string, unknown>;
    const msg: WorkerMessageSummary = { type: typeof r.type === "string" ? r.type : "?" };
    if (typeof r.content_excerpt === "string") msg.contentExcerpt = r.content_excerpt;
    if (typeof r.name === "string") msg.name = r.name;
    if (typeof r.tool_result_excerpt === "string") msg.toolResultExcerpt = r.tool_result_excerpt;
    if (Array.isArray(r.tool_calls)) {
      msg.toolCalls = r.tool_calls.flatMap((c) => {
        if (typeof c !== "object" || c === null) return [];
        const cc = c as Record<string, unknown>;
        return [{
          name: typeof cc.name === "string" ? cc.name : "?",
          argsExcerpt: typeof cc.args_excerpt === "string" ? cc.args_excerpt : "",
        }];
      });
    }
    out.push(msg);
  }
  return out;
}

export function parseWorkerFrames(events: readonly SseEvent[]): Map<string, WorkerTimeline[]> {
  const byId = new Map<string, WorkerTimeline>();
  for (const evt of events) {
    if (evt.event !== "worker") continue;
    const f = asFrame(evt.data);
    if (f === null) continue;
    const w = ensureWorker(byId, f);
    if (f.kind === "start") {
      w.taskExcerpt = typeof f.data.task_excerpt === "string" ? f.data.task_excerpt : "";
      w.role = typeof f.data.role === "string" ? f.data.role : null;
      w.maxSteps = typeof f.data.max_steps === "number" ? f.data.max_steps : null;
    } else if (f.kind === "update") {
      w.steps.push({
        wseq: f.wseq,
        node: typeof f.data.node === "string" ? f.data.node : "?",
        stepCount: typeof f.data.step_count === "number" ? f.data.step_count : null,
        durationMs: typeof f.data._duration_ms === "number" ? f.data._duration_ms : 0,
        serverMs: serverMsOf(evt.id),
        messages: summarizeMessages(f.data.messages),
      });
    } else {
      const outcome = f.data.outcome;
      w.status = outcome === "max_steps" || outcome === "cancelled" ? outcome : "success";
      w.summary = {
        iterationUsed: typeof f.data.iteration_used === "number" ? f.data.iteration_used : 0,
        llmCallCount: typeof f.data.llm_call_count === "number" ? f.data.llm_call_count : 0,
        wallClockMs: typeof f.data.wall_clock_ms === "number" ? f.data.wall_clock_ms : 0,
      };
    }
  }

  // 第二遍:孙 worker 挂父;根按 parent_tool_call_id 分组(到达序 = Map 插入序)。
  const roots = new Map<string, WorkerTimeline[]>();
  for (const w of byId.values()) {
    if (w.parentWorkerId !== null) {
      const parent = byId.get(w.parentWorkerId);
      if (parent) parent.children.push(w);
      continue; // 父缺失 → 丢(防御)
    }
    if (w.parentToolCallId === null) continue; // 无法挂卡 → 丢(防御)
    const bucket = roots.get(w.parentToolCallId);
    if (bucket) bucket.push(w);
    else roots.set(w.parentToolCallId, [w]);
  }
  return roots;
}
