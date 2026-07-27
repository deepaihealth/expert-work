/**
 * TraceView — Batch 4b Task 4. Renders a run's normalized Langfuse trace
 * (Task 3's `RunTrace`/`TraceSpan` facade DTO) as the wireframe v4
 * two-pane view: a left indented operation tree + a right time-axis
 * waterfall, row-aligned, with a below-the-fold detail panel for
 * whichever row is selected. See
 * docs/superpowers/specs/2026-07-11-batch4b-wireframe.html (`.wf` /
 * `.rw` / `.tw` / `.tdot` / `.gbar` / `.detail` / `.state`) for the
 * authoritative visual structure this transcribes.
 *
 * Scope notes (kept close to the Task description's explicit field
 * list rather than every wireframe pixel — see task-4-report.md):
 *  - Span `label`/`detail` are opaque passthrough text — the facade
 *    (control_plane/api/trace_facade.py `_classify`) already emits
 *    human-readable labels ("LLM 调用"/"工具调用"/…) and `detail` is raw
 *    data (tool name / call purpose), not something this component
 *    translates or reformats.
 *  - The detail panel's meta row sticks to the Task description's
 *    explicit field list (kind/latency/model/tokens/cost) — `TraceSpan`
 *    carries no absolute wall-clock timestamp, so the wireframe's
 *    "09:03:12 → …" range chip is dropped rather than fabricated.
 *  - Per-row subtree collapse (the wireframe's `tog()`) isn't
 *    implemented — every row always renders; only the detail panel's
 *    input/output sections are collapsible, per the Task description
 *    ("io 各可折叠").
 *  - The axis's endless background gridlines (`.axis .g`, a CSS
 *    `bottom:-2000px` hack) and the `.trace-sum` / `.legend` strips are
 *    skipped — they're outside the "核心结构" list in the task brief.
 */
import { useState, type CSSProperties, type KeyboardEvent } from "react";
import { Modal } from "antd";
import { Cog, RefreshCw, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  fetchRunTraceRaw,
  type RenderedMessage,
  type RunTrace,
  type RunTraceIo,
  type TraceSpan,
  type TraceStatus,
} from "../../../api/trace_facade";
import { fmtDuration } from "./duration_format";
import { buildRows, isWideBar, type TraceRowData } from "./trace_tree";
import { cleanUntrusted } from "./untrusted_clean";
import { EntryBreakdown } from "./EntryBreakdown";

const ACCENT = "var(--ew-text-info, #4c8dff)";
const SUCCESS = "var(--ew-text-success, #3ecf8e)";
const WARNING = "var(--ew-text-warning, #e8a33d)";
const DANGER = "var(--ew-text-danger, #f0616d)";
const PURPLE = "var(--ew-accent-violet, #b18cff)";
const MUTED = "var(--ew-text-tertiary)";
// Entry-chain spans (memory_recall / workspace_ingest / context_gates) get
// their own indigo tint — distinct from both the llm blue and tool violet
// families so the pre-first-token stretch reads apart from the rest of the
// waterfall at a glance. Same var + fallback as EntryBreakdown.tsx's bar so
// the breakdown and the waterfall agree on the colour.
const ENTRY = "var(--ew-trace-entry, #7c8cff)";
// Auxiliary LLM calls render in a muted LLM tint — still the LLM (blue) family
// so the kind reads at a glance, but dimmed so a background sub-call never
// competes with the main conversation for attention.
const AUX_LLM = `color-mix(in srgb, ${ACCENT} 48%, ${MUTED})`;
const LANE = "300px";

const ACTION_LINK_STYLE: CSSProperties = {
  border: 0,
  background: "transparent",
  color: ACCENT,
  cursor: "pointer",
  padding: 0,
  font: "inherit",
  fontSize: 11,
};

/** Best-effort clipboard copy — `navigator.clipboard` is unavailable in some
 *  test/embedded environments; silently no-op rather than throwing. */
function copyText(text: string): void {
  if (typeof navigator === "undefined" || !navigator.clipboard) return;
  navigator.clipboard.writeText(text).catch(() => {});
}

export interface TraceViewProps {
  trace: RunTrace;
  /** Called when the user clicks "Refresh" on the `not_ready` degraded
   *  state — the caller should trigger a refetch of `trace` (see
   *  PlaygroundTab.tsx). Omitted → the refresh button doesn't render. */
  onRefresh?: () => void;
  /** Identify the run whose trace this is — needed to fetch untruncated raw
   *  span content (fetchRunTraceRaw) for the detail panel's "查看原文" (view
   *  raw) action. Omitted → that action no-ops. */
  threadId?: string;
  runId?: string;
}

export function TraceView({ trace, onRefresh, threadId, runId }: TraceViewProps) {
  // `status: "ok"` doesn't type-narrow `trace`/`spans` into defined (both
  // stay optional on RunTrace) — guard defensively rather than asserting,
  // degrading to "unavailable" on a malformed ok payload instead of crashing.
  const spans = trace.spans;
  // The waterfall renders nothing unless there's at least one root span
  // (`parentId === null`) to start the tree walk from. Two ingestion-in-
  // progress shapes have spans yet no root: zero spans, or child spans that
  // landed before their session-root parent (every `parentId` dangles). Both
  // must surface as refreshable `not_ready`, never a bare axis with no bars.
  const hasRoot = Array.isArray(spans) && spans.some((s) => s.parentId === null);
  if (trace.status !== "ok" || !trace.trace || !spans || !hasRoot) {
    // A genuinely malformed ok (missing trace/spans) degrades to `unavailable`;
    // an ok trace that's simply still ingesting (no root yet) → `not_ready`.
    const status: Exclude<TraceStatus, "ok"> =
      trace.status !== "ok"
        ? trace.status
        : trace.trace && spans
          ? "not_ready"
          : "unavailable";
    return (
      <div data-testid="trace-view">
        <TraceStateCard status={status} onRefresh={onRefresh} />
      </div>
    );
  }

  return (
    <div data-testid="trace-view">
      <TraceTree spans={spans} totalMs={trace.trace.latencyMs} threadId={threadId} runId={runId} />
    </div>
  );
}


function TraceTree({
  spans,
  totalMs,
  threadId,
  runId,
}: {
  spans: readonly TraceSpan[];
  totalMs: number;
  threadId?: string;
  runId?: string;
}) {
  const { t } = useTranslation();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const rows = buildRows(spans);
  const selected = spans.find((s) => s.id === selectedId) ?? null;

  return (
    <div>
      <EntryBreakdown spans={spans} selectedId={selectedId} onSelect={setSelectedId} />
      <div
        style={{
          display: "grid",
          gridTemplateColumns: `${LANE} 1fr`,
          background: "var(--ew-surface-raised)",
          borderBottom: "1px solid var(--ew-border-strong)",
        }}
      >
        <div
          style={{
            padding: "6px 12px",
            fontSize: 11,
            letterSpacing: "0.05em",
            textTransform: "uppercase",
            color: MUTED,
          }}
        >
          {t("playground.tr_col_action")}
        </div>
        <div style={{ padding: "6px 12px" }}>
          <Axis totalMs={totalMs} />
        </div>
      </div>
      {rows.map((row) => (
        <TraceRow
          key={row.span.id}
          row={row}
          totalMs={totalMs}
          selected={row.span.id === selectedId}
          onSelect={() => setSelectedId(row.span.id)}
        />
      ))}
      {selected && (
        <TraceDetail
          key={selected.id}
          span={selected}
          onClose={() => setSelectedId(null)}
          threadId={threadId}
          runId={runId}
        />
      )}
    </div>
  );
}

function Axis({ totalMs }: { totalMs: number }) {
  const fractions = [0, 0.25, 0.5, 0.75, 1];
  return (
    <div style={{ position: "relative", height: 14 }}>
      {fractions.map((f, i) => (
        <span
          key={f}
          style={{
            position: "absolute",
            left: `${f * 100}%`,
            top: 0,
            fontFamily: "var(--ew-font-mono)",
            fontSize: 10,
            color: MUTED,
            fontVariantNumeric: "tabular-nums",
            transform:
              i === 0 ? undefined : i === fractions.length - 1 ? "translateX(-100%)" : "translateX(-50%)",
          }}
        >
          {fmtDuration(Math.round(f * totalMs))}
        </span>
      ))}
    </div>
  );
}

/** An auxiliary LLM call — memory extract/verify/reconcile, planner, reflect,
 *  compress, judge — as opposed to the main agent conversation turn. The facade
 *  tags each via the span's `purpose` ("main"/"" is the main conversation). */
function isAuxLlm(span: Pick<TraceSpan, "kind" | "purpose">): boolean {
  return span.kind === "llm" && span.purpose !== "" && span.purpose !== "main";
}

/** Error level overrides the kind-based color everywhere a span's dot/bar
 *  renders — an errored LLM/tool call is red first, its kind second. An
 *  auxiliary LLM call renders in a muted LLM tint so it reads apart from the
 *  main conversation without competing with it. */
function kindDotColor(span: Pick<TraceSpan, "kind" | "level" | "purpose" | "group">): string {
  if (span.level === "error") return DANGER;
  if (span.kind === "llm") return isAuxLlm(span) ? AUX_LLM : ACCENT;
  if (span.kind === "tool") return PURPLE;
  if (span.group === "entry") return ENTRY;
  return MUTED;
}

function kindBarColor(span: Pick<TraceSpan, "kind" | "level" | "purpose" | "group">): string {
  if (span.level === "error") return DANGER;
  if (span.kind === "llm")
    return `color-mix(in srgb, ${isAuxLlm(span) ? AUX_LLM : ACCENT} 62%, transparent)`;
  if (span.kind === "tool") return PURPLE;
  if (span.group === "entry") return `color-mix(in srgb, ${ENTRY} 62%, transparent)`;
  return `color-mix(in srgb, ${MUTED} 45%, transparent)`;
}

function TreeGuideCell({ variant }: { variant: "v" | "elbow" | "blank" }) {
  return (
    <span aria-hidden style={{ position: "relative", width: 17, flex: "0 0 auto", alignSelf: "stretch" }}>
      {variant === "v" && (
        <span
          style={{
            position: "absolute",
            left: 8,
            top: 0,
            bottom: 0,
            width: 1,
            background: "var(--ew-border-strong)",
          }}
        />
      )}
      {variant === "elbow" && (
        <>
          <span
            style={{
              position: "absolute",
              left: 8,
              top: 0,
              height: "50%",
              width: 1,
              background: "var(--ew-border-strong)",
            }}
          />
          <span
            style={{
              position: "absolute",
              left: 8,
              top: "50%",
              width: 9,
              height: 1,
              background: "var(--ew-border-strong)",
            }}
          />
        </>
      )}
    </span>
  );
}

function GanttBar({ span, totalMs }: { span: TraceSpan; totalMs: number }) {
  const leftPct = totalMs > 0 ? (span.startMs / totalMs) * 100 : 0;
  const widthPct = totalMs > 0 ? (span.latencyMs / totalMs) * 100 : 0;
  const wide = isWideBar(widthPct);
  return (
    <div
      data-testid="trace-bar"
      style={{
        position: "absolute",
        left: `${leftPct}%`,
        width: `${widthPct}%`,
        minWidth: 3,
        height: 15,
        borderRadius: 3,
        display: "flex",
        alignItems: "center",
        background: kindBarColor(span),
      }}
    >
      <span
        style={{
          fontFamily: "var(--ew-font-mono)",
          fontVariantNumeric: "tabular-nums",
          fontSize: 11,
          whiteSpace: "nowrap",
          color: wide ? "var(--ew-text-primary)" : "var(--ew-text-secondary)",
          marginLeft: wide ? 6 : "calc(100% + 6px)",
        }}
      >
        {fmtDuration(span.latencyMs)}
      </span>
    </div>
  );
}

function TraceRow({
  row,
  totalMs,
  selected,
  onSelect,
}: {
  row: TraceRowData;
  totalMs: number;
  selected: boolean;
  onSelect: () => void;
}) {
  const { t } = useTranslation();
  const { span, depth, continues } = row;
  const isError = span.level === "error";

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>): void => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onSelect();
    }
  };

  return (
    <div
      data-testid="trace-row"
      data-error={isError ? "true" : undefined}
      role="button"
      tabIndex={0}
      onClick={onSelect}
      onKeyDown={onKeyDown}
      style={{
        display: "grid",
        gridTemplateColumns: `${LANE} 1fr`,
        alignItems: "stretch",
        minHeight: 31,
        borderTop: "1px solid var(--ew-border-subtle)",
        cursor: "pointer",
        background: selected
          ? `color-mix(in srgb, ${isError ? DANGER : ACCENT} ${isError ? 15 : 13}%, transparent)`
          : isError
            ? `color-mix(in srgb, ${DANGER} 8%, transparent)`
            : undefined,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 7, padding: "0 12px 0 0", minWidth: 0 }}>
        {continues.map((cont, i) => (
          <TreeGuideCell key={i} variant={i === depth - 1 ? "elbow" : cont ? "v" : "blank"} />
        ))}
        <span aria-hidden style={{ width: 14, textAlign: "center", color: MUTED, fontSize: 10, flex: "0 0 auto" }}>
          ·
        </span>
        <span
          aria-hidden
          style={{ width: 8, height: 8, borderRadius: 2, flex: "0 0 auto", background: kindDotColor(span) }}
        />
        {isAuxLlm(span) && (
          <Cog
            data-testid="trace-aux-marker"
            aria-label={t("playground.tr_aux_llm")}
            size={11}
            style={{ color: AUX_LLM, flex: "0 0 auto" }}
          />
        )}
        <span
          style={{
            fontFamily: "var(--ew-font-mono)",
            fontSize: 12.5,
            color: "var(--ew-text-primary)",
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
            minWidth: 0,
          }}
        >
          {span.label}
          {span.detail !== null && <span style={{ color: MUTED }}> · {span.detail}</span>}
        </span>
        <span style={{ display: "inline-flex", gap: 5, flex: "0 0 auto" }}>
          {span.model !== null && (
            <span
              data-testid="trace-model-chip"
              style={{
                fontSize: 11,
                fontFamily: "var(--ew-font-mono)",
                padding: "0 5px",
                borderRadius: 4,
                color: ACCENT,
                background: `color-mix(in srgb, ${ACCENT} 12%, transparent)`,
              }}
            >
              {span.model}
            </span>
          )}
          {span.costUsd !== null && (
            <span
              data-testid="trace-cost-chip"
              style={{
                fontSize: 11,
                fontFamily: "var(--ew-font-mono)",
                padding: "0 5px",
                borderRadius: 4,
                color: SUCCESS,
                background: `color-mix(in srgb, ${SUCCESS} 13%, transparent)`,
              }}
            >
              ${span.costUsd.toFixed(4)}
            </span>
          )}
        </span>
      </div>
      <div style={{ position: "relative", padding: "0 12px", display: "flex", alignItems: "center" }}>
        <GanttBar span={span} totalMs={totalMs} />
      </div>
    </div>
  );
}

/** `RenderedMessage.role` is LangChain's raw `type` string (`system` /
 *  `human` / `ai` / `tool` / occasionally something else) — colors match the
 *  wireframe's `.role.*` classes; anything unrecognized falls back to the
 *  same muted tone as `system`. */
function roleColor(role: string): string {
  if (role === "human") return ACCENT;
  if (role === "ai") return PURPLE;
  if (role === "tool") return SUCCESS;
  return MUTED;
}

function UntrustedBadge() {
  const { t } = useTranslation();
  return (
    <span
      data-testid="msg-untrusted"
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        fontSize: 10,
        fontFamily: "var(--ew-font-mono)",
        color: WARNING,
        border: `1px solid color-mix(in srgb, ${WARNING} 40%, var(--ew-border-subtle))`,
        borderRadius: 4,
        padding: "0 6px",
        flex: "0 0 auto",
      }}
    >
      ⚑ {t("playground.tr_msg_untrusted")}
    </span>
  );
}

function TruncationRow({
  fullChars,
  copySource,
  onViewRaw,
}: {
  fullChars: number;
  copySource: string;
  onViewRaw?: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        marginTop: 8,
        paddingTop: 8,
        borderTop: "1px dashed var(--ew-border-subtle)",
        fontSize: 11,
        color: MUTED,
        flexWrap: "wrap",
      }}
    >
      <span
        style={{
          fontFamily: "var(--ew-font-mono)",
          color: WARNING,
          border: `1px solid color-mix(in srgb, ${WARNING} 40%, var(--ew-border-subtle))`,
          borderRadius: 4,
          padding: "0 6px",
        }}
      >
        {t("playground.tr_msg_truncated", { n: fullChars })}
      </span>
      <button type="button" onClick={() => copyText(copySource)} style={ACTION_LINK_STYLE}>
        {t("playground.tr_msg_copy")}
      </button>
      <span aria-hidden>·</span>
      {/* `onViewRaw` is bound in `TraceDetail` to the field (input/output)
       *  this row belongs to and fetches the untruncated raw text via
       *  fetchRunTraceRaw; optional so isolated renders stay inert. */}
      <button type="button" onClick={() => onViewRaw?.()} style={ACTION_LINK_STYLE}>
        {t("playground.tr_msg_raw")}
      </button>
    </div>
  );
}

/** One structured chat message inside an `IoSection` whose `io.kind ===
 *  "messages"` (LLM span i/o). Independently collapsible — `system` starts
 *  collapsed (usually the bulk of the token budget: skill/tool defs), every
 *  other role starts expanded. Content is run through `cleanUntrusted`
 *  before rendering (spotlight-fenced tool output is common here); the raw,
 *  unclean text is only ever shown by the "查看原文" raw endpoint. */
function MessageBlock({
  message,
  onViewRaw,
}: {
  message: RenderedMessage;
  onViewRaw?: () => void;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(message.role !== "system");
  const toggle = (): void => setExpanded((v) => !v);
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>): void => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      toggle();
    }
  };

  const { text: cleaned, hadUntrusted } = cleanUntrusted(message.content);
  const hasToolCalls = message.toolCalls !== null && message.toolCalls.length > 0;
  const showToolCall = message.content === "" && hasToolCalls;
  const color = roleColor(message.role);

  return (
    <div
      data-testid="trace-message"
      style={{
        border: "1px solid var(--ew-border-subtle)",
        borderRadius: 6,
        background: "var(--ew-surface-base)",
        overflow: "hidden",
      }}
    >
      <div
        role="button"
        tabIndex={0}
        onClick={toggle}
        onKeyDown={onKeyDown}
        style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 10px", fontSize: 12, cursor: "pointer" }}
      >
        <span aria-hidden style={{ color: MUTED, fontSize: 10, width: 9, flex: "0 0 auto" }}>
          {expanded ? "▾" : "▸"}
        </span>
        <span
          style={{
            fontFamily: "var(--ew-font-mono)",
            fontSize: 11,
            padding: "0 6px",
            borderRadius: 4,
            letterSpacing: "0.03em",
            flex: "0 0 auto",
            color,
            background: `color-mix(in srgb, ${color} 14%, transparent)`,
          }}
        >
          {message.role}
        </span>
        {hadUntrusted && <UntrustedBadge />}
        <span
          style={{
            marginLeft: "auto",
            fontFamily: "var(--ew-font-mono)",
            fontSize: 10.5,
            color: MUTED,
            flex: "0 0 auto",
          }}
        >
          {t("playground.tr_msg_chars", { n: message.fullChars })}
        </span>
      </div>
      {expanded && (
        <div style={{ padding: "4px 11px 10px 11px", borderTop: "1px solid var(--ew-border-subtle)" }}>
          {showToolCall ? (
            <span style={{ fontFamily: "var(--ew-font-mono)", fontSize: 12, color: PURPLE }}>
              {t("playground.tr_msg_toolcall", { name: (message.toolCalls ?? []).join(", ") })}
            </span>
          ) : (
            <pre
              style={{
                margin: 0,
                fontFamily: "var(--ew-font-mono)",
                fontSize: 12,
                lineHeight: 1.55,
                color: "var(--ew-text-secondary)",
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
                maxHeight: 280,
                overflow: "auto",
              }}
            >
              {cleaned}
            </pre>
          )}
          {message.truncated && !showToolCall && (
            <TruncationRow fullChars={message.fullChars} copySource={cleaned} onViewRaw={onViewRaw} />
          )}
        </div>
      )}
    </div>
  );
}

function IoSection({
  testId,
  title,
  hint,
  io,
  onViewRaw,
}: {
  testId: string;
  title: string;
  hint?: string;
  io: RunTraceIo | null;
  onViewRaw?: () => void;
}) {
  const [expanded, setExpanded] = useState(true);
  if (io === null) return null;

  const toggle = (): void => setExpanded((v) => !v);
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>): void => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      toggle();
    }
  };

  return (
    <div data-testid={testId} style={{ borderBottom: "1px solid var(--ew-border-subtle)" }}>
      <div
        role="button"
        tabIndex={0}
        onClick={toggle}
        onKeyDown={onKeyDown}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "8px 13px",
          fontSize: 12,
          color: "var(--ew-text-secondary)",
          cursor: "pointer",
        }}
      >
        <span aria-hidden style={{ color: MUTED, fontSize: 10 }}>
          {expanded ? "▾" : "▸"}
        </span>
        {title}
        {hint !== undefined && (
          <span
            style={{
              fontSize: 10,
              color: MUTED,
              border: "1px solid var(--ew-border-subtle)",
              borderRadius: 999,
              padding: "0 6px",
            }}
          >
            {hint}
          </span>
        )}
      </div>
      {expanded &&
        (io.kind === "messages" ? (
          <div style={{ padding: "2px 13px 12px 13px", display: "flex", flexDirection: "column", gap: 5 }}>
            {io.messages.map((message, i) => (
              <MessageBlock key={i} message={message} onViewRaw={onViewRaw} />
            ))}
          </div>
        ) : (
          <IoText io={io} onViewRaw={onViewRaw} />
        ))}
    </div>
  );
}

function IoText({
  io,
  onViewRaw,
}: {
  io: Extract<RunTraceIo, { kind: "text" }>;
  onViewRaw?: () => void;
}) {
  const { text: cleaned, hadUntrusted } = cleanUntrusted(io.text);
  return (
    <div style={{ padding: "0 13px 12px 30px" }}>
      {hadUntrusted && <UntrustedBadge />}
      <pre
        style={{
          margin: 0,
          fontFamily: "var(--ew-font-mono)",
          fontSize: 12,
          lineHeight: 1.55,
          color: "var(--ew-text-secondary)",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          maxHeight: 280,
          overflow: "auto",
        }}
      >
        {cleaned}
      </pre>
      {io.truncated && <TruncationRow fullChars={io.fullChars} copySource={cleaned} onViewRaw={onViewRaw} />}
    </div>
  );
}

/** `IoSection` title/hint pick kind-aware copy: LLM spans carry a chat
 *  message list ("对话消息"/"回复"), tool spans carry a single args/result
 *  payload ("参数"/"结果"); anything else (session/generic span) falls back
 *  to the generic in/out labels with no hint chip. */
function ioLabels(
  kind: TraceSpan["kind"],
  t: (key: string, options?: Record<string, unknown>) => string,
): { inTitle: string; inHint?: string; outTitle: string; outHint?: string } {
  if (kind === "llm") {
    return {
      inTitle: t("playground.tr_io_llm_msgs"),
      inHint: t("playground.tr_io_llm_msgs_hint"),
      outTitle: t("playground.tr_io_llm_out"),
      outHint: t("playground.tr_io_llm_out_hint"),
    };
  }
  if (kind === "tool") {
    return {
      inTitle: t("playground.tr_io_tool_args"),
      inHint: t("playground.tr_io_tool_args_hint"),
      outTitle: t("playground.tr_io_tool_result"),
      outHint: t("playground.tr_io_tool_result_hint"),
    };
  }
  return {
    inTitle: t("playground.tr_io_in"),
    outTitle: t("playground.tr_io_out"),
  };
}

/** State for the "查看原文" (view raw) modal — `null` while closed. Lives in
 *  `TraceDetail` (not `IoSection`/`MessageBlock`) so a single modal serves
 *  both the input and output sections; `TraceDetail` remounts per span (its
 *  `key={selected.id}` in `TraceTree`), so this resets on span change. */
type RawViewState = { status: "loading" | "ok" | "error"; content: string };

function TraceDetail({
  span,
  onClose,
  threadId,
  runId,
}: {
  span: TraceSpan;
  onClose: () => void;
  threadId?: string;
  runId?: string;
}) {
  const { t } = useTranslation();
  const tokenParts: string[] = [];
  if (span.inputTokens !== null) tokenParts.push(`in ${span.inputTokens}`);
  if (span.outputTokens !== null) tokenParts.push(`out ${span.outputTokens}`);
  const labels = ioLabels(span.kind, t);
  const isError = span.level === "error";

  const [rawView, setRawView] = useState<RawViewState | null>(null);

  // threadId/runId are only available once the caller knows which run this
  // trace belongs to (PlaygroundTab.tsx) — isolated renders (tests, or a
  // trace shown without run context) leave the action a no-op rather than
  // throwing.
  const handleViewRaw = async (field: "input" | "output"): Promise<void> => {
    if (threadId === undefined || runId === undefined) return;
    setRawView({ status: "loading", content: "" });
    try {
      const content = await fetchRunTraceRaw(threadId, runId, span.id, field);
      setRawView({ status: "ok", content });
    } catch {
      setRawView({ status: "error", content: "" });
    }
  };

  return (
    <div
      data-testid="trace-detail"
      style={{
        border: "1px solid var(--ew-border-strong)",
        borderTop: 0,
        borderRadius: "0 0 6px 6px",
        background: "var(--ew-surface-raised)",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          flexWrap: "wrap",
          padding: "10px 13px",
          borderBottom: "1px solid var(--ew-border-subtle)",
        }}
      >
        <span style={{ display: "inline-flex", alignItems: "center", gap: 7, fontSize: 13, fontWeight: 600 }}>
          <span aria-hidden style={{ width: 8, height: 8, borderRadius: 2, background: kindDotColor(span) }} />
          {isAuxLlm(span) && (
            <Cog
              data-testid="trace-aux-marker"
              aria-label={t("playground.tr_aux_llm")}
              size={12}
              style={{ color: AUX_LLM, flex: "0 0 auto" }}
            />
          )}
          {span.label}
          {span.detail !== null && (
            <span style={{ color: MUTED, fontWeight: 400 }}> · {span.detail}</span>
          )}
        </span>
        <span
          style={{
            display: "flex",
            gap: 12,
            flexWrap: "wrap",
            fontFamily: "var(--ew-font-mono)",
            fontVariantNumeric: "tabular-nums",
            fontSize: 11.5,
            color: MUTED,
          }}
        >
          <span>{span.kind.toUpperCase()}</span>
          <span>
            {t("playground.tr_detail_latency")}{" "}
            <b style={{ color: "var(--ew-text-secondary)", fontWeight: 500 }}>{fmtDuration(span.latencyMs)}</b>
          </span>
          {span.model !== null && (
            <span>
              {t("playground.tr_detail_model")}{" "}
              <b style={{ color: "var(--ew-text-secondary)", fontWeight: 500 }}>{span.model}</b>
            </span>
          )}
          {tokenParts.length > 0 && (
            <span>
              {t("playground.tr_detail_tokens")}{" "}
              <b style={{ color: "var(--ew-text-secondary)", fontWeight: 500 }}>{tokenParts.join(" / ")}</b>
            </span>
          )}
          {span.costUsd !== null && (
            <span>
              {t("playground.tr_detail_cost")}{" "}
              <b style={{ color: "var(--ew-text-secondary)", fontWeight: 500 }}>${span.costUsd.toFixed(4)}</b>
            </span>
          )}
        </span>
        <button
          type="button"
          onClick={onClose}
          aria-label={t("playground.tr_detail_close")}
          data-testid="trace-detail-close"
          style={{
            marginLeft: "auto",
            border: 0,
            background: "transparent",
            color: MUTED,
            cursor: "pointer",
            padding: "0 4px",
          }}
        >
          <X size={14} strokeWidth={1.75} />
        </button>
      </div>
      {isError && (
        <div
          data-testid="trace-detail-error"
          style={{
            display: "flex",
            gap: 9,
            alignItems: "flex-start",
            padding: "9px 13px",
            borderBottom: "1px solid var(--ew-border-subtle)",
            background: `color-mix(in srgb, ${DANGER} 9%, transparent)`,
            color: "var(--ew-text-primary)",
            fontSize: 12.5,
          }}
        >
          <span
            style={{
              fontSize: 10,
              fontFamily: "var(--ew-font-mono)",
              color: DANGER,
              border: `1px solid color-mix(in srgb, ${DANGER} 45%, var(--ew-border-subtle))`,
              borderRadius: 4,
              padding: "0 6px",
              flex: "0 0 auto",
              marginTop: 1,
            }}
          >
            ERROR
          </span>
          {span.statusMessage !== null && <span>{span.statusMessage}</span>}
        </div>
      )}
      <IoSection
        testId="trace-io-input"
        title={labels.inTitle}
        hint={labels.inHint}
        io={span.input}
        onViewRaw={() => void handleViewRaw("input")}
      />
      <IoSection
        testId="trace-io-output"
        title={labels.outTitle}
        hint={labels.outHint}
        io={span.output}
        onViewRaw={() => void handleViewRaw("output")}
      />
      <Modal
        open={rawView !== null}
        onCancel={() => setRawView(null)}
        footer={null}
        title={t("playground.tr_msg_raw")}
        destroyOnHidden
      >
        {/* testid on the content wrapper — antd forwards it to the modal
         *  root (see CreateBaseModal.tsx's same convention). */}
        <div data-testid="trace-raw-modal">
          {rawView?.status === "loading" && <span style={{ color: MUTED }}>{t("common.loading")}</span>}
          {rawView?.status === "error" && (
            <span style={{ color: DANGER }}>{t("playground.tr_raw_error")}</span>
          )}
          {rawView?.status === "ok" && (
            <pre
              style={{
                margin: 0,
                fontFamily: "var(--ew-font-mono)",
                fontSize: 12,
                lineHeight: 1.55,
                color: "var(--ew-text-secondary)",
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
                maxHeight: 420,
                overflow: "auto",
              }}
            >
              {rawView.content}
            </pre>
          )}
        </div>
      </Modal>
    </div>
  );
}

const STATE_TONE: Record<Exclude<TraceStatus, "ok">, string> = {
  not_ready: WARNING,
  unavailable: DANGER,
  no_trace: MUTED,
};

function TraceStateCard({
  status,
  onRefresh,
}: {
  status: Exclude<TraceStatus, "ok">;
  onRefresh?: () => void;
}) {
  const { t } = useTranslation();
  const copy: Record<Exclude<TraceStatus, "ok">, { title: string; msg: string }> = {
    not_ready: {
      title: t("playground.tr_state_not_ready_title"),
      msg: t("playground.tr_state_not_ready_msg"),
    },
    unavailable: {
      title: t("playground.tr_state_unavailable_title"),
      msg: t("playground.tr_state_unavailable_msg"),
    },
    no_trace: {
      title: t("playground.tr_state_no_trace_title"),
      msg: t("playground.tr_state_no_trace_msg"),
    },
  };
  const { title, msg } = copy[status];

  return (
    <div
      data-testid="trace-state"
      style={{
        border: "1px solid var(--ew-border-subtle)",
        borderRadius: 6,
        background: "var(--ew-surface-base)",
        padding: "14px 13px",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 12, marginBottom: 7 }}>
        <span
          aria-hidden
          style={{ width: 7, height: 7, borderRadius: "50%", flex: "0 0 auto", background: STATE_TONE[status] }}
        />
        <strong>{title}</strong>
      </div>
      <p style={{ margin: 0, color: "var(--ew-text-secondary)", fontSize: 12.5, lineHeight: 1.5 }}>{msg}</p>
      {status === "not_ready" && onRefresh && (
        <button
          type="button"
          data-testid="trace-refresh"
          onClick={onRefresh}
          style={{
            marginTop: 9,
            font: "inherit",
            fontSize: 12,
            border: "1px solid var(--ew-border-strong)",
            background: "var(--ew-surface-raised)",
            color: "var(--ew-text-secondary)",
            padding: "3px 11px",
            borderRadius: 5,
            cursor: "pointer",
            display: "inline-flex",
            alignItems: "center",
            gap: 5,
          }}
        >
          <RefreshCw size={12} strokeWidth={1.75} />
          {t("common.refresh")}
        </button>
      )}
    </div>
  );
}
