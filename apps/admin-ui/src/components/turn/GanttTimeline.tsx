/**
 * GanttTimeline — Gantt-style execution-trace renderer (Task 2 of the
 * debug-console execution-timeline upgrade). Consumes Task 1's
 * `buildGanttRows` output (`api/gantt_timeline.ts`) and renders it as
 * positioned bars on a shared time axis instead of a vertical step list, so
 * concurrent tool calls / worker delegations overlap visually instead of
 * reading as sequential.
 *
 * Visual/behavioural baseline is the confirmed interactive prototype
 * (docs/superpowers/specs/2026-07-31-gantt-execution-timeline-design.md,
 * gantt-mockup.html): two-density `variant` prop (embedded/expanded), a
 * label-column antd Tooltip carrying the untruncated name + model, hover-
 * only duration labels in the embedded density, click-to-expand row detail
 * (one row at a time — `renderDetail` is injected by the caller, so this
 * component stays agnostic of what a row's detail actually renders), marker
 * ticks for instantaneous events (compaction/retry/error/approval/guard/
 * end), and an entrance grow animation that respects
 * `prefers-reduced-motion` — handled in the co-located stylesheet, since
 * pseudo-classes / `@keyframes` / `@media` aren't expressible via inline
 * React style objects (no scoped-CSS precedent existed elsewhere in this
 * codebase; every other component styles purely inline, but hover-reveal +
 * a disable-on-reduced-motion animation both need real CSS).
 *
 * Colors are semantic `--ew-*` tokens end to end (see GanttTimeline.css),
 * never a hardcoded hex here: agent=info, aux=accent-violet, tool=success,
 * worker=warning, final=the raw success-500 scale step — a more saturated
 * flat green than the `success` semantic token already used for ordinary
 * `tool` bars, so the two stay visually distinct without inventing a new
 * token.
 */
import { type ReactNode, useState } from "react";
import { Tooltip } from "antd";

import type { GanttMarker, GanttModel, GanttRow } from "../../api/gantt_timeline";
import { fmtDuration } from "../../pages/agent_detail/playground/duration_format";
import "./GanttTimeline.css";

export interface GanttTimelineProps {
  model: GanttModel;
  variant: "embedded" | "expanded";
  /** Growing-bar tick switch for in-progress (`durationMs === null`) rows —
   *  `true` renders them as still running, `false` as interrupted (the run
   *  ended before this unit produced a result). Task 3 drives this from the
   *  turn's run status. */
  running?: boolean;
  /** Row detail renderer — the caller owns what a row expands into (reuses
   *  the existing step/tool/worker cards); this component only toggles
   *  visibility, one row at a time. */
  renderDetail: (row: GanttRow) => ReactNode;
}

const KIND_CLASS: Record<GanttRow["kind"], string> = {
  agent: "ew-gantt-bar--agent",
  aux: "ew-gantt-bar--aux",
  tool: "ew-gantt-bar--tool",
  worker: "ew-gantt-bar--worker",
  final: "ew-gantt-bar--final",
};

const MARKER_COLOR: Record<GanttMarker["kind"], string> = {
  compaction: "var(--ew-text-warning, #f59e0b)",
  retry: "var(--ew-text-warning, #f59e0b)",
  error: "var(--ew-text-danger, #ef4444)",
  approval: "var(--ew-accent-violet, #a855f7)",
  guard: "var(--ew-text-warning, #f59e0b)",
  end: "var(--ew-text-success, #34d399)",
};

const MIN_BAR_WIDTH_PCT = 0.35;

/** Adaptive gridline step, ms: <10s axis → 1s ticks, <60s → 10s, else 30s. */
function axisStepMs(totalMs: number): number {
  if (totalMs < 10_000) return 1_000;
  if (totalMs < 60_000) return 10_000;
  return 30_000;
}

function ticksFor(totalMs: number): number[] {
  if (totalMs <= 0) return [];
  const step = axisStepMs(totalMs);
  const out: number[] = [];
  for (let t = step; t <= totalMs; t += step) out.push(t);
  return out;
}

function pct(valueMs: number, totalMs: number): number {
  return totalMs > 0 ? (valueMs / totalMs) * 100 : 0;
}

/** Bar geometry as CSS percentage strings. A `durationMs === null` row (still
 *  in progress, or truncated because the run ended before it finished) has
 *  no known end — it's drawn stretching to the right edge of the axis
 *  rather than at the zero-width point Task 1 places it at. */
function barGeometry(row: GanttRow, totalMs: number): { left: string; width: string } {
  const safeTotal = totalMs > 0 ? totalMs : 1;
  const leftPct = pct(row.startMs, safeTotal);
  const widthPct =
    row.durationMs === null
      ? Math.max(pct(safeTotal - row.startMs, safeTotal), MIN_BAR_WIDTH_PCT)
      : Math.max(pct(row.durationMs, safeTotal), MIN_BAR_WIDTH_PCT);
  return { left: `${leftPct}%`, width: `${widthPct}%` };
}

export function GanttTimeline({ model, variant, running = false, renderDetail }: GanttTimelineProps) {
  const [openKey, setOpenKey] = useState<string | null>(null);
  const embedded = variant === "embedded";
  const safeTotal = model.totalMs > 0 ? model.totalMs : 1;
  const ticks = ticksFor(model.totalMs);

  const toggle = (key: string): void => setOpenKey((cur) => (cur === key ? null : key));

  return (
    <div className="ew-gantt-scroll" data-testid="gantt-timeline" data-variant={variant}>
      <div className="ew-gantt-inner">
        <div className="ew-gantt-axis" aria-hidden>
          {ticks.map((tick) => (
            <span key={tick} className="ew-gantt-tick" style={{ left: `${pct(tick, safeTotal)}%` }}>
              {tick / 1000}s
            </span>
          ))}
        </div>
        <div className="ew-gantt-body">
          {model.rows.map((row) => {
            const open = openKey === row.key;
            const { left, width } = barGeometry(row, model.totalMs);
            const barClasses = [
              "ew-gantt-bar",
              KIND_CLASS[row.kind],
              row.durationMs === null
                ? running
                  ? "ew-gantt-bar--running"
                  : "ew-gantt-bar--interrupted"
                : null,
            ]
              .filter((c): c is string => c !== null)
              .join(" ");
            const tooltipTitle = row.model ? `${row.label} · ${row.model}` : row.label;

            return (
              <div key={row.key}>
                <div
                  className={`ew-gantt-row${open ? " ew-gantt-row--active" : ""}`}
                  data-testid={`gantt-row-${row.key}`}
                  role="button"
                  tabIndex={0}
                  onClick={() => toggle(row.key)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      toggle(row.key);
                    }
                  }}
                >
                  <Tooltip title={tooltipTitle}>
                    <div
                      className="ew-gantt-label"
                      data-testid={`gantt-label-${row.key}`}
                      style={{ paddingLeft: row.depth * 18 + 10 }}
                    >
                      {row.depth > 0 && (
                        <span className="ew-gantt-twig" aria-hidden>
                          └
                        </span>
                      )}
                      <span className="ew-gantt-name">{row.label}</span>
                      {!embedded && row.model && <span className="ew-gantt-model">{row.model}</span>}
                    </div>
                  </Tooltip>
                  <div className="ew-gantt-track">
                    <div className={barClasses} data-testid={`gantt-bar-${row.key}`} style={{ left, width }}>
                      {row.durationMs !== null && (
                        <span
                          className={`ew-gantt-dur${embedded ? " gantt-dur-hover" : ""}`}
                          data-testid={`gantt-dur-${row.key}`}
                        >
                          {fmtDuration(row.durationMs)}
                        </span>
                      )}
                    </div>
                  </div>
                </div>
                {open && (
                  <div className="ew-gantt-detail" data-testid={`gantt-detail-${row.key}`}>
                    {renderDetail(row)}
                  </div>
                )}
              </div>
            );
          })}
          <div className="ew-gantt-markers" aria-hidden={model.markers.length === 0}>
            {model.markers.map((marker, i) => (
              <span
                key={`${marker.kind}-${marker.atMs}-${i}`}
                className={`ew-gantt-marker ew-gantt-marker--${marker.kind}`}
                data-testid="gantt-marker"
                title={marker.text}
                style={{ left: `${pct(marker.atMs, safeTotal)}%`, color: MARKER_COLOR[marker.kind] }}
              />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
