/**
 * ProcessStrip — one turn's 过程条 (spec §八.3): the compact step rows
 * (think / tool / plan / memory / reflect / marker) fold into a single strip
 * instead of stacking under the user bubble. **Running** ⇒ automatically
 * open, showing only the last 3 rows with the earlier ones behind a
 * 「还有 N 步…」 button; **settled** ⇒ collapsed to one headline
 * (``process_summary.ts``) with the turn's total step duration on the right.
 * A click on the head pins the state manually — from then on it no longer
 * follows ``running``, and it is remembered per turn (this component is
 * mounted once per turn by ``TurnBlock``).
 *
 * See .superpowers/sdd/2026-08-18-debug-console-pr-a1-feedback/task-3-brief.md.
 */
import { useMemo, useState, type JSX } from "react";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { CompactRow as CompactRowT } from "../../api/trajectory_rows";
import type { FireNowResult } from "../../api/triggers";
import { fmtDuration } from "../../pages/agent_detail/playground/duration_format";
import { CompactRow } from "./CompactRow";
import { processHeadline, summarizeProcess } from "./process_summary";
import "./process_strip.css";

export interface ProcessStripProps {
  rows: readonly { row: CompactRowT; liveText?: string }[];
  running: boolean;
  /** 已展开详情的行 id 集合 + 切换(TurnBlock 持有,行详情跟着轮走)。 */
  expandedRowIds: ReadonlySet<string>;
  onToggleRow: (id: string) => void;
  onInspectRow: (rowId: string) => void;
  onFireResult?: (r: FireNowResult) => void;
  /** PR-B Task 1 — 对话记录只读链路:透传给 CompactRow → ToolCallCard,
   *  ``true`` 时「立即触发」整卡不渲染。Default false。 */
  readOnly?: boolean;
}

/** 运行中自动展开时只留最近 3 步(spec §八.3),更早的折进「还有 N 步…」。 */
const AUTO_VISIBLE_ROWS = 3;

export function ProcessStrip({
  rows,
  running,
  expandedRowIds,
  onToggleRow,
  onInspectRow,
  onFireResult,
  readOnly = false,
}: ProcessStripProps): JSX.Element | null {
  const { t } = useTranslation();
  /** `null` = 跟随 `running` 自动;点过头部之后固定成用户选的那个。 */
  const [open, setOpen] = useState<boolean | null>(null);
  const summary = useMemo(
    () => summarizeProcess(rows.map((r) => r.row)),
    [rows],
  );

  if (rows.length === 0) return null;

  const isOpen = open ?? running;
  // 只有「自动展开」才截尾;手动展开(含点了「还有 N 步…」)一律全量。
  const tailOnly = open === null && running && rows.length > AUTO_VISIBLE_ROWS;
  const visible = isOpen ? (tailOnly ? rows.slice(-AUTO_VISIBLE_ROWS) : rows) : [];
  // 失败尾段由本组件单独渲染成红字,所以摘要按 `failed: 0` 取。
  const headline = processHeadline({ ...summary, failed: 0 }, t);

  return (
    <div className="ew-process">
      <button
        type="button"
        className="ew-process__head"
        data-testid="console-process-head"
        aria-expanded={isOpen}
        onClick={() => setOpen(!isOpen)}
      >
        <span className="ew-process__caret" aria-hidden>
          {isOpen ? "▾" : "▸"}
        </span>
        <span className="ew-process__headline">
          {headline}
          {summary.failed > 0 && (
            <>
              {" · "}
              <span className="ew-process__failed">
                {t("console.process_failed", { n: summary.failed })}
              </span>
            </>
          )}
        </span>
        {summary.durationMs !== null && (
          <span className="ew-process__duration">{fmtDuration(summary.durationMs)}</span>
        )}
        {running && (
          <Loader2
            size={12}
            className="ew-process__spin"
            data-testid="console-process-spinner"
            aria-hidden
          />
        )}
      </button>

      {isOpen && (
        <div className="ew-process__steps" data-testid="console-process-steps">
          {tailOnly && (
            <button
              type="button"
              className="ew-process__more"
              data-testid="console-process-more"
              onClick={() => setOpen(true)}
            >
              {t("console.process_more", { n: rows.length - AUTO_VISIBLE_ROWS })}
            </button>
          )}
          {visible.map(({ row, liveText }) => (
            <CompactRow
              key={row.id}
              row={row}
              expanded={expandedRowIds.has(row.id)}
              onToggle={() => onToggleRow(row.id)}
              liveText={liveText}
              onInspect={() => onInspectRow(row.id)}
              onFireResult={onFireResult}
              readOnly={readOnly}
            />
          ))}
        </div>
      )}
    </div>
  );
}
