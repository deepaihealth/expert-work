/**
 * The transcript turn card — user message + agent answer + the full per-turn
 * debug surface (step timeline / exact Langfuse trace / raw frames, run state,
 * artifacts, approval gate, feedback bar).
 *
 * Lifted verbatim out of ``pages/agent_detail/PlaygroundTab.tsx`` so the
 * conversation detail page can render the same (read-only) timeline. No logic
 * changed by the move.
 *
 * i18n note: reads the ``playground.*`` namespace — now a **cross-page
 * shared** namespace (see ``components/turn/types.ts``), plus
 * ``event_stream.*`` / ``trace_toolbar.*`` / ``common.*``.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Collapse,
  Modal,
  Segmented,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import {
  AlertTriangle,
  Check,
  Download,
  ExternalLink,
  Maximize2,
  MessageSquareText,
  RotateCcw,
  X,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import type { ApprovalItem } from "../../api/approvals";
import { buildGanttRows, serverMsOf, type GanttModel, type GanttRow } from "../../api/gantt_timeline";
import type { RateCardRecord } from "../../api/rate_card";
import { getRun } from "../../api/runs";
import { getRunTrace, type RunTrace } from "../../api/trace_facade";
import type { SseEvent } from "../../api/sessions";
import {
  artifactsFromTools,
  parseCompactionEvents,
  parseRetryEvents,
  toolStatusSummary,
} from "../../api/tool_timeline";
import type { FireNowResult } from "../../api/triggers";
import { summarizeTurn } from "../../api/turn_summary";
import { parseAgentState } from "../../api/agent_state";
import { parseTimeline } from "../../api/timeline";
import { CompactionSummaryList } from "../CompactionCard";
import { EventCard } from "../EventCard";
import { MarkdownView } from "../MarkdownView";
import { AgentStatePanels } from "../../pages/agent_detail/playground/AgentStatePanels";
import { fmtDuration } from "../../pages/agent_detail/playground/duration_format";
import type { FallbackLine } from "../../pages/agent_detail/playground/history_turns";
import { RunStatusBanner } from "../../pages/agent_detail/playground/RunStatusBanner";
import { StepTimeline } from "../../pages/agent_detail/playground/StepTimeline";
import type { LiveStep } from "../../pages/agent_detail/playground/useTokenStream";
import { timelineBannerModel } from "../../pages/agent_detail/playground/timeline_banner";
import { traceBannerModel } from "../../pages/agent_detail/playground/trace_banner";
import { TraceView } from "../../pages/agent_detail/playground/TraceView";
import { labelPurpose } from "../../pages/agent_detail/playground/trace_purpose";
import { TurnMeta } from "../../pages/agent_detail/playground/TurnMeta";
import { PlanPanel } from "../../pages/run_detail/PlanPanel";
import { buildLangfuseTraceUrl } from "../../config/env";
import { FeedbackBar } from "./FeedbackBar";
import {
  FullTextModal,
  FullTextTrigger,
  type FullTextState,
} from "./FullTextModal";
import { GanttTimeline } from "./GanttTimeline";
import type { Turn } from "./types";

const { Text } = Typography;

/** Minor#5 — the commentary clamp length, previously a repeated magic
 *  number (240) at each render site. */
const COMMENTARY_CLAMP_CHARS = 240;

/** A commentary-channel line — the de-emphasised icon + secondary-text +
 *  clamp rendering shared by the live answer block, the historical-turn
 *  fallback branch, and the degraded flat-message views (spec 2026-07-30,
 *  Important#1/Minor#3/Minor#5: keeps every rendering site byte-identical
 *  instead of hand-copied). Exported so ``PlaygroundTab``/``ConversationDetail``
 *  can reuse it for their flat-list degradation paths (Minor#3). */
export function CommentarySegmentLine({
  text,
  label,
}: {
  text: string;
  label: string;
}) {
  return (
    <div
      style={{ display: "flex", gap: 6, alignItems: "flex-start", marginBottom: 6 }}
      data-testid="turn-segment-commentary"
    >
      <MessageSquareText
        size={12}
        role="img"
        style={{ marginTop: 3, flexShrink: 0, color: "var(--ew-text-tertiary)" }}
        aria-label={label}
      />
      <Text type="secondary" style={{ fontSize: 12, whiteSpace: "pre-wrap" }}>
        {text.length > COMMENTARY_CLAMP_CHARS
          ? `${text.slice(0, COMMENTARY_CLAMP_CHARS)}…`
          : text}
      </Text>
    </div>
  );
}

// A just-finished run's Langfuse trace lands as `not_ready` for a moment
// (ingestion isn't atomic — the root closes before its child observations
// land). Auto-poll a few times so the waterfall appears without a manual
// refresh, then settle on whatever we last got.
const TRACE_NOT_READY_MAX_RETRIES = 6;
const TRACE_NOT_READY_RETRY_MS = 1500;

export function runIdOf(events: readonly SseEvent[]): string | null {
  for (const e of events) {
    if (
      e.event === "metadata" &&
      e.data !== null &&
      typeof e.data === "object"
    ) {
      const rid = (e.data as Record<string, unknown>).run_id;
      if (typeof rid === "string" && rid) return rid;
    }
  }
  return null;
}

/** Task 3 — Gantt growing-bar calibration anchor: the most recent SSE frame
 *  carrying a valid server-ms id (``serverMsOf``) plus the client wall-clock
 *  moment it was received. ``GanttModel`` is a point-in-time snapshot with no
 *  "now" input (Task 2's documented boundary) — the caller advances
 *  ``model.totalMs`` itself using ``lastFrame.serverMs + (Date.now() -
 *  lastFrame.receivedAtMs)`` (design doc: 客户端 now 与最近帧 id_ms 的偏差
 *  校准), never raw ``Date.now()`` alone, so client/server clock skew never
 *  leaks into the axis. ``null`` when no frame has a parseable id — the
 *  caller then leaves the axis static (same as a settled turn). */
function lastKnownFrame(
  events: readonly SseEvent[],
): { serverMs: number; receivedAtMs: number } | null {
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const serverMs = serverMsOf(events[i].id);
    const receivedAtMs = Date.parse(events[i].receivedAt);
    if (serverMs !== null && Number.isFinite(receivedAtMs)) {
      return { serverMs, receivedAtMs };
    }
  }
  return null;
}

/** #5 — build an ``ApprovalItem`` from a backend ``approval`` SSE frame so the
 *  gate renders the instant the run pauses, without waiting for the terminal
 *  ``end`` frame + a ``/v1/approvals`` poll (which never fires when the client
 *  misses ``end``). The decide call only needs ``thread_id`` + ``run_id``; the
 *  rest feeds the gate card. Fields absent from the stream default safely. */
export function approvalItemFromEvent(data: unknown): ApprovalItem | null {
  if (data === null || typeof data !== "object") return null;
  const d = data as Record<string, unknown>;
  if (typeof d.run_id !== "string" || typeof d.thread_id !== "string")
    return null;
  const str = (v: unknown): string => (typeof v === "string" ? v : "");
  return {
    id: str(d.request_id) || d.run_id,
    tenant_id: str(d.tenant_id),
    user_id: null,
    run_id: d.run_id,
    thread_id: d.thread_id,
    request_id: str(d.request_id),
    node: str(d.node),
    reason_kind: str(d.reason_kind),
    action_summary: str(d.action_summary),
    proposed_args:
      d.proposed_args !== null && typeof d.proposed_args === "object"
        ? (d.proposed_args as Record<string, unknown>)
        : {},
    requested_at: str(d.requested_at),
    timeout_at: str(d.timeout_at),
    status: "pending",
    decided_by: null,
    decided_at: null,
  };
}

export function ApprovalGate({
  approval,
  busy,
  onDecide,
}: {
  approval: ApprovalItem;
  busy: boolean;
  onDecide: (decision: "approve" | "reject") => void;
}) {
  const { t } = useTranslation();
  return (
    <div
      data-testid="playground-approval"
      style={{
        border: "1px solid var(--ew-color-warning, #d4a017)",
        borderRadius: 6,
        padding: 10,
        marginTop: 8,
        background: "var(--ew-surface-raised)",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          marginBottom: 4,
        }}
      >
        <AlertTriangle size={14} strokeWidth={1.75} />
        <Text strong style={{ fontSize: 12 }}>
          {approval.node} — {t("playground.approval_awaiting")}
        </Text>
      </div>
      <Text style={{ fontSize: 12, display: "block", marginBottom: 6 }}>
        {approval.action_summary}
      </Text>
      <pre
        style={{
          margin: 0,
          fontSize: 11,
          fontFamily: "var(--ew-font-mono)",
          color: "var(--ew-text-secondary)",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          maxHeight: 160,
          overflow: "auto",
          marginBottom: 8,
        }}
      >
        {JSON.stringify(approval.proposed_args, null, 2)}
      </pre>
      <Space size={8}>
        <Button
          type="primary"
          size="small"
          icon={<Check size={13} strokeWidth={1.75} />}
          loading={busy}
          onClick={() => onDecide("approve")}
          data-testid="playground-approval-approve"
        >
          {t("playground.approval_approve")}
        </Button>
        <Button
          danger
          size="small"
          icon={<X size={13} strokeWidth={1.75} />}
          loading={busy}
          onClick={() => onDecide("reject")}
          data-testid="playground-approval-reject"
        >
          {t("playground.approval_reject")}
        </Button>
        <Text type="secondary" style={{ fontSize: 11 }}>
          {t("playground.approval_modify_hint")}
        </Text>
      </Space>
    </div>
  );
}

export interface TurnCardProps {
  turn: Turn;
  /** The turn's index in the transcript — sent as feedback ``turn_seq``. */
  turnSeq: number;
  /** Seed for this turn's own view state (persisted global default). Each
   *  TurnCard owns its view independently, so switching one turn to "exact"
   *  fetches only that turn's trace — not every rendered turn's (avoids a
   *  request burst on long threads). ``onViewChange`` updates the persisted
   *  default so the preference still seeds future turns. */
  initialEventView: "timeline" | "raw" | "exact";
  onViewChange: (view: "timeline" | "raw" | "exact") => void;
  threadId: string | null;
  onDownloadArtifact: (name: string) => Promise<void>;
  rate: RateCardRecord | null;
  onDecide: (
    turnId: string,
    approval: ApprovalItem,
    decision: "approve" | "reject",
  ) => void;
  deciding: boolean;
  onExport: (turn: Turn) => void;
  exporting: boolean;
  /** item 15 — gates the "open in Langfuse" deep link (Langfuse has no
   *  per-tenant isolation; see TraceToolbar for the same gate). */
  isSystemAdmin: boolean;
  /** Historical-turn rendering: hides the approval gate and feedback bar.
   *  NOT a blanket "no writes" switch — ``onExport`` still fires (callers
   *  must pass a real handler, not a no-op) and the manage_task 「立即触发」
   *  button inside ToolCallCard is currently ungated. Live turns default
   *  false. */
  readOnly?: boolean;
  /** Historical-turn lazy load state — drives the placeholder below. */
  loadState?: "pending" | "loading" | "done" | "error";
  /** Assistant text shown before this historical turn's events replay, one
   *  entry per assistant message (spec 2026-07-30 — each keeps its
   *  structural channel so it renders commentary/final like a live turn). */
  fallbackLines?: FallbackLine[];
  /** 流式打字机(子项目 3a)— live token buffers by step, forwarded to
   *  ``StepTimeline``. Only the currently-streaming live turn receives these
   *  (undefined/default elsewhere — history turns never stream). */
  liveByStep?: ReadonlyMap<number, LiveStep>;
  ttftMs?: number | null;
  finalized?: boolean;
  /** Spec 1 PR4 Task 5 — see ``StepTimelineProps.onFireResult``; PlaygroundTab
   *  passes its real ``handleFireResult`` here for both the live and history
   *  TurnCard render sites, so a manage_task card's 「立即触发」 button (live
   *  or replayed) reports its result back up into the transcript's task
   *  result cards. */
  onFireResult?: (result: FireNowResult) => void;
  /** #10 — per-turn retry. Live turns get a real re-dispatch handler; the
   *  playground's history turns get a backfill-the-input-box handler (a past
   *  run's enqueued inputs aren't exposed by the backend yet). Not passed on
   *  the read-only conversation page → the button simply doesn't render. */
  onRetry?: (turn: Turn) => void;
}

export function TurnCard({
  turn,
  turnSeq,
  initialEventView,
  onViewChange,
  threadId,
  onDownloadArtifact,
  rate,
  onDecide,
  deciding,
  onExport,
  exporting,
  isSystemAdmin,
  readOnly = false,
  loadState = "done",
  fallbackLines,
  liveByStep,
  ttftMs = null,
  finalized = false,
  onFireResult,
  onRetry,
}: TurnCardProps) {
  const { t } = useTranslation();
  // Minor#1 — memoized: an unmemoized call produces a fresh `segments` array
  // identity every render, which the streaming auto-scroll effect below
  // depends on ([segments, turn.status]) — that forced the view back to the
  // bottom on every render while streaming, interrupting a user scrolling up.
  const summary = useMemo(() => summarizeTurn(turn.events), [turn.events]);
  const toolStats = toolStatusSummary(turn.events);
  const agentState = parseAgentState(turn.events);
  const retries = parseRetryEvents(turn.events);
  const compactions = parseCompactionEvents(turn.events);
  const timeline = useMemo(() => parseTimeline(turn.events), [turn.events]);
  // Task 3 — the "timeline" eventView renders `GanttTimeline` (Gantt
  // execution trace) instead of `StepTimeline`; built straight off
  // `turn.events` (Task 1's `buildGanttRows` reuses `parseTimeline`/
  // `parseToolCalls`/`parseWorkerFrames` internally, so this isn't a second
  // parse pass). `settled` flips once the run leaves "running" so the last
  // no-tool-calls agent row gets `kind: "final"` (#1072 final-step
  // semantics — see gantt_timeline.ts).
  const gantt = useMemo(
    () => buildGanttRows(turn.events, { settled: turn.status !== "running" }),
    [turn.events, turn.status],
  );
  // M3 — hoisted above the growing-bar interval effect below: the effect's
  // dependency array reads both values during render (not just at effect-run
  // time), so they must already exist at that point (TDZ) — `handleViewChange`
  // and the Collapse wiring that own these further down still work the same,
  // just referencing state declared earlier in the function body.
  const [eventView, setEventViewLocal] = useState(initialEventView);
  const [eventsCollapseKeys, setEventsCollapseKeys] = useState<string[]>(["events"]);
  // Growing bar: while running AND the timeline view's events panel is
  // actually visible, tick every 1s so the axis (and any pending tool/worker
  // row stretching to its right edge) keeps advancing between SSE frames
  // instead of freezing at the last known event — cleared the instant the
  // turn settles, the view switches away from "timeline", or the events
  // panel collapses (effect cleanup + the dependency array below). Ticking
  // for a view/panel nobody can see was pure wasted work (M3).
  const [nowTick, setNowTick] = useState(0);
  useEffect(() => {
    if (turn.status !== "running") return;
    if (eventView !== "timeline" || !eventsCollapseKeys.includes("events")) return;
    const id = window.setInterval(() => setNowTick((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, [turn.status, eventView, eventsCollapseKeys]);
  // `GanttModel` is a point-in-time snapshot (Task 2's documented boundary —
  // it has no "now" input); advance its `totalMs` here using the calibrated
  // "now" (`lastKnownFrame` above the component) so the axis actually grows
  // between frames rather than freezing.
  const ganttModel = useMemo((): GanttModel => {
    if (turn.status !== "running") return gantt;
    const last = lastKnownFrame(turn.events);
    if (last === null) return gantt;
    const nowServerMs = last.serverMs + (Date.now() - last.receivedAtMs);
    const grownTotalMs = gantt.totalMs + Math.max(0, nowServerMs - last.serverMs);
    return grownTotalMs > gantt.totalMs ? { ...gantt, totalMs: grownTotalMs } : gantt;
  }, [gantt, turn.status, turn.events, nowTick]);
  const [ganttExpanded, setGanttExpanded] = useState(false);
  // Row detail — a thin adapter handing the row's underlying `TimelineItem`
  // to the existing 整卡 (full-card) renderer. Both `detail.type` variants
  // ("item" for agent/aux rows, "parentStep" for tool/worker rows) carry the
  // same `TimelineItem`, so a single-element `StepTimeline` array reuses the
  // existing card verbatim (nested tool/worker sub-timeline included) — no
  // new detail-rendering logic.
  // C2 — does NOT forward `liveByStep`/`ttftMs`/`finalized`: this single-row
  // `StepTimeline` only ever renders `row.detail.item` (a settled item), but
  // the full `liveByStep` map covers every in-flight step, not just this
  // row's. A single-element `StepTimeline` treats every step absent from its
  // (one-item) `settled` set as unsettled — so passing the whole map here
  // made every OTHER step's live buffer render as a phantom "interrupted"
  // card under whichever row happened to be expanded. The live surface now
  // lives once, alongside the Gantt (see the C1 sibling `StepTimeline` in
  // the `eventView === "timeline"` branch below), not per-row.
  const renderGanttDetail = useCallback(
    (row: GanttRow) => (
      <StepTimeline items={[row.detail.item]} onFireResult={onFireResult} hideLegend />
    ),
    [onFireResult],
  );
  // C1 fix (Critical, gantt-execution-timeline review) — `StepTimeline`
  // derives its own `settled` step-number set solely from the `items` array
  // it's handed (see StepTimeline.tsx); the sibling `StepTimeline` call below
  // passes `items={[]}` by design (the Gantt itself renders every settled
  // step; this call exists only to surface the still-streaming typewriter),
  // so THAT internal `settled` set is always empty. `useTokenStream`
  // accumulates `liveByStep` per step and never clears a step's buffer once
  // it settles — by design, it documents the consuming render filtering it
  // out via `settled`. An always-empty `settled` set defeats that filter: an
  // already-settled step's stale buffer renders forever as a duplicate
  // "streaming" (or, once `finalized`, falsely "interrupted") card next to
  // its own real Gantt row. Compute the settled step-number set here — from
  // the same `timeline` parse already built above, i.e. exactly what
  // `StepTimeline` would compute were it handed the full item list — and
  // filter `liveByStep` before handing it down.
  const settledSteps = useMemo(() => {
    const s = new Set<number>();
    for (const it of timeline) {
      if (it.kind === "agent" && it.stepCount !== null) s.add(it.stepCount);
    }
    return s;
  }, [timeline]);
  const unsettledLiveByStep = useMemo(() => {
    if (!liveByStep) return liveByStep;
    const filtered = new Map<number, LiveStep>();
    for (const [step, live] of liveByStep) {
      if (!settledSteps.has(step)) filtered.set(step, live);
    }
    return filtered;
  }, [liveByStep, settledSteps]);
  // Task 11 — RunStatusBanner status for the timeline view, derived from
  // this turn's own SSE-parsed items (NOT Langfuse level, unlike the exact
  // view's traceBanner below).
  const timelineBanner = useMemo(() => timelineBannerModel(timeline), [timeline]);
  // parseAgentState always returns a plain object (never null), so a bare
  // truthiness check on it would always pass — check its channels instead so
  // the run-state section actually hides when every channel is empty.
  const hasAgentState =
    agentState.recalledMemories.length > 0 ||
    agentState.toolFailures.length > 0 ||
    agentState.reflections.length > 0 ||
    agentState.subagentInvocations.length > 0 ||
    (agentState.signals.noProgressStreak ?? 0) > 0 ||
    agentState.signals.escalateNext === true;
  // A+B — artifacts the agent registered this turn (``save_artifact``). The
  // agent can't emit a download link itself (the endpoint is user-scoped +
  // auth'd), so surface them as an inline download row — deer-flow's pattern.
  const turnArtifacts = useMemo(
    () => artifactsFromTools(turn.events),
    [turn.events],
  );
  const [downloadingArtifact, setDownloadingArtifact] = useState<string | null>(
    null,
  );
  const downloadArtifact = useCallback(
    async (name: string) => {
      setDownloadingArtifact(name);
      try {
        await onDownloadArtifact(name);
      } finally {
        setDownloadingArtifact(null);
      }
    },
    [onDownloadArtifact],
  );
  // Channelled segments (spec 2026-07-30): commentary rows render de-emphasised
  // in sequence order; the final row is the answer body. The old join("\n\n")
  // flattened narration INTO the answer — that's exactly the bug.
  const segments = summary.segments;
  const hasText = segments.length > 0;
  // Named distinctly from the `fullText`/`setFullText` modal state below (the
  // shared "查看全文" trigger for this block and the reasoning section) —
  // same name would shadow that state variable.
  const answerFullText = segments.map((s) => s.text).join("\n\n");
  const runId = runIdOf(turn.events);
  // #9e/#11 — 「查看全文」 modal shared by the answer block and the turn's
  // reasoning summary section.
  const [fullText, setFullText] = useState<FullTextState | null>(null);
  // #10 — failed-turn detection for the retry button's danger styling.
  // ``turn.status === "error"`` only covers transport-level failures (a
  // server-side failure emits an ``error`` frame then still settles "done"),
  // so also read the timeline banner / raw error frames.
  const turnFailed =
    turn.status === "error" ||
    timelineBanner?.status === "error" ||
    turn.events.some((e) => e.event === "error");
  // I2 — while streaming, keep the capped answer box pinned to the newest
  // text (the transcript-level auto-scroll can't reach inside this inner
  // scroll div). Once the turn settles, stop forcing so the user can scroll.
  const answerScrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const node = answerScrollRef.current;
    if (turn.status === "running" && node) {
      node.scrollTop = node.scrollHeight;
    }
  }, [segments, turn.status]);

  // Per-turn view state, seeded from the persisted global default. Switching
  // one turn's view no longer flips every other turn (and no longer fans out
  // a getRunTrace to every rendered turn at once). `eventView` itself is
  // declared earlier in this function (M3 — see the growing-bar interval
  // effect above).
  const handleViewChange = useCallback(
    (next: "timeline" | "raw" | "exact") => {
      setEventViewLocal(next);
      onViewChange(next); // persist as the default seed for future turns
    },
    [onViewChange],
  );

  // Batch 4b item 14 — "精确" (exact) trace view. Lazy: this turn's trace is
  // fetched only once this turn's own view is "exact".
  const agentStepCount = useMemo(
    () => timeline.filter((it) => it.kind === "agent").length,
    [timeline],
  );
  const [trace, setTrace] = useState<RunTrace | null>(null);
  const traceRetriesRef = useRef(0);
  useEffect(() => {
    if (eventView !== "exact" || !threadId || !runId || trace !== null) return;
    let cancelled = false;
    void getRunTrace(threadId, runId)
      .then((data) => {
        if (!cancelled) setTrace(data);
      })
      .catch(() => {
        if (!cancelled) setTrace({ status: "unavailable" });
      });
    return () => {
      cancelled = true;
    };
  }, [eventView, threadId, runId, trace]);
  // Fresh retry budget whenever the turn or the view changes.
  useEffect(() => {
    traceRetriesRef.current = 0;
  }, [runId, eventView]);
  // Auto-poll while the trace is still ingesting (`not_ready`), up to the cap —
  // clearing `trace` re-triggers the fetch effect above. Stops the moment the
  // trace resolves to any non-`not_ready` state (or the budget runs out).
  useEffect(() => {
    if (
      trace?.status !== "not_ready" ||
      traceRetriesRef.current >= TRACE_NOT_READY_MAX_RETRIES
    ) {
      return;
    }
    const timer = setTimeout(() => {
      traceRetriesRef.current += 1;
      setTrace(null);
    }, TRACE_NOT_READY_RETRY_MS);
    return () => clearTimeout(timer);
  }, [trace]);
  // A finished run's trace only lands in Langfuse after the run closes, so the
  // lazy fetch above may have run (and burned its not-ready retry budget) while
  // the run was still in flight. Re-arm one fresh fetch the moment this turn
  // completes, so the exact view auto-refreshes instead of needing a manual
  // click — setTrace(null) re-triggers the fetch effect when the view is exact.
  useEffect(() => {
    if (turn.status === "done") setTrace(null);
  }, [turn.status]);
  // A' purpose labelling (spec §3.2) — see trace_purpose.ts.
  const labeledTrace = useMemo(
    () =>
      trace
        ? labelPurpose(trace, agentStepCount, t("playground.tr_purpose_primary"))
        : null,
    [trace, agentStepCount, t],
  );
  // Task 10 — RunStatusBanner status for the exact view, derived from the
  // trace's spans (null hides the banner: still loading / not a fully-loaded
  // ok trace).
  const traceBanner = useMemo(
    () => (labeledTrace ? traceBannerModel(labeledTrace) : null),
    [labeledTrace],
  );

  // item 15 — direct Langfuse deep link. system_admin only (Langfuse has no
  // per-tenant isolation — see TraceToolbar). Best-effort: a failed getRun
  // just leaves the link hidden.
  const [traceId, setTraceId] = useState<string | null>(null);
  useEffect(() => {
    if (!isSystemAdmin || !threadId || !runId) return;
    let cancelled = false;
    void getRun(threadId, runId)
      .then((detail) => {
        if (!cancelled) setTraceId(detail.trace_id ?? null);
      })
      .catch(() => {
        // Best-effort — the link simply stays hidden on failure.
      });
    return () => {
      cancelled = true;
    };
  }, [isSystemAdmin, threadId, runId]);
  const langfuseUrl = isSystemAdmin ? buildLangfuseTraceUrl(traceId) : null;

  // #4 cost — non-cached input + cache_read + output, each at its per-mtok rate
  // (micro-元 per 1M tokens). null when no usage or no rate for the model.
  const costCny =
    summary.usage && rate
      ? (Math.max(
          0,
          summary.usage.inputTokens - summary.usage.cacheReadTokens,
        ) *
          rate.input_per_mtok_micros +
          summary.usage.cacheReadTokens * rate.cache_read_per_mtok_micros +
          summary.usage.outputTokens * rate.output_per_mtok_micros) /
        1e12
      : null;

  // 历史轮懒态:事件未到位时显示输入 + 兜底答案 + 载入指示,避免用空 events
  // 跑完整解析机器。事件到位(loadState==="done")后走下方原完整渲染。此早返回
  // 必须在上方所有 hook 之后,否则 loadState 切换会改变 hook 数量,违反 Rules of Hooks。
  if (readOnly && turn.events.length === 0 && loadState !== "done") {
    return (
      <div
        data-testid="playground-turn"
        style={{ display: "flex", flexDirection: "column", gap: 8, flexShrink: 0 }}
      >
        <div
          style={{
            alignSelf: "flex-end",
            maxWidth: "85%",
            padding: "6px 10px",
            borderRadius: 8,
            fontSize: 13,
            whiteSpace: "pre-wrap",
            background: "var(--ew-surface-raised)",
            border: "1px solid var(--ew-border-subtle)",
          }}
        >
          {turn.input}
        </div>
        {fallbackLines && fallbackLines.length > 0 ? (
          <>
            {/* Minor#2 — restores the pre-segments wrapper styling (git show
                b10d54b5:.../TurnCard.tsx :518-528) so an unreplayed historical
                turn's fallback answer keeps its de-emphasised look, visually
                distinct from a live/settled answer. */}
            <div
              style={{
                alignSelf: "flex-start",
                maxWidth: "85%",
                fontSize: 13,
                opacity: 0.75,
                maxHeight: 420,
                overflowY: "auto",
              }}
            >
              {fallbackLines.map((l, i) =>
                l.channel === "commentary" ? (
                  <CommentarySegmentLine
                    key={i}
                    text={l.text}
                    label={t("playground.segment_commentary")}
                  />
                ) : (
                  <MarkdownView key={i}>{l.text}</MarkdownView>
                ),
              )}
            </div>
            {/* Important#1 — a commentary line clamps to 240 chars above with
                no other way to read the rest; a replay that never lands
                (fallback is permanent, not "until the timeline loads") must
                not leave long narration permanently truncated. */}
            <FullTextTrigger
              onClick={() =>
                setFullText({
                  title: t("playground.view_full_text"),
                  text: fallbackLines.map((l) => l.text).join("\n\n"),
                })
              }
            />
          </>
        ) : null}
        {loadState !== "error" ? (
          <div
            style={{ display: "flex", alignItems: "center", gap: 6, opacity: 0.6, fontSize: 12 }}
          >
            <Spin size="small" />
            <span>{t("playground.history_loading")}</span>
          </div>
        ) : null}
        <FullTextModal state={fullText} onClose={() => setFullText(null)} />
      </div>
    );
  }

  return (
    <div
      data-testid="playground-turn"
      style={{
        border: "1px solid var(--ew-border-subtle)",
        borderRadius: 6,
        overflow: "hidden",
        // The transcript is a flex column — without this the (single) turn
        // shrinks to the container height and its overflow:hidden clips the
        // events instead of letting the transcript scroll. Keep natural height.
        flexShrink: 0,
      }}
    >
      {/* User message */}
      <div
        style={{
          padding: "8px 12px",
          background: "var(--ew-surface-raised)",
          borderBottom: "1px solid var(--ew-border-subtle)",
        }}
      >
        <Text style={{ whiteSpace: "pre-wrap", fontSize: 13 }}>
          {turn.input}
        </Text>
        {turn.attachments.length > 0 && (
          <div style={{ marginTop: 4 }}>
            {turn.attachments.map((a) => (
              <Tag key={a.id} bordered={false} style={{ fontSize: 11 }}>
                {a.name}
              </Tag>
            ))}
          </div>
        )}
      </div>

      {/* Agent answer */}
      <div style={{ padding: "8px 12px" }} data-testid="playground-turn-answer">
        {/* C1 — a failed turn keeps whatever answer it produced; the Alert is
            a banner ABOVE the content, never a replacement (this is the only
            place a failed run's partial output can be read back). Empty error
            text falls back to the generic copy in ``message``. */}
        {turn.status === "error" && (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 8 }}
            message={t("playground.turn_failed")}
            description={turn.error || undefined}
            data-testid="playground-turn-error"
          />
        )}
        {hasText ? (
          <>
            {/* #11 — cap the answer block so a long (multi-step) answer
                scrolls inside its own container instead of stretching the
                page (420 mirrors TraceView's raw modal cap). */}
            <div
              ref={answerScrollRef}
              style={{ maxHeight: 420, overflowY: "auto" }}
              data-testid="playground-turn-answer-scroll"
            >
              {segments.map((seg, i) => {
                const isLast = i === segments.length - 1;
                // While streaming, the newest segment is still a candidate
                // answer — render it plainly; earlier segments are already
                // superseded (a later message exists), so they are
                // commentary regardless of their settled channel.
                const asCommentary =
                  turn.status === "running" ? !isLast : seg.channel === "commentary";
                if (asCommentary) {
                  return (
                    <CommentarySegmentLine
                      key={i}
                      text={seg.text}
                      label={t("playground.segment_commentary")}
                    />
                  );
                }
                return turn.status === "running" ? (
                  <Text key={i} style={{ whiteSpace: "pre-wrap", fontSize: 13 }}>
                    {seg.text}
                  </Text>
                ) : (
                  <MarkdownView key={i}>{seg.text}</MarkdownView>
                );
              })}
            </div>
            <FullTextTrigger
              onClick={() =>
                setFullText({ title: t("playground.view_full_text"), text: answerFullText })
              }
            />
          </>
        ) : turn.status === "running" ? (
          <Text style={{ whiteSpace: "pre-wrap", fontSize: 13 }}>
            {t("playground.turn_running")}
          </Text>
        ) : turn.status !== "error" ? (
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t("playground.turn_no_text")}
          </Text>
        ) : null}

        {/* A+B — inline download row for artifacts this turn registered. */}
        {turnArtifacts.length > 0 && (
          <div
            style={{
              marginTop: 8,
              display: "flex",
              gap: 6,
              flexWrap: "wrap",
              alignItems: "center",
            }}
            data-testid="playground-turn-artifacts"
          >
            <Text type="secondary" style={{ fontSize: 11 }}>
              {t("playground.workspace_artifacts")}:
            </Text>
            {turnArtifacts.map((a) => (
              <Button
                key={a.name}
                size="small"
                icon={<Download size={11} strokeWidth={1.75} />}
                loading={downloadingArtifact === a.name}
                onClick={() => void downloadArtifact(a.name)}
                aria-label={t("playground.artifact_download", { name: a.name })}
                data-testid="playground-turn-artifact-download"
              >
                {a.name}
              </Button>
            ))}
          </div>
        )}

        {/* #5 — approval gate (run paused on an approval-required tool). */}
        {!readOnly && turn.approval && threadId && (
          <ApprovalGate
            approval={turn.approval}
            busy={deciding}
            onDecide={(decision) => onDecide(turn.id, turn.approval!, decision)}
          />
        )}

        <TurnMeta
          summary={summary}
          costCny={costCny}
          runId={runId}
          threadId={threadId}
        />

        {/* SE-16 (SE-A46) — per-turn 👍/👎 quality signal feeding the
            skill-evolution curation pipeline. Settled turns only. */}
        {!readOnly && turn.status === "done" && threadId && (
          <FeedbackBar threadId={threadId} turnSeq={turnSeq} />
        )}
      </div>

      {/* Reasoning (collapsed) + events (expanded by default). Controlled
          (M3) — the growing-bar interval effect above needs to know whether
          the "events" panel is actually open. */}
      <Collapse
        ghost
        size="small"
        activeKey={eventsCollapseKeys}
        onChange={(keys) => setEventsCollapseKeys(typeof keys === "string" ? [keys] : keys)}
        items={[
          ...(summary.reasoning.length > 0
            ? [
                {
                  key: "reasoning",
                  label: t("playground.reasoning_label"),
                  children: (
                    <>
                      <pre
                        data-testid="playground-reasoning"
                        style={{
                          margin: 0,
                          fontSize: 11,
                          fontFamily: "var(--ew-font-mono)",
                          color: "var(--ew-text-secondary)",
                          whiteSpace: "pre-wrap",
                          wordBreak: "break-word",
                          maxHeight: 240,
                          overflow: "auto",
                        }}
                      >
                        {summary.reasoning.join("\n\n———\n\n")}
                      </pre>
                      {/* #9e — same full-text modal as the step cards. */}
                      <FullTextTrigger
                        onClick={() =>
                          setFullText({
                            title: t("playground.reasoning_label"),
                            text: summary.reasoning.join("\n\n———\n\n"),
                          })
                        }
                      />
                    </>
                  ),
                },
              ]
            : []),
          ...(hasAgentState ||
          retries.length > 0 ||
          compactions.length > 0 ||
          summary.perStepUsage.length > 0
            ? [
                {
                  key: "run-state",
                  label: t("playground.state_section"),
                  children: (
                    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                      {threadId && (
                        <PlanPanel
                          threadId={threadId}
                          runStatus={turn.status === "running" ? "running" : "success"}
                        />
                      )}
                      <AgentStatePanels
                        state={agentState}
                        retries={retries}
                        perStepUsage={summary.perStepUsage}
                      />
                      {compactions.length > 0 && (
                        <CompactionSummaryList items={compactions} />
                      )}
                    </div>
                  ),
                },
              ]
            : []),
          {
            key: "events",
            label: (
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 8,
                }}
              >
                <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                  {t("playground.events_label")}
                  {toolStats.total > 0 && (
                    <Tag bordered={false} style={{ margin: 0 }} data-testid="playground-tool-count">
                      {t(
                        toolStats.total === 1
                          ? "playground.tool_count_one"
                          : "playground.tool_count_other",
                        { count: toolStats.total },
                      )}
                    </Tag>
                  )}
                  {toolStats.failed > 0 && (
                    <Tag color="error" bordered={false} style={{ margin: 0 }} data-testid="playground-tool-failed">
                      {t("playground.tool_failed_count", { count: toolStats.failed })}
                    </Tag>
                  )}
                </span>
                {/* Toggle lives next to the content it switches; stop the click
                    from collapsing the panel. */}
                <span
                  onClick={(e) => e.stopPropagation()}
                  role="presentation"
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 8,
                  }}
                >
                  <Segmented<"timeline" | "raw" | "exact">
                    size="small"
                    value={eventView}
                    onChange={handleViewChange}
                    options={[
                      { value: "exact", label: t("event_stream.view_exact") },
                      {
                        value: "timeline",
                        label: t("event_stream.view_timeline"),
                      },
                      { value: "raw", label: t("event_stream.view_raw") },
                    ]}
                    data-testid="playground-event-view-toggle"
                  />
                  {/* Task 3 — Gantt-only affordance: opens the 92vw `expanded`
                      variant of the same model/renderDetail (FullTextModal
                      sizing precedent). */}
                  {eventView === "timeline" && (
                    <Button
                      size="small"
                      icon={<Maximize2 size={13} strokeWidth={1.75} />}
                      onClick={() => setGanttExpanded(true)}
                      title={t("playground.gantt_expand")}
                      aria-label={t("playground.gantt_expand")}
                      data-testid="playground-gantt-expand"
                    />
                  )}
                  <Button
                    size="small"
                    icon={<Download size={13} strokeWidth={1.75} />}
                    loading={exporting}
                    onClick={() => onExport(turn)}
                    title={t("playground.export_json_tip")}
                    aria-label={t("playground.export_json")}
                    data-testid="playground-export-json"
                  >
                    {t("playground.export_json")}
                  </Button>
                  {/* #10 — one unified retry button (success turns can re-run
                      too); danger tint marks a failed turn. Hidden while the
                      turn is still running or when no handler is wired (the
                      read-only conversation page). */}
                  {onRetry && turn.status !== "running" && (
                    <Button
                      size="small"
                      danger={turnFailed}
                      icon={<RotateCcw size={13} strokeWidth={1.75} />}
                      onClick={() => onRetry(turn)}
                      data-testid="playground-turn-retry"
                    >
                      {t("playground.retry")}
                    </Button>
                  )}
                  {langfuseUrl !== null && (
                    <Button
                      size="small"
                      type="link"
                      icon={<ExternalLink size={13} strokeWidth={1.75} />}
                      href={langfuseUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      data-testid="playground-turn-langfuse"
                    >
                      {t("trace_toolbar.open_in_langfuse")}
                    </Button>
                  )}
                </span>
              </div>
            ),
            children:
              turn.events.length === 0 ? (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {t("playground.empty_log")}
                </Text>
              ) : eventView === "timeline" ? (
                <>
                  {timelineBanner && (
                    <RunStatusBanner
                      status={timelineBanner.status}
                      summary={t("playground.rb_ok")}
                      errorLabel={
                        timelineBanner.status === "error"
                          ? (timelineBanner.errorStepCount != null
                              ? t("playground.tl_step", { n: timelineBanner.errorStepCount })
                              : (timelineBanner.errorText ?? t("playground.rb_ok")))
                          : undefined
                      }
                      errorMessage={
                        // errorText already fills errorLabel in the no-step
                        // fallback branch above — only surface it as the
                        // message when a step number owns the label, else the
                        // banner renders the same text twice.
                        timelineBanner.status === "error" &&
                        timelineBanner.errorStepCount != null
                          ? (timelineBanner.errorText ?? undefined)
                          : undefined
                      }
                      // I1 — restored: GanttTimeline rows now carry
                      // `data-error` (see gantt_timeline.ts's `hasError`),
                      // so jump-to-error has a DOM anchor again.
                      onJump={
                        timelineBanner.status === "error"
                          ? () => {
                              document
                                .querySelector(
                                  '[data-testid="gantt-timeline"] [data-error="true"]',
                                )
                                ?.scrollIntoView({ behavior: "smooth", block: "center" });
                            }
                          : undefined
                      }
                    />
                  )}
                  {gantt.degraded && (
                    <Text
                      type="secondary"
                      style={{ fontSize: 12, display: "block", marginBottom: 6 }}
                      data-testid="playground-gantt-degraded"
                    >
                      {t("playground.gantt_degraded")}
                    </Text>
                  )}
                  <GanttTimeline
                    model={ganttModel}
                    variant="embedded"
                    running={turn.status === "running"}
                    renderDetail={renderGanttDetail}
                  />
                  {/* C1 — the Gantt has no render surface for a still-
                      streaming step (it has no settled row yet, so it never
                      appears in `ganttModel.rows`); without this, switching
                      to the "timeline" view during a live run showed no
                      typewriter at all. `items={[]}` renders nothing but live
                      cards — settled steps stay Gantt-only, never duplicated
                      here, because `liveByStep` is pre-filtered to only the
                      unsettled steps above (see the C1-fix `settledSteps` /
                      `unsettledLiveByStep` memos): `StepTimeline`'s own
                      internal `settled` set is always empty here (it's
                      computed from `items`, which is always `[]` at this call
                      site), so the filtering has to happen on the way in. */}
                  <StepTimeline
                    items={[]}
                    liveByStep={unsettledLiveByStep}
                    ttftMs={ttftMs}
                    finalized={finalized}
                    hideLegend
                  />
                </>
              ) : eventView === "exact" ? (
                labeledTrace ? (
                  <>
                    {traceBanner && (
                      <RunStatusBanner
                        status={traceBanner.status}
                        summary={t("playground.rb_ok")}
                        metrics={
                          traceBanner.status === "ok"
                            ? [
                                {
                                  label: t("playground.tr_detail_latency"),
                                  value: fmtDuration(traceBanner.latencyMs ?? 0),
                                },
                                ...(traceBanner.totalCostUsd != null
                                  ? [
                                      {
                                        label: t("playground.tr_detail_cost"),
                                        value: `$${traceBanner.totalCostUsd.toFixed(4)}`,
                                      },
                                    ]
                                  : []),
                              ]
                            : undefined
                        }
                        errorLabel={traceBanner.errorLabel ?? undefined}
                        errorMessage={traceBanner.errorMessage ?? undefined}
                        onJump={
                          traceBanner.status === "error"
                            ? () => {
                                document
                                  .querySelector(
                                    '[data-testid="trace-view"] [data-error="true"]',
                                  )
                                  ?.scrollIntoView({ behavior: "smooth", block: "center" });
                              }
                            : undefined
                        }
                      />
                    )}
                    <TraceView
                      trace={labeledTrace}
                      threadId={threadId ?? undefined}
                      runId={runId ?? undefined}
                      onRefresh={() => {
                        traceRetriesRef.current = 0;
                        setTrace(null);
                      }}
                    />
                  </>
                ) : (
                  <Text
                    type="secondary"
                    style={{ fontSize: 12 }}
                    data-testid="playground-trace-loading"
                  >
                    {t("common.loading")}
                  </Text>
                )
              ) : (
                <div
                  style={{ display: "flex", flexDirection: "column", gap: 8 }}
                >
                  {turn.events.map((evt, idx) => (
                    <EventCard key={`${evt.receivedAt}-${idx}`} evt={evt} />
                  ))}
                </div>
              ),
          },
        ]}
      />
      <FullTextModal state={fullText} onClose={() => setFullText(null)} />
      {/* Task 3 — Gantt 放大 Modal: same model/renderDetail as the embedded
          copy above, just the `expanded` variant (wider label column, model
          name + duration always visible). Sizing matches FullTextModal's
          92vw precedent, widened for the axis. */}
      <Modal
        open={ganttExpanded}
        onCancel={() => setGanttExpanded(false)}
        footer={null}
        width="min(92vw, 1680px)"
        destroyOnHidden
        title={t("playground.gantt_expand")}
      >
        <GanttTimeline
          model={ganttModel}
          variant="expanded"
          running={turn.status === "running"}
          renderDetail={renderGanttDetail}
        />
      </Modal>
    </div>
  );
}
