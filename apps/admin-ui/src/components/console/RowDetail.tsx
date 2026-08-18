/**
 * RowDetail — a trajectory row's detail panel: five tabs (Summary / Payload /
 * Result / Timing / Raw). Task 17 of the debug-console PR-A plan. See
 * .superpowers/sdd/2026-08-18-debug-console-pr-a-console/task-17-brief.md.
 *
 * Tab shell + Summary + Raw live here; the heavier per-kind Payload/Result
 * rendering split out to RowDetailPayloadResult.tsx, and the Timing
 * two-column table to RowDetailTiming.tsx, to keep this file under its
 * 400-line budget.
 */
import { useState, type ReactNode } from "react";
import { Button, Tabs, Typography } from "antd";
import { X } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { SseEvent } from "../../api/sessions";
import type { RunTrace } from "../../api/trace_facade";
import type { SpanMatch } from "../../api/trace_match";
import type { TrajectoryRow } from "../../api/trajectory_rows";
import type { FireNowResult } from "../../api/triggers";
import { fmtDuration } from "../../pages/agent_detail/playground/duration_format";
import { EventCard } from "../EventCard";
import { FullTextModal, type FullTextState } from "../turn/FullTextModal";
import { RowDetailPayload, RowDetailResult } from "./RowDetailPayloadResult";
import { RowDetailTiming } from "./RowDetailTiming";

const { Text } = Typography;

/** 自己带内边距 —— 详情面板是 Splitter 的一格,不给 padding 头部会贴着左边被
 *  裁掉一截(测试环境上报的 bug,spec §八.8)。 */
const ROOT_STYLE: React.CSSProperties = { padding: "8px 12px", height: "100%", overflow: "auto" };

export type RowDetailTab = "summary" | "payload" | "result" | "timing" | "raw";

export interface RowDetailProps {
  row: TrajectoryRow;
  /** 该行在**全表**里的 1-based 序号(行表的 `#` 列 / 泳道块的 `#` 同源);
   *  头部只显示它 —— 「第 N 轮」在右栏面板头部已经有了(spec §八.8)。 */
  rowIndex: number;
  /** 0-based; displayed as +1. */
  turnSeq: number;
  /** Raw tab resolves `row.eventIndexes` against this full frame list. */
  events: readonly SseEvent[];
  match: SpanMatch;
  trace: RunTrace | null;
  traceLoading: boolean;
  onRefreshTrace: () => void;
  onFireResult?: (result: FireNowResult) => void;
  onClose: () => void;
}

type TFn = ReturnType<typeof useTranslation>["t"];

/** First-line summary for the header. Task 16's `rowSummary` (`./TrajectoryRows`)
 *  isn't landed in this worktree yet — this small private stand-in covers the
 *  header only; the controller dedupes the two once Task 16 merges. */
function headerSummary(row: TrajectoryRow, t: TFn): string {
  const firstLine = (s: string): string => s.split("\n")[0]?.trim() ?? "";
  switch (row.kind) {
    case "think":
      if (row.text.trim() !== "") return firstLine(row.text);
      if (row.content !== null && row.content.trim() !== "") return firstLine(row.content);
      return t("console.traj_llm_call", { model: row.model ?? "" });
    case "tool":
      return row.entry.toolName;
    case "plan":
      return row.goal ?? row.reason ?? "";
    case "memory":
      return t(row.direction === "recall" ? "console.row_memory_recall" : "console.row_memory_writeback", {
        n: row.count,
      });
    case "reflect":
      return t(row.verdict === "pass" ? "console.row_reflect_pass" : "console.row_reflect_revise");
    case "subagent":
      return row.worker.label;
    default:
      // user / assistant / marker kinds (compaction, retry, error, approval,
      // guard, gap) all carry a plain `text` field.
      return firstLine(row.text);
  }
}

function SummaryRow({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 10, padding: "3px 0", fontSize: 12.5 }}>
      <Text type="secondary" style={{ minWidth: 90, flexShrink: 0 }}>
        {label}
      </Text>
      <span style={{ color: "var(--ew-text-primary)" }}>{value}</span>
    </div>
  );
}

/** Per-kind rows appended after level / status / duration (Summary tab). */
function kindRows(row: TrajectoryRow, t: TFn): { label: string; value: ReactNode }[] {
  switch (row.kind) {
    case "think":
      return [
        { label: t("console.detail_model"), value: row.model ?? t("console.detail_none") },
        { label: "", value: t("console.detail_tokens", { in: row.inputTokens, out: row.outputTokens }) },
        { label: t("console.detail_finish_reason"), value: row.finishReason ?? t("console.detail_none") },
      ];
    case "tool": {
      const rows = [{ label: t("console.detail_tool"), value: row.entry.toolName }];
      if (row.entry.server !== null) rows.push({ label: t("console.detail_server"), value: row.entry.server });
      if (row.entry.action) rows.push({ label: t("console.detail_action"), value: row.entry.action });
      return rows;
    }
    case "plan":
      return [
        { label: t("console.detail_steps_total"), value: row.stepsTotal },
        { label: t("console.detail_goal"), value: row.goal ?? t("console.detail_none") },
      ];
    case "memory":
      return [{ label: t("console.detail_count"), value: row.count }];
    case "subagent":
      return [
        {
          label: t("console.detail_worker"),
          value: `${row.worker.label} · ${t(`playground.tl_worker_${row.worker.status}`)}`,
        },
        { label: t("console.detail_worker_steps"), value: row.worker.steps.length },
      ];
    case "user":
      return [
        { label: t("console.detail_attachments"), value: row.attachmentNames.length },
        { label: t("console.detail_variables"), value: Object.keys(row.inputs).length },
      ];
    case "assistant":
      return [{ label: t("console.detail_chars"), value: row.text.length }];
    default:
      return [];
  }
}

function SummaryTab({ row, turnSeq }: { row: TrajectoryRow; turnSeq: number }) {
  const { t } = useTranslation();
  const levelValue =
    row.step === null
      ? t("console.detail_level_turn_only", { turn: turnSeq + 1 })
      : t("console.detail_level", { turn: turnSeq + 1, step: row.step });

  return (
    <div data-testid="console-detail-summary">
      <SummaryRow label="" value={levelValue} />
      <SummaryRow label={t("console.detail_status")} value={t(`console.traj_status_${row.status}`)} />
      <SummaryRow
        label={t("console.detail_duration")}
        value={row.durationMs !== null ? fmtDuration(row.durationMs) : t("console.detail_none")}
      />
      {kindRows(row, t).map((r, i) => (
        <SummaryRow key={i} label={r.label} value={r.value} />
      ))}
    </div>
  );
}

function RawTab({ row, events }: { row: TrajectoryRow; events: readonly SseEvent[] }) {
  const { t } = useTranslation();
  const frames = row.eventIndexes
    .map((i) => events[i])
    .filter((e): e is SseEvent => e !== undefined);

  return (
    <div data-testid="console-detail-raw" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {frames.length === 0 ? (
        <Text type="secondary">{t("console.detail_no_frames")}</Text>
      ) : (
        frames.map((evt, i) => <EventCard key={i} evt={evt} />)
      )}
    </div>
  );
}

export function RowDetail(props: RowDetailProps) {
  const { row, rowIndex, turnSeq, events, match, trace, traceLoading, onRefreshTrace, onFireResult, onClose } = props;
  const { t } = useTranslation();
  // Not reset on `row.id` change (no effect keyed off it) — the user
  // inspecting Timing stays on Timing when they pick a different row.
  const [activeTab, setActiveTab] = useState<RowDetailTab>("summary");
  const [fullText, setFullText] = useState<FullTextState | null>(null);

  const tabs: RowDetailTab[] = ["summary", "payload", "result", "timing", "raw"];
  const items = tabs.map((key) => ({
    key,
    label: <span data-testid={`console-detail-tab-${key}`}>{t(`console.detail_tab_${key}`)}</span>,
    children:
      key === "summary" ? (
        <SummaryTab row={row} turnSeq={turnSeq} />
      ) : key === "payload" ? (
        <RowDetailPayload row={row} match={match} events={events} />
      ) : key === "result" ? (
        <RowDetailResult row={row} onFireResult={onFireResult} onOpenFullText={setFullText} />
      ) : key === "timing" ? (
        <RowDetailTiming
          row={row}
          match={match}
          trace={trace}
          traceLoading={traceLoading}
          onRefreshTrace={onRefreshTrace}
        />
      ) : (
        <RawTab row={row} events={events} />
      ),
  }));

  return (
    <div style={ROOT_STYLE}>
      <div
        data-testid="console-detail-header"
        style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}
      >
        <Text style={{ fontSize: 11, fontFamily: "var(--ew-font-mono)", color: "var(--ew-text-tertiary)" }}>
          {`#${rowIndex}`}
        </Text>
        <Text type="secondary" style={{ fontSize: 11, fontFamily: "var(--ew-font-mono)" }}>
          {t(`console.traj_kind_${row.kind}`)}
        </Text>
        <Text
          type="secondary"
          ellipsis
          style={{ fontSize: 12, flex: 1, minWidth: 0 }}
        >
          {headerSummary(row, t)}
        </Text>
        {row.durationMs !== null && (
          <Text type="secondary" style={{ fontSize: 11, fontFamily: "var(--ew-font-mono)" }}>
            {fmtDuration(row.durationMs)}
          </Text>
        )}
        <Button
          type="text"
          size="small"
          icon={<X size={14} strokeWidth={1.75} />}
          onClick={onClose}
          aria-label={t("console.detail_close")}
          data-testid="console-detail-close"
        />
      </div>
      <Tabs
        size="small"
        activeKey={activeTab}
        onChange={(key) => setActiveTab(key as RowDetailTab)}
        items={items}
      />
      <FullTextModal state={fullText} onClose={() => setFullText(null)} />
    </div>
  );
}
