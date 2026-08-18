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
 *
 * PR-A.1 Task 6(spec §八.8):行改成七列表格(`# / 类型 / 摘要 / 入 / 出 /
 * 思考 / 耗时`,token 三列只有 think 行填),表头 sticky,泳道拖出来的
 * `range` 只留区间内的行并在表头上方挂一枚可清除的筛选芯片,hover 与泳道
 * 双向联动。见
 * .superpowers/sdd/2026-08-18-debug-console-pr-a1-feedback/task-6-brief.md。
 */
import type { JSX, KeyboardEvent } from "react";
import { useEffect, useMemo, useRef } from "react";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";

import type { TrajectoryRow } from "../../api/trajectory_rows";
import { fmtDuration } from "../../pages/agent_detail/playground/duration_format";
import { formatCompact } from "../../utils/runFormat";
import { firstSentence } from "./text_summary";
import "./trajectory_rows.css";

export interface TrajectoryRowsProps {
  /** 已含 live 合成行(父级拼)。 */
  rows: readonly TrajectoryRow[];
  selectedRowId: string | null;
  hoveredRowId: string | null;
  onHoverRow: (rowId: string | null) => void;
  onSelectRow: (rowId: string) => void;
  /** 该轮进行中 → 行列表自动滚到底(用户没上滚时)。 */
  running: boolean;
  /** 泳道拖出来的行序号 **1-based 闭区间**;null = 不筛选。 */
  range: { from: number; to: number } | null;
  onClearRange: () => void;
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
    case "plan": {
      if (row.source !== "update_plan") return t("console.row_plan_create", { n: row.stepsTotal });
      const base = t("console.row_plan_update", { n: row.stepsTotal });
      return row.reason ? `${base} · ${firstSentence(row.reason)}` : base;
    }
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

/** 七列(顺序即渲染顺序);列宽在 trajectory_rows.css 的 grid-template-columns。 */
const COLUMN_KEYS = ["idx", "kind", "summary", "in", "out", "think", "duration"] as const;

/** token 列:没报(`undefined`)和报了 0 都留空 —— 三列填一片 `0` 只是噪声,
 *  「这一步没花 token」本来也不是读者要在行表里找的信息。 */
function tokenCell(n: number | undefined): string {
  return n === undefined || n === 0 ? "" : formatCompact(n);
}

export function TrajectoryRows(props: TrajectoryRowsProps): JSX.Element {
  const { rows, selectedRowId, hoveredRowId, onHoverRow, onSelectRow, running, range, onClearRange } = props;
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

  // 行的 `#` 与 `data-index` 永远是**全表**里的 1-based 序号(泳道块的
  // `rowIndex` 同源),筛选只决定哪几行渲染出来。
  const visible = useMemo(
    () =>
      rows
        .map((row, i) => ({ row, index: i + 1 }))
        .filter(({ index }) => range === null || (index >= range.from && index <= range.to)),
    [rows, range],
  );

  const handleKeyDown = (e: KeyboardEvent<HTMLUListElement>): void => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    // 上下键只在**看得见的**行之间走 —— 筛选中跳到被隐藏的行等于卡死。
    const idx = visible.findIndex(({ row }) => row.id === selectedRowId);
    const delta = e.key === "ArrowDown" ? 1 : -1;
    const nextIdx = idx === -1 ? 0 : Math.min(Math.max(idx + delta, 0), visible.length - 1);
    const next = visible[nextIdx];
    if (next) onSelectRow(next.row.id);
  };

  return (
    <div className="ew-traj-rows-wrap">
      {range !== null && (
        <div className="ew-traj-rows__filter" data-testid="console-traj-filter">
          <span>{t("console.traj_filter", { a: range.from, b: range.to, n: visible.length })}</span>
          <button
            type="button"
            className="ew-traj-rows__filter-clear"
            aria-label={t("console.traj_filter_clear")}
            data-testid="console-traj-filter-clear"
            onClick={onClearRange}
          >
            ✕
          </button>
        </div>
      )}
      <ul
        ref={listRef}
        data-testid="console-traj-rows"
        role="listbox"
        tabIndex={0}
        className="ew-traj-rows"
        onKeyDown={handleKeyDown}
      >
        <li className="ew-traj-rows__head" role="presentation" data-testid="console-traj-head">
          {COLUMN_KEYS.map((key) => (
            <span key={key}>{t(`console.traj_col_${key}`)}</span>
          ))}
        </li>
        {visible.map(({ row, index }) => {
          const selected = row.id === selectedRowId;
          const think = row.kind === "think" ? row : null;
          return (
            <li key={row.id} className="ew-traj-rows__item">
              <button
                type="button"
                ref={selected ? selectedRef : undefined}
                data-testid="console-traj-row"
                data-kind={row.kind}
                data-row-id={row.id}
                data-status={row.status}
                data-index={index}
                data-hovered={row.id === hoveredRowId ? "true" : undefined}
                role="option"
                aria-selected={selected}
                className={`ew-traj-row${selected ? " ew-traj-row--selected" : ""}`}
                onClick={() => onSelectRow(row.id)}
                onMouseEnter={() => onHoverRow(row.id)}
                onMouseLeave={() => onHoverRow(null)}
              >
                <span className="ew-traj-row__idx">{index}</span>
                <span className="ew-traj-row__kind">
                  {t(`console.traj_kind_${row.kind}`)}
                  {row.status === "running" && <span className="ew-traj-row__pulse" aria-hidden="true" />}
                </span>
                <span className="ew-traj-row__summary">{rowSummary(row, t)}</span>
                <span className="ew-traj-row__tok">{think === null ? "" : tokenCell(think.inputTokens)}</span>
                <span className="ew-traj-row__tok">{think === null ? "" : tokenCell(think.outputTokens)}</span>
                <span className="ew-traj-row__tok">{think === null ? "" : tokenCell(think.reasoningTokens)}</span>
                <span className="ew-traj-row__duration">
                  {row.durationMs === null ? "" : fmtDuration(row.durationMs)}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
