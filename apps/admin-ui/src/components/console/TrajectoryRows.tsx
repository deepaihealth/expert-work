/**
 * TrajectoryRows — the debug console's flat right-rail trajectory row list
 * (调试台重设计 PR-A Task 16). Renders whatever `rows` the parent hands it
 * (Task 4's `trajectoryRowsOf`, live rows already spliced in) as a
 * `role="listbox"` of kind-labelled, one-line-summarised rows: click or
 * arrow-key to select, auto-scroll to the bottom while a turn is running
 * (unless the reader has scrolled up to look at history).
 *
 * `rowSummary` is exported standalone (no React) so Task 17's RowDetail can
 * reuse the exact same one-line text for its header.
 * See .superpowers/sdd/2026-08-18-debug-console-pr-a-console/task-16-brief.md.
 */
import type { JSX, KeyboardEvent } from "react";
import { useEffect, useRef } from "react";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";

import type { TrajectoryRow } from "../../api/trajectory_rows";
import { fmtDuration } from "../../pages/agent_detail/playground/duration_format";
import "./trajectory_rows.css";

export interface TrajectoryRowsProps {
  /** 已含 live 合成行(父级拼)。 */
  rows: readonly TrajectoryRow[];
  selectedRowId: string | null;
  onSelectRow: (rowId: string) => void;
  /** 该轮进行中 → 行列表自动滚到底(用户没上滚时)。 */
  running: boolean;
}

/** Text up to (not including) the first newline. */
function firstLine(text: string): string {
  const idx = text.indexOf("\n");
  return idx === -1 ? text : text.slice(0, idx);
}

/** One-line summary for a trajectory row — shared with Task 17's RowDetail
 *  header (`t` is react-i18next's `TFunction`, so this stays pure/testable
 *  outside of React). */
export function rowSummary(row: TrajectoryRow, t: TFunction): string {
  switch (row.kind) {
    case "user":
    case "assistant":
      return firstLine(row.text);
    case "think":
      return row.text === "" ? t("console.traj_llm_call", { model: row.model ?? "" }) : firstLine(row.text);
    case "tool": {
      const args = JSON.stringify(row.entry.args ?? {}).slice(0, 80);
      const result =
        row.status === "running"
          ? t("console.row_tool_pending")
          : row.status === "error"
            ? `${t("console.row_tool_error")}: ${firstLine(row.entry.resultPreview ?? "")}`
            : firstLine(row.entry.resultPreview ?? "");
      return `${row.entry.toolName} · ${args} → ${result}`;
    }
    case "plan":
      return row.source === "update_plan"
        ? t("console.row_plan_update", { n: row.stepsTotal })
        : t("console.row_plan_create", { n: row.stepsTotal });
    case "memory":
      return row.direction === "recall"
        ? t("console.row_memory_recall", { n: row.count })
        : t("console.row_memory_writeback", { n: row.count });
    case "reflect":
      return row.verdict === "pass" ? t("console.row_reflect_pass") : t("console.row_reflect_revise");
    case "subagent":
      return t("console.row_subagent", { name: row.worker.label });
    default:
      // MarkerRow (compaction / retry / error / approval / guard / gap).
      return row.text;
  }
}

/** How close to the bottom (px) still counts as "hasn't scrolled up". */
const AUTO_SCROLL_SLACK_PX = 80;

export function TrajectoryRows({ rows, selectedRowId, onSelectRow, running }: TrajectoryRowsProps): JSX.Element {
  const { t } = useTranslation();
  const listRef = useRef<HTMLUListElement>(null);
  const selectedRef = useRef<HTMLButtonElement>(null);
  const prevRowCountRef = useRef(rows.length);

  // Selected row changed → bring it into view (jsdom has no
  // `scrollIntoView`, so this is an optional call).
  useEffect(() => {
    selectedRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [selectedRowId]);

  // A running turn that just grew its row list auto-scrolls to the bottom —
  // unless the reader scrolled away from it to look at earlier history.
  useEffect(() => {
    const grew = rows.length > prevRowCountRef.current;
    prevRowCountRef.current = rows.length;
    if (!running || !grew) return;
    const el = listRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight <= AUTO_SCROLL_SLACK_PX;
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }, [rows.length, running]);

  const handleKeyDown = (e: KeyboardEvent<HTMLUListElement>): void => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const idx = rows.findIndex((r) => r.id === selectedRowId);
    const delta = e.key === "ArrowDown" ? 1 : -1;
    const nextIdx = idx === -1 ? 0 : Math.min(Math.max(idx + delta, 0), rows.length - 1);
    const next = rows[nextIdx];
    if (next) onSelectRow(next.id);
  };

  return (
    <ul
      ref={listRef}
      data-testid="console-traj-rows"
      role="listbox"
      tabIndex={0}
      className="ew-traj-rows"
      onKeyDown={handleKeyDown}
    >
      {rows.map((row) => {
        const selected = row.id === selectedRowId;
        return (
          <li key={row.id} className="ew-traj-rows__item">
            <button
              type="button"
              ref={selected ? selectedRef : undefined}
              data-testid="console-traj-row"
              data-kind={row.kind}
              data-row-id={row.id}
              data-status={row.status}
              role="option"
              aria-selected={selected}
              className={`ew-traj-row${selected ? " ew-traj-row--selected" : ""}`}
              onClick={() => onSelectRow(row.id)}
            >
              <span className="ew-traj-row__kind">
                {t(`console.traj_kind_${row.kind}`)}
                {row.status === "running" && <span className="ew-traj-row__pulse" aria-hidden="true" />}
              </span>
              <span className="ew-traj-row__summary">{rowSummary(row, t)}</span>
              {row.durationMs !== null && <span className="ew-traj-row__duration">{fmtDuration(row.durationMs)}</span>}
            </button>
          </li>
        );
      })}
    </ul>
  );
}
