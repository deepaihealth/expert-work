/**
 * TurnBlock — one turn of the debug console's middle-column transcript: the
 * user bubble, its compact step-row list (settled rows from
 * ``compactRowsOf`` plus, for the currently-streaming live turn, the
 * still-unsettled synthetic rows from ``live_rows.ts``'s
 * ``liveSyntheticRows``) folded into the ``ProcessStrip`` 过程条 (spec
 * §八.3), the approval gate, the answer bubble, and the
 * turn's status/metric/action footer. Composes Task 10's leaves
 * (``UserBubble`` / ``CompactRow`` / ``AnswerBubble`` / ``TurnFooter``) plus
 * ``ApprovalGate`` (imported from ``components/turn/TurnCard.tsx`` per the
 * controller ruling — that module is retired in the next PR, this one just
 * borrows the still-live export).
 *
 * See .superpowers/sdd/2026-08-18-debug-console-pr-a-console/task-11-brief.md.
 */
import { useCallback, useMemo, useState, type JSX } from "react";

import type { ApprovalItem } from "../../api/approvals";
import type { RateCardRecord } from "../../api/rate_card";
import type { FireNowResult } from "../../api/triggers";
import { compactRowsOf } from "../../api/trajectory_rows";
import { summarizeTurn } from "../../api/turn_summary";
import type { LiveStep } from "../../pages/agent_detail/playground/useTokenStream";
import { ReadonlyTooltip } from "../ReadonlyTooltip";
import { ApprovalGate } from "../turn/TurnCard";
import type { Turn } from "../turn/types";
import { AnswerBubble } from "./AnswerBubble";
import { liveSyntheticRows, settledStepsOf } from "./live_rows";
import { ProcessStrip } from "./ProcessStrip";
import { TurnFooter } from "./TurnFooter";
import type { ConsoleTurn } from "./types";
import { UserBubble } from "./UserBubble";

export interface TurnBlockProps {
  turn: ConsoleTurn;
  threadId: string | null;
  selected: boolean;
  onSelect: (key: string) => void;
  /** 紧凑行「轨迹」→ 父级切到「轨迹」视图并选中该行对应的账本记录
   *  (PR-A.2 Task 11 接 `TrajectoryView.focusRequest`)。 */
  onInspectRow: (turnKey: string, rowId: string) => void;
  /** 仅当前流式 live 轮传;其它 undefined。 */
  liveByStep?: ReadonlyMap<number, LiveStep>;
  rate: RateCardRecord | null;
  isSystemAdmin: boolean;
  readOnly: boolean;
  isTenantSwitched: boolean;
  onDecide: (
    turnId: string,
    approval: ApprovalItem,
    decision: "approve" | "reject",
  ) => void;
  deciding: boolean;
  onExport: (turn: Turn) => void;
  exporting: boolean;
  onRetry?: (turn: Turn) => void;
  onDownloadArtifact: (name: string) => Promise<void>;
  onFireResult?: (r: FireNowResult) => void;
  /** 历史轮懒加载 ref(``useHistoryTurns.registerRow(runId, threadId)`` 的
   *  返回);live 轮不传。 */
  rowRef?: (el: HTMLElement | null) => void;
}

/** #4 cost — same formula as ``TurnCard.tsx:643-653``: non-cached input +
 *  cache_read + output, each at its per-mtok rate (micro-元 per 1M tokens).
 *  null when no usage or no rate for the model. */
function costCnyOf(
  summary: ReturnType<typeof summarizeTurn>,
  rate: RateCardRecord | null,
): number | null {
  if (!summary.usage || !rate) return null;
  const usage = summary.usage;
  return (
    (Math.max(0, usage.inputTokens - usage.cacheReadTokens) *
      rate.input_per_mtok_micros +
      usage.cacheReadTokens * rate.cache_read_per_mtok_micros +
      usage.outputTokens * rate.output_per_mtok_micros) /
    1e12
  );
}

/** The highest not-yet-settled step's buffered answer content — the
 *  typewriter text ``AnswerBubble`` shows while that step streams. */
function liveAnswerTextOf(
  events: ConsoleTurn["turn"]["events"],
  liveByStep: ReadonlyMap<number, LiveStep> | undefined,
): string | undefined {
  if (!liveByStep) return undefined;
  const settled = settledStepsOf(events);
  let highest: number | null = null;
  for (const step of liveByStep.keys()) {
    if (settled.has(step)) continue;
    if (highest === null || step > highest) highest = step;
  }
  return highest === null ? undefined : liveByStep.get(highest)?.content;
}

export function TurnBlock(props: TurnBlockProps): JSX.Element {
  const {
    turn,
    threadId,
    selected,
    onSelect,
    onInspectRow,
    liveByStep,
    rate,
    readOnly,
    isTenantSwitched,
    onDecide,
    deciding,
    onExport,
    exporting,
    onRetry,
    onDownloadArtifact,
    onFireResult,
    rowRef,
  } = props;

  const events = turn.turn.events;
  const summary = useMemo(() => summarizeTurn(events), [events]);
  const settledRows = useMemo(() => compactRowsOf(events), [events]);
  const syntheticRows = useMemo(
    () => liveSyntheticRows(events, liveByStep),
    [events, liveByStep],
  );
  // 合成行的 think `text` 是整段还在增长的 reasoning buffer,要走 CompactRow
  // 的 `liveText` 分支(标签换成 `console.row_think_live`、取最新一行);已落
  // `updates` 帧的 settled 行仍取首行,所以只给合成 think 行带 liveText。
  const rows = useMemo(
    () => [
      ...settledRows.map((row) => ({ row, liveText: undefined as string | undefined })),
      ...syntheticRows.map((row) => ({
        row,
        liveText: row.kind === "think" ? row.text : undefined,
      })),
    ],
    [settledRows, syntheticRows],
  );

  const [expandedIds, setExpandedIds] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const toggleRow = useCallback((id: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const liveText = useMemo(
    () => liveAnswerTextOf(events, liveByStep),
    [events, liveByStep],
  );
  const costCny = useMemo(() => costCnyOf(summary, rate), [summary, rate]);

  const approval = turn.turn.approval;

  return (
    <div
      ref={rowRef}
      data-testid="console-turn"
      data-selected={selected}
      onClick={(e) => {
        if (e.target === e.currentTarget) onSelect(turn.key);
      }}
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 8,
        padding: 8,
        borderRadius: 6,
        flexShrink: 0,
        background: selected ? "var(--ew-surface-selected)" : undefined,
      }}
    >
      <UserBubble
        input={turn.turn.input}
        attachments={turn.turn.attachments}
        inputs={turn.turn.inputs}
      />

      <ProcessStrip
        rows={rows}
        running={turn.turn.status === "running"}
        expandedRowIds={expandedIds}
        onToggleRow={toggleRow}
        onInspectRow={(rowId) => onInspectRow(turn.key, rowId)}
        onFireResult={onFireResult}
      />

      {!readOnly && approval && threadId && (
        <ReadonlyTooltip on={isTenantSwitched} block>
          <ApprovalGate
            approval={approval}
            busy={deciding}
            disabled={isTenantSwitched}
            onDecide={(decision) => onDecide(turn.turn.id, approval, decision)}
          />
        </ReadonlyTooltip>
      )}

      <AnswerBubble
        turn={turn}
        summary={summary}
        liveText={liveText}
        onDownloadArtifact={onDownloadArtifact}
      />

      <TurnFooter
        turn={turn}
        threadId={threadId}
        summary={summary}
        costCny={costCny}
        readOnly={readOnly}
        isTenantSwitched={isTenantSwitched}
        onRetry={onRetry}
        onExport={onExport}
        exporting={exporting}
        onInspect={() => onSelect(turn.key)}
      />
    </div>
  );
}
