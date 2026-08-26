/**
 * TurnFooter — the console turn's one-row status/meta/action footer: a
 * status tag, a compact meta summary (tokens · steps · latency · model)
 * with a hover breakdown, the feedback bar (settled, non-read-only,
 * non-tenant-switched turns), and the retry / export / view-trajectory
 * buttons (spec §八.4, PR-A.1 Task 4).
 *
 * Replaces the four-row PR-A footer: ``TurnMeta``'s chip row (and its
 * "view run" link) is gone — the metrics collapse into one meta span +
 * Tooltip breakdown, and the run-detail link moves to the trajectory
 * panel header (Task 6). The retry/export buttons are lifted verbatim
 * from ``components/turn/TurnCard.tsx`` (TurnCard.tsx:1043-1068) — no
 * rendering logic changed there, just consolidated to read ``ConsoleTurn``
 * instead of ``Turn`` + loose props.
 */
import { Button, Tag, Tooltip } from "antd";
import { Download, Route, RotateCcw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import type { TurnSummary } from "../../api/turn_summary";
import { fmtDuration } from "../../pages/agent_detail/playground/duration_format";
import { ReadonlyTooltip } from "../ReadonlyTooltip";
import { FeedbackBar } from "../turn/FeedbackBar";
import type { Turn } from "../turn/types";
import type { ConsoleTurn } from "./types";
import "./turn_footer.css";

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
  /** PR-B Task 3 — ConversationDetail's per-turn "查看运行" deep link to
   *  ``/runs/{threadId}/{runId}``. Omitted → the link doesn't render (the
   *  playground has no run-detail page to link to). */
  runHref?: string;
}

const STATUS_TAG_COLOR: Record<Turn["status"], string> = {
  running: "processing",
  done: "success",
  error: "error",
  // BUG-9 — a cancelled/broken-off run is neither success nor failure.
  interrupted: "warning",
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
  runHref,
}: TurnFooterProps) {
  const { t } = useTranslation();
  const status = turn.turn.status;
  // #10 — failed-turn detection for the retry button's danger styling
  // (mirrors TurnCard.tsx's `turnFailed`, minus the timeline-banner check —
  // this footer has no Gantt/StepTimeline parse of its own).
  const failed = status === "error" || turn.turn.events.some((e) => e.event === "error");

  // 「总耗时」优先取 run 行的墙钟(finished_at − created_at,含建 run 到
  // 第一帧的时间);老后端 / live 轮退回帧时序推出的 latencyMs。带标签渲染
  // —— 裸时长和步耗时并排时分不出哪个是整轮的(2026-08-26 用户反馈)。
  let wallMs: number | null = null;
  if (turn.createdAt !== null && turn.finishedAt !== null) {
    const start = Date.parse(turn.createdAt);
    const end = Date.parse(turn.finishedAt);
    if (!Number.isNaN(start) && !Number.isNaN(end) && end >= start) wallMs = end - start;
  }
  if (wallMs === null) wallMs = summary.latencyMs;

  const meta: string[] = [];
  if (summary.usage) {
    meta.push(
      t("console.footer_tokens", { n: summary.usage.totalTokens.toLocaleString() }),
    );
  }
  if (summary.stepCount !== null) {
    meta.push(t("console.footer_steps", { n: summary.stepCount }));
  }
  if (wallMs !== null) {
    meta.push(t("console.footer_total_duration", { d: fmtDuration(wallMs) }));
  }
  if (summary.modelName) meta.push(summary.modelName);

  const breakdown: string[] = summary.usage
    ? [
        `${t("playground.usage_in")}: ${summary.usage.inputTokens}`,
        `${t("playground.usage_out")}: ${summary.usage.outputTokens}`,
        `${t("playground.usage_cache")}: ${summary.usage.cacheReadTokens}`,
        `${t("playground.usage_reasoning")}: ${summary.usage.reasoningTokens}`,
        ...(costCny !== null ? [`≈ ¥${costCny.toFixed(4)}`] : []),
        ...(summary.finishReason && summary.finishReason !== "stop"
          ? [`${t("playground.meta_finish")}: ${summary.finishReason}`]
          : []),
      ]
    : [];

  // 中断原因(InterruptReason 短码,来自 run 行的 error)—— 「主动取消」和
  // 「断流被杀 / 连带取消」必须分得开;词表外的值 / 老 run 的 null 落回
  // 通用「已中断」。
  const INTERRUPT_REASONS = new Set([
    "user_cancel",
    "client_disconnect",
    "tenant_suspended",
    "agent_disabled",
  ]);
  const interruptReason =
    status === "interrupted" && turn.runError !== null && INTERRUPT_REASONS.has(turn.runError)
      ? turn.runError
      : null;
  const statusLabel =
    interruptReason !== null
      ? t(`console.interrupt_reason_${interruptReason}`)
      : t(`console.footer_status_${status}`);

  return (
    <div className="ew-turn-footer">
      <Tag color={STATUS_TAG_COLOR[status]} bordered={false} data-testid="console-turn-status">
        {statusLabel}
      </Tag>
      {meta.length > 0 && (
        <Tooltip
          title={
            breakdown.length ? (
              <div style={{ whiteSpace: "pre-line" }}>{breakdown.join("\n")}</div>
            ) : undefined
          }
        >
          <span className="ew-turn-footer__meta" data-testid="console-footer-meta">
            {meta.join(" · ")}
          </span>
        </Tooltip>
      )}
      <span className="ew-turn-footer__acts">
        {/* SE-16 — per-turn 👍/👎 quality signal. Settled, non-read-only turns
            only. Track C W2: 切入态只读——反馈是写操作,置灰 + Tooltip。 */}
        {/* 终审 F4 — interrupted turns keep the feedback bar: a run that
            broke off midway is exactly the turn worth a 👎. */}
        {!readOnly && (status === "done" || status === "interrupted") && threadId && (
          <ReadonlyTooltip on={isTenantSwitched}>
            <FeedbackBar threadId={threadId} turnSeq={turn.seq} disabled={isTenantSwitched} />
          </ReadonlyTooltip>
        )}
        {onRetry && status !== "running" && (
          <Button
            type="text"
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
          type="text"
          size="small"
          icon={<Download size={13} strokeWidth={1.75} />}
          loading={exporting}
          onClick={() => onExport(turn.turn)}
          data-testid="playground-export-json"
        >
          {t("console.footer_export")}
        </Button>
        <Button
          type="link"
          size="small"
          icon={<Route size={13} strokeWidth={1.75} />}
          onClick={onInspect}
          data-testid="console-turn-inspect"
        >
          {t("console.footer_view_trajectory")}
        </Button>
        {runHref !== undefined && (
          <Link to={runHref} data-testid="console-turn-run-link">
            {t("console.turn_view_run")}
          </Link>
        )}
      </span>
    </div>
  );
}
