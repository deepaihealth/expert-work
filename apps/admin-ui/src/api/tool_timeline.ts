/**
 * Tool-call timeline parser.
 *
 * Walks a run's SSE ``updates`` frames (LangGraph ``{node: {messages}}``
 * chunks, each message a ``BaseMessage.model_dump()``) and reconstructs the
 * agent's tool activity: every ``AIMessage.tool_calls[]`` is a CALL, every
 * ``ToolMessage`` (linked by ``tool_call_id``) is its RESULT. The result
 * message carries no tool name (LangChain quirk), so the name + args come
 * from the call side.
 *
 * MCP tools are registered as ``mcp__{server}__{tool}`` (orchestrator
 * ``MCPTool`` — wire-safe form, see ``mcp_tool_name``), so we attribute the
 * originating MCP server from the name. Builtin tools (``web_search``,
 * ``exec_python``, …) keep their bare name.
 */
import { serverMsOf } from "./gantt_timeline";
import type { SseEvent } from "./sessions";
import type { WorkerTimeline } from "./worker_timeline";

export type ToolCallStatus = "pending" | "success" | "error" | "pending_approval";

export interface ToolCallEntry {
  /** ``tool_call_id`` — links the call to its result. */
  id: string;
  /** Raw tool name as the LLM called it (e.g. ``mcp__amap-maps__maps_direction_driving``). */
  rawName: string;
  isMcp: boolean;
  /** MCP server name when ``isMcp`` (else ``null``). */
  server: string | null;
  /** Display tool name — the ``mcp__server__`` prefix stripped. */
  toolName: string;
  args: Record<string, unknown>;
  status: ToolCallStatus;
  /** Result text with the spotlight ``«UNTRUSTED…»`` fence stripped (``null`` until the result arrives). */
  resultPreview: string | null;
  /** Structured sandbox result (exec_python / bash only) parsed from ``resultPreview``. */
  execResult?: ExecResult;
  /** Tool execution time in ms, from the result's ``additional_kwargs.duration_ms`` (``null`` until the result arrives or if absent). */
  durationMs: number | null;
  /** RESULT frame's SSE id ms segment (``serverMsOf``) — ``null`` until the
   *  result arrives or its frame ``id`` is missing/malformed. Gantt data
   *  layer's absolute-time anchor for this call's bar. */
  serverMs?: number | null;
  /** B2 PR2 — 本次调用派生的 worker 子时间线(spawn_worker / subagent);无则缺省。 */
  workers?: WorkerTimeline[];
  /** 定时任务工具(``manage_task`` create)回传的 trigger id —— 供「立即触发」按钮。取自 wire ToolMessage 的 ``artifact.trigger_id``。 */
  triggerId?: string | null;
  /** ``manage_task`` 动作(create/update/…),取自 wire ``artifact.action``。
   *  「立即触发」按钮据此收紧为仅 create 卡。 */
  action?: string | null;
}

const MCP_PREFIX = "mcp__";
const MCP_SEP = "__";
// Spotlight injection-defense fence lines wrapping untrusted tool output.
const SPOTLIGHT_FENCE = /«\/?UNTRUSTED[^»]*»/g;

/** Structured stdout / stderr / exit code of a sandbox tool (exec_python, bash). */
export interface ExecResult {
  stdout: string;
  stderr: string;
  exitCode: number | null;
}

/** Builtin tools whose result follows ``format_sandbox_outcome``'s rendering. */
const SANDBOX_TOOLS = new Set(["exec_python", "bash"]);

/**
 * Parse the rendered sandbox result string into structured fields. Format
 * (``format_sandbox_outcome``): sections joined by ``\n\n`` —
 * ``stdout:\n<out>``, ``stderr:\n<err>`` (each optional; ``(no output)`` when
 * both empty), an optional ``[execution timed out …]`` line, then a trailing
 * ``exit_code: <n>``. ``exit_code`` is always last. Best-effort: a null
 * ``exitCode`` signals an unrecognised shape.
 */
export function parseExecResult(preview: string): ExecResult {
  const exitMatch = preview.match(/\nexit_code:\s*(-?\d+)\s*$/);
  const exitCode = exitMatch ? Number(exitMatch[1]) : null;
  const body = exitMatch ? preview.slice(0, exitMatch.index).trimEnd() : preview;
  const section = (label: string): string => {
    const marker = `${label}:\n`;
    const start = body.indexOf(marker);
    if (start === -1) return "";
    const rest = body.slice(start + marker.length);
    const next = rest.search(/\n\n(?:stdout:\n|stderr:\n|\[execution timed out)/);
    return (next === -1 ? rest : rest.slice(0, next)).trim();
  };
  return { stdout: section("stdout"), stderr: section("stderr"), exitCode };
}

interface ParsedName {
  isMcp: boolean;
  server: string | null;
  toolName: string;
}

function parseName(raw: string): ParsedName {
  if (raw.startsWith(MCP_PREFIX)) {
    const rest = raw.slice(MCP_PREFIX.length); // "server__tool"
    const sep = rest.indexOf(MCP_SEP);
    if (sep > 0) {
      return { isMcp: true, server: rest.slice(0, sep), toolName: rest.slice(sep + MCP_SEP.length) };
    }
    return { isMcp: true, server: null, toolName: rest };
  }
  return { isMcp: false, server: null, toolName: raw };
}

function stripFence(content: string): string {
  return content.replace(SPOTLIGHT_FENCE, "").trim();
}

/** Flatten the messages across every node in one ``updates`` chunk. */
export function messagesOf(data: unknown): Array<Record<string, unknown>> {
  if (data === null || typeof data !== "object") return [];
  const out: Array<Record<string, unknown>> = [];
  for (const nodeVal of Object.values(data as Record<string, unknown>)) {
    if (nodeVal !== null && typeof nodeVal === "object") {
      const msgs = (nodeVal as Record<string, unknown>).messages;
      if (Array.isArray(msgs)) {
        for (const m of msgs) {
          if (m !== null && typeof m === "object") out.push(m as Record<string, unknown>);
        }
      }
    }
  }
  return out;
}

/** One COMPACTION event (RT-2 PR-4) — a context-compression pass landed,
 *  summarising the middle of the transcript. Numeric-only per the backend
 *  payload (no conversation content). */
export interface CompactionSummary {
  /** Client receive order — de-dupes replayed frames, orders the cards. */
  receivedAt: string;
  passes: number;
  tokensBefore: number;
  tokensAfter: number;
  summaryChars: number;
}

function numberField(data: Record<string, unknown>, key: string): number | null {
  const v = data[key];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/**
 * Extract the ordered COMPACTION summaries from a run's SSE frames. A frame is
 * ``event: "compaction"`` with ``data: {passes, tokens_before, tokens_after,
 * summary_chars}`` (see ``sse._publish_compaction``). Malformed / partial
 * frames are skipped rather than rendered half-blank.
 */
export function parseCompactionEvents(events: readonly SseEvent[]): CompactionSummary[] {
  const out: CompactionSummary[] = [];
  for (const evt of events) {
    if (evt.event !== "compaction") continue;
    if (evt.data === null || typeof evt.data !== "object") continue;
    const data = evt.data as Record<string, unknown>;
    const passes = numberField(data, "passes");
    const tokensBefore = numberField(data, "tokens_before");
    const tokensAfter = numberField(data, "tokens_after");
    const summaryChars = numberField(data, "summary_chars");
    if (passes === null || tokensBefore === null || tokensAfter === null || summaryChars === null) {
      continue;
    }
    out.push({ receivedAt: evt.receivedAt, passes, tokensBefore, tokensAfter, summaryChars });
  }
  return out;
}

/** One transient-retry event (sse.py retry publish): the run hit a retryable
 *  error and backed off before re-attempting the astream loop. */
export interface RetryEntry {
  receivedAt: string;
  attempt: number;
  errorClass: string;
  backoffS: number;
}

/** Extract ordered retry events (``event: "retry"`` with
 *  ``{attempt, error_class, backoff_s}``). Malformed frames are skipped. */
export function parseRetryEvents(events: readonly SseEvent[]): RetryEntry[] {
  const out: RetryEntry[] = [];
  for (const evt of events) {
    if (evt.event !== "retry" || evt.data === null || typeof evt.data !== "object") continue;
    const d = evt.data as Record<string, unknown>;
    if (typeof d.attempt !== "number" || typeof d.error_class !== "string") continue;
    out.push({
      receivedAt: evt.receivedAt,
      attempt: d.attempt,
      errorClass: d.error_class,
      backoffS: typeof d.backoff_s === "number" ? d.backoff_s : 0,
    });
  }
  return out;
}

/**
 * Reconstruct the ordered tool-call timeline from a run's SSE frames.
 *
 * ``awaitingApproval`` — the run paused at an approval gate (the turn carries
 * a pending ``ApprovalItem``). The gate dispatches NOTHING for the blocked
 * batch, so any call still ``pending`` is not executing — it is awaiting the
 * human decision. Surface those as ``pending_approval`` rather than the
 * generic ``pending`` (进行中), which would otherwise read as a stuck tool.
 */
export function parseToolCalls(
  events: readonly SseEvent[],
  awaitingApproval = false,
): ToolCallEntry[] {
  const order: string[] = [];
  const byId = new Map<string, ToolCallEntry>();

  const ensure = (id: string, init: () => ToolCallEntry): ToolCallEntry => {
    let entry = byId.get(id);
    if (entry === undefined) {
      entry = init();
      byId.set(id, entry);
      order.push(id);
    }
    return entry;
  };

  for (const evt of events) {
    if (evt.event !== "updates") continue;
    for (const m of messagesOf(evt.data)) {
      // Call side — an AIMessage carrying tool_calls.
      if (m.type === "ai" && Array.isArray(m.tool_calls)) {
        for (const tc of m.tool_calls as Array<Record<string, unknown>>) {
          if (typeof tc.id !== "string" || tc.id === "") continue;
          const rawName = typeof tc.name === "string" ? tc.name : "";
          const parsed = parseName(rawName);
          const args =
            tc.args !== null && typeof tc.args === "object"
              ? (tc.args as Record<string, unknown>)
              : {};
          const entry = ensure(tc.id, () => ({
            id: tc.id as string,
            rawName,
            isMcp: parsed.isMcp,
            server: parsed.server,
            toolName: parsed.toolName,
            args,
            status: "pending",
            resultPreview: null,
            durationMs: null,
            serverMs: null,
            triggerId: null,
            action: null,
          }));
          // A re-seen call (replayed frame) refreshes name/args, never status.
          entry.rawName = rawName;
          entry.isMcp = parsed.isMcp;
          entry.server = parsed.server;
          entry.toolName = parsed.toolName;
          entry.args = args;
        }
      }
      // Result side — a ToolMessage linked by tool_call_id. The orchestrator
      // now stamps ``name`` on the result too; use it as a fallback when the
      // call frame was missed (truncated stream), and to seed the entry.
      if (m.type === "tool" && typeof m.tool_call_id === "string" && m.tool_call_id !== "") {
        const status: ToolCallStatus = m.status === "error" ? "error" : "success";
        const preview = typeof m.content === "string" ? stripFence(m.content) : "";
        const resultName = typeof m.name === "string" ? m.name : "";
        const entry = ensure(m.tool_call_id, () => {
          const parsed = parseName(resultName);
          return {
            id: m.tool_call_id as string,
            rawName: resultName,
            isMcp: parsed.isMcp,
            server: parsed.server,
            toolName: resultName === "" ? (m.tool_call_id as string) : parsed.toolName,
            args: {},
            status,
            resultPreview: preview,
            durationMs: null,
            serverMs: null,
            triggerId: null,
            action: null,
          };
        });
        // Fill the name from the result only if the call side didn't provide it.
        if (entry.rawName === "" && resultName !== "") {
          const parsed = parseName(resultName);
          entry.rawName = resultName;
          entry.isMcp = parsed.isMcp;
          entry.server = parsed.server;
          entry.toolName = parsed.toolName;
        }
        entry.status = status;
        entry.resultPreview = preview;
        entry.serverMs = serverMsOf(evt.id);
        const ak = m.additional_kwargs;
        const durRaw =
          ak !== null && typeof ak === "object"
            ? (ak as Record<string, unknown>).duration_ms
            : undefined;
        if (typeof durRaw === "number" && Number.isFinite(durRaw)) {
          entry.durationMs = durRaw;
        }
        // artifact — LangChain ToolMessage's structured metadata field
        // (``ToolResult.meta``, builder.py:2819-2827). ``manage_task``'s
        // create action stashes the new trigger's id here so the debug
        // console can offer a 立即触发 (run now) shortcut.
        const art = (m as { artifact?: unknown }).artifact;
        if (art !== null && typeof art === "object") {
          const rec = art as Record<string, unknown>;
          const tid = rec.trigger_id;
          if (typeof tid === "string" && tid !== "") entry.triggerId = tid;
          const act = rec.action;
          if (typeof act === "string" && act !== "") entry.action = act;
        }
      }
    }
  }

  const entries = order.map((id) => byId.get(id) as ToolCallEntry);
  for (const entry of entries) {
    if (!entry.isMcp && SANDBOX_TOOLS.has(entry.toolName) && entry.resultPreview) {
      entry.execResult = parseExecResult(entry.resultPreview);
    }
  }
  if (awaitingApproval) {
    for (const entry of entries) {
      if (entry.status === "pending") entry.status = "pending_approval";
    }
  }
  return entries;
}

/** An artifact the agent registered this turn — drives the inline per-message
 *  download row (the agent can't emit a download URL itself; the UI renders it
 *  from the artifact name, the same way deer-flow surfaces ``present_files``). */
export interface TurnArtifact {
  name: string;
  kind: string;
}

/** Artifacts registered via a successful ``save_artifact`` call in this turn's
 *  events, newest-wins on re-save (a re-saved name keeps one chip). */
export function artifactsFromTools(events: readonly SseEvent[]): TurnArtifact[] {
  const byName = new Map<string, TurnArtifact>();
  for (const entry of parseToolCalls(events)) {
    if (entry.toolName !== "save_artifact" || entry.status !== "success") continue;
    const name = typeof entry.args.name === "string" ? entry.args.name.trim() : "";
    if (name === "") continue;
    const kind = typeof entry.args.kind === "string" ? entry.args.kind : "other";
    byName.set(name, { name, kind });
  }
  return [...byName.values()];
}

/** Aggregate a turn's tool activity for an at-a-glance header: how many calls,
 *  how many failed. ``pending`` / ``pending_approval`` are not failures. */
export function toolStatusSummary(
  events: readonly SseEvent[],
): { total: number; failed: number } {
  const entries = parseToolCalls(events);
  const failed = entries.filter((e) => e.status === "error").length;
  return { total: entries.length, failed };
}
