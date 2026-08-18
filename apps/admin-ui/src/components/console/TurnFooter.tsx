/**
 * TurnFooter — the console turn's metric/action row: status tag, ``TurnMeta``
 * (usage/latency/cost/model chips + view-run link), the feedback bar (settled,
 * non-read-only, non-tenant-switched turns), and the retry / export / inspect
 * buttons.
 *
 * JSX lifted from ``components/turn/TurnCard.tsx``'s feedback-bar block
 * (TurnCard.tsx:881-899) and its export/retry buttons (TurnCard.tsx:1043-
 * 1068) — no rendering logic changed, just consolidated to read
 * ``ConsoleTurn`` instead of ``Turn`` + loose props. ``runIdOf`` comes from
 * ``console_turns.ts`` (NOT ``TurnCard.tsx`` — see that module's docstring).
 * See .superpowers/sdd/2026-08-18-debug-console-pr-a-console/task-10-brief.md.
 */
import { Button, Space, Tag } from "antd";
import { RotateCcw, Download } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { TurnSummary } from "../../api/turn_summary";
import { ReadonlyTooltip } from "../ReadonlyTooltip";
import { TurnMeta } from "../../pages/agent_detail/playground/TurnMeta";
import { runIdOf } from "./console_turns";
import { FeedbackBar } from "../turn/FeedbackBar";
import type { Turn } from "../turn/types";
import type { ConsoleTurn } from "./types";

export interface TurnFooterProps {
  turn: ConsoleTurn;
  threadId: string | null;
  summary: TurnSummary;
  /** ≈CNY for the turn (null when no usage or no rate). */
  costCny: number | null;
  readOnly: boolean;
  isTenantSwitched: boolean;
  /** Omitted → the retry button doesn't render (read-only conversation page). */
  onRetry?: (turn: Turn) => void;
  onExport: (turn: Turn) => void;
  exporting: boolean;
  onInspect: () => void;
  selected: boolean;
}

const STATUS_TAG_COLOR: Record<Turn["status"], string> = {
  running: "processing",
  done: "success",
  error: "error",
};

export function TurnFooter({
  turn,
  threadId,
  summary,
  costCny,
  readOnly,
  isTenantSwitched,
  onRetry,
  onExport,
  exporting,
  onInspect,
  selected,
}: TurnFooterProps) {
  const { t } = useTranslation();
  const status = turn.turn.status;
  const runId = runIdOf(turn.turn.events);
  // #10 — failed-turn detection for the retry button's danger styling
  // (mirrors TurnCard.tsx's `turnFailed`, minus the timeline-banner check —
  // this footer has no Gantt/StepTimeline parse of its own).
  const failed = status === "error" || turn.turn.events.some((e) => e.event === "error");

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <Tag
          color={STATUS_TAG_COLOR[status]}
          bordered={false}
          data-testid="console-turn-status"
        >
          {t(`console.footer_status_${status}`)}
        </Tag>
        <TurnMeta summary={summary} costCny={costCny} runId={runId} threadId={threadId} />
      </div>

      {/* SE-16 — per-turn 👍/👎 quality signal. Settled, non-read-only turns
          only. Track C W2: 切入态只读——反馈是写操作,置灰 + Tooltip。 */}
      {!readOnly && status === "done" && threadId && (
        <ReadonlyTooltip on={isTenantSwitched} block>
          <FeedbackBar threadId={threadId} turnSeq={turn.seq} disabled={isTenantSwitched} />
        </ReadonlyTooltip>
      )}

      <Space size={8}>
        {onRetry && status !== "running" && (
          <Button
            size="small"
            danger={failed}
            icon={<RotateCcw size={13} strokeWidth={1.75} />}
            onClick={() => onRetry(turn.turn)}
            data-testid="playground-turn-retry"
          >
            {t("playground.retry")}
          </Button>
        )}
        <Button
          size="small"
          icon={<Download size={13} strokeWidth={1.75} />}
          loading={exporting}
          onClick={() => onExport(turn.turn)}
          data-testid="playground-export-json"
        >
          {t("playground.export_json")}
        </Button>
        <Button
          size="small"
          type={selected ? "primary" : "default"}
          onClick={onInspect}
          data-testid="console-turn-inspect"
        >
          {t("console.footer_inspect")}
        </Button>
      </Space>
    </div>
  );
}
