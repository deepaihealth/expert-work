/**
 * LaneStrip — the debug console's compact three-lane time strip (input /
 * model / tools), built off `lane_strip_model.ts`'s `laneModelOf`. Each
 * `GanttRow` becomes one block on its lane, positioned by percentage of the
 * turn's total elapsed ms (same `pct` / min-bar-width algorithm as
 * `components/turn/GanttTimeline.tsx:98-113`, copied — not imported, since
 * that component owns a different (per-row) layout). Clicking a block that
 * resolves to a trajectory row selects it; a block with no resolvable row
 * (e.g. the caller's `rows` hasn't caught up with `events` yet) renders as a
 * non-interactive span instead of a dead button.
 *
 * See .superpowers/sdd/2026-08-18-debug-console-pr-a-console/task-15-brief.md.
 */
import { useEffect, useMemo, useState, type JSX } from "react";
import { useTranslation } from "react-i18next";

import type { SseEvent } from "../../api/sessions";
import type { TrajectoryRow } from "../../api/trajectory_rows";
import { laneModelOf, type Lane, type LaneBlock } from "./lane_strip_model";
import "./lane_strip.css";

export interface LaneStripProps {
  events: readonly SseEvent[];
  rows: readonly TrajectoryRow[];
  running: boolean;
  selectedRowId: string | null;
  onSelectRow: (rowId: string) => void;
}

const LANES: readonly Lane[] = ["input", "model", "tools"];
const LANE_LABEL_KEY: Record<Lane, string> = {
  input: "console.lane_input",
  model: "console.lane_model",
  tools: "console.lane_tools",
};

/** Copied from `components/turn/GanttTimeline.tsx:71`. */
const MIN_BAR_WIDTH_PCT = 0.35;

/** Copied from `components/turn/GanttTimeline.tsx:98-100`. */
function pct(valueMs: number, totalMs: number): number {
  return totalMs > 0 ? (valueMs / totalMs) * 100 : 0;
}

export function LaneStrip({
  events,
  rows,
  running,
  selectedRowId,
  onSelectRow,
}: LaneStripProps): JSX.Element | null {
  const { t } = useTranslation();
  // Running-growth tick — mirrors TurnCard.tsx:404-410: only ticks while the
  // turn is running, cleaned up on unmount/settle so no stray interval keeps
  // firing once the caller stops passing `running`.
  const [nowTick, setNowTick] = useState(0);
  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(() => setNowTick((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, [running]);

  const model = useMemo(
    () => laneModelOf(events, rows, { running, nowMs: Date.now() }),
    [events, rows, running, nowTick],
  );

  if (model.blocks.length === 0) return null;

  const safeTotal = model.totalMs > 0 ? model.totalMs : 1;
  const blocksByLane: Record<Lane, LaneBlock[]> = { input: [], model: [], tools: [] };
  for (const block of model.blocks) blocksByLane[block.lane].push(block);

  return (
    <div className="ew-lane-strip" data-testid="console-lane-strip">
      <div className="ew-lane-strip__body">
        {LANES.map((lane) => (
          <div key={lane} className="ew-lane-strip__row">
            <span className="ew-lane-strip__head">{t(LANE_LABEL_KEY[lane])}</span>
            <div className="ew-lane-strip__track">
              {blocksByLane[lane].map((block) => {
                const left = pct(block.startMs, safeTotal);
                const isLive = block.durationMs === null && running;
                const width =
                  block.durationMs === null
                    ? isLive
                      ? Math.max(pct(safeTotal - block.startMs, safeTotal), MIN_BAR_WIDTH_PCT)
                      : MIN_BAR_WIDTH_PCT
                    : Math.max(pct(block.durationMs, safeTotal), MIN_BAR_WIDTH_PCT);
                const classes = [
                  "ew-lane__block",
                  isLive ? "ew-lane__block--live" : block.durationMs === null ? "ew-lane__block--interrupted" : null,
                  block.hasError ? "ew-lane__block--error" : null,
                ]
                  .filter((c): c is string => c !== null)
                  .join(" ");
                const style = { left: `${left}%`, width: `${width}%` };

                if (block.rowId === null) {
                  return (
                    <span
                      key={block.key}
                      className={classes}
                      data-testid="console-lane-block"
                      data-lane={lane}
                      data-error={block.hasError ? "true" : undefined}
                      title={block.label}
                      style={style}
                    />
                  );
                }

                const rowId = block.rowId;
                return (
                  <button
                    key={block.key}
                    type="button"
                    className={classes}
                    data-testid="console-lane-block"
                    data-lane={lane}
                    data-row-id={rowId}
                    data-error={block.hasError ? "true" : undefined}
                    aria-pressed={rowId === selectedRowId}
                    title={block.label}
                    style={style}
                    onClick={() => onSelectRow(rowId)}
                  />
                );
              })}
            </div>
          </div>
        ))}
        <div className="ew-lane-strip__markers" aria-hidden={model.markers.length === 0}>
          {model.markers.map((marker) => (
            <span
              key={marker.key}
              className="ew-lane__marker"
              data-testid="console-lane-marker"
              data-kind={marker.kind}
              title={marker.text}
              style={{ left: `${Math.min(pct(marker.atMs, safeTotal), 100)}%` }}
            />
          ))}
        </div>
      </div>
      {model.degraded && <div className="ew-lane-strip__degraded">{t("playground.gantt_degraded")}</div>}
    </div>
  );
}
