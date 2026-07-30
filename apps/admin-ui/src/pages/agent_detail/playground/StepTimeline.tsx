/**
 * StepTimeline — renders a turn's `TimelineItem[]` (Task 1's `parseTimeline`
 * output) as the wireframe's typed execution-trace axis: agent steps as big
 * cards (per-step reasoning + nested tool cards / final answer), memory /
 * planner / reflect channels as light collapsible rows, and
 * compaction / retry / error / approval / end as axis markers — all in
 * arrival order. See docs/superpowers/specs/2026-07-10-batch3-wireframe.html
 * (`.timeline` / `.step` / `.node-row` / `.marker` / `.legend`) for the
 * authoritative visual structure this transcribes.
 */
import { useState, type CSSProperties, type KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";

import type { AgentStep, AuxNodeItem, MarkerItem, TimelineItem } from "../../../api/timeline";
import type { FireNowResult } from "../../../api/triggers";
import type { WorkerStatus, WorkerTimeline } from "../../../api/worker_timeline";
import { ToolCallCard } from "../../../components/ToolTimeline";
import {
  FullTextModal,
  FullTextTrigger,
  type FullTextState,
} from "../../../components/turn/FullTextModal";
import { fmtDuration } from "./duration_format";
import { StreamingStepCard } from "./StreamingStepCard";
import type { LiveStep } from "./useTokenStream";

// Semantic colors as CSS vars with wireframe-matching hex fallbacks — same
// `var(--ew-*, #hex)` convention Batch 1/2 established (ToolTimeline.tsx /
// AgentStatePanels.tsx already use `--ew-text-danger` this way).
const DANGER = "var(--ew-text-danger, #cf1322)";
const SUCCESS = "var(--ew-text-success, #3ecf8e)";
const WARNING = "var(--ew-text-warning, #e8a33d)";
const PURPLE = "var(--ew-accent-violet, #a855f7)";
const INFO = "var(--ew-text-info, #4c8dff)";

export interface StepTimelineProps {
  items: readonly TimelineItem[];
  /** Live token buffers by step (子项目 3a content + 3b reasoning/tool); absent for history/non-streaming turns. */
  liveByStep?: ReadonlyMap<number, LiveStep>;
  /** TTFT to show on the synthetic streaming card. */
  ttftMs?: number | null;
  /** Run ended — orphan live steps (no authoritative card) render as interrupted. */
  finalized?: boolean;
  /** Spec 1 PR4 Task 4 — see ``ToolTimelineProps.onFireResult``; threaded
   *  down to each ``manage_task`` tool card's 「立即触发」 button. */
  onFireResult?: (result: FireNowResult) => void;
}

/** Renders `fmtDuration(ms)` with the `tl_duration` aria-label — used for the
 *  agent step head, the nested tool card, and the aux node row. Hidden
 *  (renders nothing, no placeholder) when `ms` is `null` (Task 4 didn't
 *  observe a duration for this step). */
function DurationBadge({ ms, style }: { ms: number | null; style?: CSSProperties }) {
  const { t } = useTranslation();
  if (ms === null) return null;
  return (
    <span aria-label={t("playground.tl_duration")} style={style}>
      {fmtDuration(ms)}
    </span>
  );
}

export function StepTimeline({
  items,
  liveByStep,
  ttftMs = null,
  finalized = false,
  onFireResult,
}: StepTimelineProps) {
  // Steps that already have an authoritative card — their live buffer is superseded.
  const settled = new Set<number>();
  for (const it of items) {
    if (it.kind === "agent" && it.stepCount !== null) settled.add(it.stepCount);
  }
  const liveCards = [...(liveByStep ?? new Map<number, LiveStep>())]
    .filter(([step]) => !settled.has(step))
    .sort(([a], [b]) => a - b);

  if (items.length === 0 && liveCards.length === 0) return null;

  return (
    <div>
      <div data-testid="step-timeline" style={{ position: "relative", paddingLeft: 26 }}>
        <span
          aria-hidden
          style={{
            position: "absolute",
            left: 9,
            top: 4,
            bottom: 4,
            width: 2,
            background: "var(--ew-border-default)",
            borderRadius: 2,
          }}
        />
        {items.map((item) => {
          switch (item.kind) {
            case "agent":
              return <AgentStepCard key={item.seq} item={item} onFireResult={onFireResult} />;
            case "compaction":
            case "retry":
            case "error":
            case "approval":
            case "guard":
            case "end":
              return <MarkerRow key={item.seq} item={item} />;
            default:
              return <AuxNodeRow key={item.seq} item={item} />;
          }
        })}
        {liveCards.map(([step, live]) => (
          <StreamingStepCard
            key={`live-${step}`}
            step={step}
            live={live}
            interrupted={finalized}
            ttftMs={ttftMs}
          />
        ))}
      </div>
      <Legend />
    </div>
  );
}

function AgentStepCard({
  item,
  onFireResult,
}: {
  item: AgentStep;
  onFireResult?: (result: FireNowResult) => void;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(item.hasError);
  // #9d — 「查看全文」 modal, shared by this step's reasoning + output areas.
  const [fullText, setFullText] = useState<FullTextState | null>(null);
  const toggle = (): void => setExpanded((v) => !v);
  const onHeadKeyDown = (e: KeyboardEvent): void => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      toggle();
    }
  };

  // #9a — a tool-calling step can carry its own content too; the collapsed
  // caret hints at both (tools count · output/final-answer label).
  const contentLabel =
    item.tools.length > 0
      ? t("playground.tl_step_output")
      : t("playground.tl_final_answer");
  const caretSuffix = [
    item.tools.length > 0
      ? t(item.tools.length === 1 ? "playground.tool_count_one" : "playground.tool_count_other", {
          count: item.tools.length,
        })
      : null,
    item.content ? contentLabel : null,
  ]
    .filter((s): s is string => s !== null)
    .join(" · ");

  return (
    <div style={{ position: "relative", marginBottom: 10 }}>
      <span
        aria-hidden
        style={{
          position: "absolute",
          left: -22,
          top: 13,
          width: 11,
          height: 11,
          borderRadius: "50%",
          background: item.hasError
            ? `color-mix(in srgb, ${DANGER} 30%, var(--ew-surface-base))`
            : "var(--ew-surface-base)",
          border: `2px solid ${item.hasError ? DANGER : SUCCESS}`,
        }}
      />
      <div
        data-testid="step-card"
        data-error={item.hasError ? "true" : undefined}
        style={{
          border: `1px solid ${
            item.hasError
              ? `color-mix(in srgb, ${DANGER} 40%, var(--ew-border-subtle))`
              : "var(--ew-border-subtle)"
          }`,
          borderRadius: 6,
          background: "var(--ew-surface-base)",
          overflow: "hidden",
        }}
      >
        <div
          data-testid="step-head"
          role="button"
          tabIndex={0}
          onClick={toggle}
          onKeyDown={onHeadKeyDown}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "8px 11px",
            cursor: "pointer",
            flexWrap: "wrap",
            background: item.hasError ? `color-mix(in srgb, ${DANGER} 8%, transparent)` : undefined,
          }}
        >
          <span style={{ fontWeight: 650, fontSize: 13 }}>
            {t("playground.tl_step", { n: item.stepCount ?? "?" })}
          </span>
          <span
            style={{ fontFamily: "var(--ew-font-mono)", fontSize: 11, color: "var(--ew-text-tertiary)" }}
          >
            {item.node}
          </span>
          <div
            style={{
              display: "flex",
              gap: 6,
              flexWrap: "wrap",
              alignItems: "center",
              fontSize: 11,
              color: "var(--ew-text-secondary)",
            }}
          >
            {item.model && <span>{item.model}</span>}
            {item.hasError ? (
              <span style={{ color: DANGER }}>{t("playground.tl_tool_failed")}</span>
            ) : (
              item.finishReason && <span>{t("playground.tl_finish", { reason: item.finishReason })}</span>
            )}
            <span>{item.totalTokens} tok</span>
            <DurationBadge ms={item.durationMs} />
          </div>
          <span style={{ marginLeft: "auto", color: "var(--ew-text-tertiary)", fontSize: 11 }}>
            {expanded ? "▾" : caretSuffix ? `▸ ${caretSuffix}` : "▸"}
          </span>
        </div>
        {expanded && (
          <div style={{ padding: "2px 11px 11px", borderTop: "1px solid var(--ew-border-subtle)" }}>
            {item.reasoning && (
              <div
                style={{
                  marginTop: 8,
                  borderLeft: `2px solid color-mix(in srgb, ${PURPLE} 55%, var(--ew-border-subtle))`,
                  paddingLeft: 10,
                }}
              >
                <div
                  style={{
                    fontSize: 10.5,
                    letterSpacing: "0.05em",
                    textTransform: "uppercase",
                    color: PURPLE,
                  }}
                >
                  {t("playground.tl_reasoning")}
                </div>
                <p
                  style={{
                    margin: "3px 0 0",
                    fontSize: 12,
                    color: "var(--ew-text-secondary)",
                    fontStyle: "italic",
                    // #9b — long CoT collapsed into one blob without these
                    // (reads as truncation); StreamingStepCard already wraps.
                    whiteSpace: "pre-wrap",
                    wordBreak: "break-word",
                    // I4 — cap the expanded reasoning (same 240 as TurnCard's
                    // turn-level reasoning); full text lives in the modal.
                    maxHeight: 240,
                    overflowY: "auto",
                  }}
                >
                  {item.reasoning}
                </p>
                <FullTextTrigger
                  onClick={() =>
                    setFullText({
                      title: t("playground.tl_reasoning"),
                      text: item.reasoning ?? "",
                    })
                  }
                />
              </div>
            )}
            {item.tools.map((tool) => (
              <div
                key={tool.id}
                style={{
                  position: "relative",
                  marginTop: 8,
                  paddingRight: tool.durationMs !== null ? 56 : 0,
                }}
              >
                <ToolCallCard entry={tool} onFireResult={onFireResult} />
                <DurationBadge
                  ms={tool.durationMs}
                  style={{
                    position: "absolute",
                    top: 2,
                    right: 0,
                    fontSize: 11,
                    color: "var(--ew-text-tertiary)",
                  }}
                />
                {tool.workers ? <WorkerSubTimeline workers={tool.workers} /> : null}
              </div>
            ))}
            {/* #9a — render the step's own content whether or not it called
                tools (the old `tools.length === 0` gate dropped a tool-calling
                step's text entirely). */}
            {item.content && (
              <div style={{ paddingTop: 8 }}>
                {item.tools.length > 0 && (
                  <div
                    style={{
                      fontSize: 10.5,
                      letterSpacing: "0.05em",
                      textTransform: "uppercase",
                      color: "var(--ew-text-tertiary)",
                    }}
                  >
                    {t("playground.tl_step_output")}
                  </div>
                )}
                <div
                  style={{
                    fontSize: 13,
                    color: "var(--ew-text-secondary)",
                    whiteSpace: "pre-wrap",
                    wordBreak: "break-word",
                    // I4 — cap the expanded step output the same way as the
                    // reasoning above; full text lives in the modal.
                    maxHeight: 240,
                    overflowY: "auto",
                  }}
                >
                  {item.content}
                </div>
                <FullTextTrigger
                  onClick={() =>
                    setFullText({
                      title: contentLabel,
                      text: item.content ?? "",
                    })
                  }
                />
              </div>
            )}
          </div>
        )}
      </div>
      <FullTextModal state={fullText} onClose={() => setFullText(null)} />
    </div>
  );
}

export function WorkerSubTimeline({ workers }: { workers: WorkerTimeline[] }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 4 }}>
      {workers.map((w) => (
        <WorkerNode key={w.workerId} worker={w} />
      ))}
    </div>
  );
}

const WORKER_STATUS_TONE: Record<WorkerStatus, string> = {
  running: INFO,
  success: SUCCESS,
  max_steps: WARNING,
  cancelled: DANGER,
};

function WorkerNode({ worker }: { worker: WorkerTimeline }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const tone = WORKER_STATUS_TONE[worker.status];
  const statusText = t(`playground.tl_worker_${worker.status}`);
  return (
    <div
      data-testid="worker-subtimeline"
      style={{
        border: "1px solid var(--ew-border-subtle)",
        borderLeft: `3px solid ${tone}`,
        borderRadius: 6,
        background: "var(--ew-surface-base)",
        fontSize: 12,
      }}
    >
      <div
        data-testid="worker-subtimeline-header"
        role="button"
        tabIndex={0}
        onClick={() => setExpanded((v) => !v)}
        onKeyDown={(e: KeyboardEvent): void => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setExpanded((v) => !v);
          }
        }}
        style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 8px", cursor: "pointer" }}
      >
        <span style={{ color: "var(--ew-text-secondary)" }}>{expanded ? "▾" : "▸"}</span>
        <span style={{ fontFamily: "var(--ew-font-mono)" }}>{worker.role ?? worker.agentRef}</span>
        <span style={{ color: tone }}>{statusText}</span>
        {worker.summary ? (
          <span data-testid="worker-summary" style={{ marginLeft: "auto", color: "var(--ew-text-secondary)" }}>
            {t("playground.tl_worker_summary", {
              steps: worker.summary.iterationUsed,
              calls: worker.summary.llmCallCount,
              duration: fmtDuration(worker.summary.wallClockMs),
            })}
          </span>
        ) : null}
      </div>
      {expanded ? (
        <div style={{ padding: "0 8px 6px 24px", display: "flex", flexDirection: "column", gap: 4 }}>
          {worker.taskExcerpt ? (
            <div style={{ color: "var(--ew-text-secondary)" }}>
              {t("playground.tl_worker_task")}: {worker.taskExcerpt}
            </div>
          ) : null}
          {worker.steps.map((s) => (
            <div key={s.wseq} data-testid="worker-step" style={{ fontFamily: "var(--ew-font-mono)" }}>
              <span style={{ color: "var(--ew-text-secondary)" }}>
                {s.node}
                {s.stepCount !== null ? ` #${s.stepCount}` : ""} · {fmtDuration(s.durationMs)}
              </span>
              {s.messages.map((m, i) => (
                <div key={i} style={{ paddingLeft: 12, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                  {m.contentExcerpt ?? ""}
                  {m.toolCalls?.map((c) => `⚙ ${c.name}(${c.argsExcerpt})`).join(" ") ?? ""}
                  {m.toolResultExcerpt ? `→ ${m.toolResultExcerpt}` : ""}
                </div>
              ))}
            </div>
          ))}
          {worker.children.length > 0 ? (
            <div style={{ paddingLeft: 8 }}>
              <div style={{ color: "var(--ew-text-secondary)" }}>{t("playground.tl_worker_children")}</div>
              <WorkerSubTimeline workers={worker.children} />
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function AuxNodeRow({ item }: { item: AuxNodeItem }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const warn = item.tone === "warn";
  const toggle = (): void => setExpanded((v) => !v);
  const onKeyDown = (e: KeyboardEvent): void => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      toggle();
    }
  };
  const dotColor =
    item.kind === "planner"
      ? INFO
      : item.kind === "reflect"
        ? warn
          ? WARNING
          : SUCCESS
        : item.kind === "memory_recall" || item.kind === "memory_writeback"
          ? SUCCESS
          : "var(--ew-border-strong)";
  const tailLabel = item.kind === "reflect" ? t("playground.tl_critique") : t("playground.tl_expand");

  return (
    <div style={{ position: "relative", marginBottom: 10 }}>
      <span
        aria-hidden
        style={{
          position: "absolute",
          left: -22,
          top: 11,
          width: 9,
          height: 9,
          borderRadius: "50%",
          background: "var(--ew-surface-base)",
          border: `2px solid ${dotColor}`,
        }}
      />
      <div
        data-testid="node-row"
        role="button"
        tabIndex={0}
        onClick={toggle}
        onKeyDown={onKeyDown}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          flexWrap: "wrap",
          padding: "7px 11px",
          borderRadius: 6,
          fontSize: 12.5,
          cursor: "pointer",
          border: `1px solid ${
            warn ? `color-mix(in srgb, ${WARNING} 40%, var(--ew-border-subtle))` : "var(--ew-border-subtle)"
          }`,
          background: warn ? `color-mix(in srgb, ${WARNING} 7%, transparent)` : "var(--ew-surface-base)",
        }}
      >
        <span
          style={{ fontFamily: "var(--ew-font-mono)", fontSize: 11, color: "var(--ew-text-tertiary)" }}
        >
          {item.kind}
        </span>
        <span>{item.summary}</span>
        <DurationBadge ms={item.durationMs} style={{ fontSize: 11, color: "var(--ew-text-tertiary)" }} />
        <span style={{ marginLeft: "auto", color: "var(--ew-text-tertiary)", fontSize: 11 }}>
          {expanded ? "▾" : "▸"} {tailLabel}
        </span>
      </div>
      {expanded && (
        <div
          style={{
            marginTop: 6,
            padding: "8px 11px",
            border: "1px solid var(--ew-border-subtle)",
            borderRadius: 6,
            background: "var(--ew-surface-raised)",
            fontSize: 12,
          }}
        >
          <AuxNodeDetail item={item} />
        </div>
      )}
    </div>
  );
}

interface MemoryDetailItem {
  id: string;
  kind: string;
  content: string;
  importance?: number;
  confidence?: number;
}

function record(v: unknown): Record<string, unknown> {
  return v !== null && typeof v === "object" ? (v as Record<string, unknown>) : {};
}

function asMemories(detail: Record<string, unknown>): MemoryDetailItem[] {
  const raw = detail.memories;
  if (!Array.isArray(raw)) return [];
  return raw.map((m, i) => {
    const o = record(m);
    return {
      id: typeof o.id === "string" ? o.id : String(i),
      kind: typeof o.kind === "string" ? o.kind : "",
      content: typeof o.content === "string" ? o.content : "",
      importance: typeof o.importance === "number" ? o.importance : undefined,
      confidence: typeof o.confidence === "number" ? o.confidence : undefined,
    };
  });
}

function planGoal(detail: Record<string, unknown>): string | null {
  const p = record(detail.plan);
  const goal = p.goal ?? p.objective;
  return typeof goal === "string" && goal.trim() !== "" ? goal : null;
}

function planSteps(detail: Record<string, unknown>): string[] {
  const p = record(detail.plan);
  const steps = Array.isArray(p.steps) ? p.steps : [];
  return steps.map((s) => {
    if (typeof s === "string") return s;
    const so = record(s);
    const text = so.description ?? so.text ?? so.title;
    return typeof text === "string" ? text : JSON.stringify(s);
  });
}

function reflectCritique(detail: Record<string, unknown>): string {
  return typeof detail.critique === "string" ? detail.critique : "";
}

function AuxNodeDetail({ item }: { item: AuxNodeItem }) {
  const { t } = useTranslation();
  if (item.kind === "memory_recall" || item.kind === "memory_writeback") {
    const memories = asMemories(item.detail);
    return (
      <>
        {memories.map((m, i) => (
          <div
            key={m.id}
            style={{
              padding: "4px 0",
              borderTop: i === 0 ? "none" : "1px solid var(--ew-border-subtle)",
              color: "var(--ew-text-secondary)",
            }}
          >
            <span
              style={{
                fontSize: 10.5,
                padding: "0 6px",
                borderRadius: 4,
                background: `color-mix(in srgb, ${SUCCESS} 16%, transparent)`,
                color: SUCCESS,
                marginRight: 6,
              }}
            >
              {m.kind}
            </span>
            {m.content}{" "}
            <span style={{ fontFamily: "var(--ew-font-mono)", fontSize: 11 }}>
              {m.importance !== undefined && `· ${t("playground.tl_importance", { v: m.importance.toFixed(2) })} `}
              {m.confidence !== undefined && `· ${t("playground.tl_confidence", { v: m.confidence.toFixed(2) })}`}
            </span>
          </div>
        ))}
      </>
    );
  }
  if (item.kind === "planner") {
    const goal = planGoal(item.detail);
    const steps = planSteps(item.detail);
    return (
      <>
        {goal && <div style={{ color: "var(--ew-text-primary)" }}>{t("playground.tl_goal", { text: goal })}</div>}
        {steps.map((s, i) => (
          <div key={i} style={{ display: "flex", gap: 7, padding: "3px 0", color: "var(--ew-text-secondary)" }}>
            <span>{i + 1}.</span> <span>{s}</span>
          </div>
        ))}
      </>
    );
  }
  if (item.kind === "reflect") {
    return <div style={{ color: "var(--ew-text-secondary)" }}>{reflectCritique(item.detail)}</div>;
  }
  return null;
}

const MARKER_TONE_COLOR: Record<MarkerItem["tone"], string> = {
  warn: WARNING,
  bad: DANGER,
  good: SUCCESS,
  pause: PURPLE,
};

function MarkerRow({ item }: { item: MarkerItem }) {
  const color = MARKER_TONE_COLOR[item.tone];
  const isCircle = item.tone === "good";
  return (
    <div
      style={{ position: "relative", marginBottom: 10 }}
      // Scroll target for the run-status banner's "jump" on a marker-only
      // failure (an `error` SSE event with no failing tool step).
      data-error={item.kind === "error" ? "true" : undefined}
    >
      <span
        aria-hidden
        style={{
          position: "absolute",
          left: isCircle ? -21 : -20,
          top: 7,
          width: isCircle ? 9 : 7,
          height: isCircle ? 9 : 7,
          borderRadius: isCircle ? "50%" : 0,
          transform: isCircle ? undefined : "rotate(45deg)",
          background: color,
        }}
      />
      <div
        data-testid="marker"
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 7,
          fontSize: 12,
          color: "var(--ew-text-secondary)",
          padding: "3px 0",
          flexWrap: "wrap",
        }}
      >
        <span
          style={{
            fontFamily: "var(--ew-font-mono)",
            fontSize: 11,
            padding: "1px 6px",
            borderRadius: 4,
            background: `color-mix(in srgb, ${color} 16%, transparent)`,
            color,
          }}
        >
          {item.kind}
        </span>
        {item.text}
      </div>
    </div>
  );
}

function legendDot(color: string, opts: { diamond?: boolean; filled?: boolean } = {}) {
  const { diamond = false, filled = false } = opts;
  return {
    width: diamond ? 8 : 10,
    height: diamond ? 8 : 10,
    borderRadius: diamond ? 0 : "50%",
    border: diamond ? undefined : `2px solid ${color}`,
    background: diamond
      ? color
      : filled
        ? `color-mix(in srgb, ${color} 30%, var(--ew-surface-base))`
        : undefined,
    transform: diamond ? "rotate(45deg)" : undefined,
  };
}

function Legend() {
  const { t } = useTranslation();
  const entries: { color: string; diamond?: boolean; filled?: boolean; label: string }[] = [
    { color: SUCCESS, label: t("playground.tl_legend_agent") },
    { color: SUCCESS, filled: true, label: t("playground.tl_legend_mem") },
    { color: INFO, label: t("playground.tl_legend_plan") },
    { color: WARNING, label: t("playground.tl_legend_reflect") },
    { color: WARNING, diamond: true, label: t("playground.tl_legend_marker") },
    { color: PURPLE, diamond: true, label: t("playground.tl_legend_approval") },
  ];
  return (
    <div
      data-testid="step-timeline-legend"
      style={{
        display: "flex",
        flexWrap: "wrap",
        gap: "6px 14px",
        marginTop: 14,
        paddingTop: 12,
        borderTop: "1px dashed var(--ew-border-subtle)",
      }}
    >
      {entries.map((it, i) => (
        <span
          key={i}
          style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11.5, color: "var(--ew-text-secondary)" }}
        >
          <span style={legendDot(it.color, { diamond: it.diamond, filled: it.filled })} />
          {it.label}
        </span>
      ))}
    </div>
  );
}
