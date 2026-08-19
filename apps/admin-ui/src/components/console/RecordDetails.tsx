/**
 * RecordDetails —— 账本里一条记录的详情(PR-A.2 Task 9,spec §九「详情」)。
 * 壳走 `DetailsFrame`,tab 集按记录类型分三档:
 *
 * - TOOL / SUBTOOL / PLAN / MEMORY / REFLECT / 标记:概要 / 载荷 / 结果 / 计时 / 原始
 * - ASSISTANT:概要 / 预览 / 原文 / 计时 / 原始
 * - USER:概要 / 预览 / 原文 / 原始
 *
 * 「概要」= 顶部 `dl`(层级跳转 + 状态 + 耗时 + ASSISTANT 用量 + Run / Langfuse)
 * 加若干分节预览,分节标题点一下就跳到对应 tab。换记录不重置 tab —— 新记录没有
 * 那个 tab 时这里渲染成概要,**不改父状态**(父组件的选中语义由 Task 10 定)。
 *
 * 投影模型 / 交互参照 deepseek-harness ui-trajectory(MIT)重写。
 */
import { useState, type ReactNode } from "react";
import { Typography } from "antd";
import { ChevronRight } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import type { RunTrace } from "../../api/trace_facade";
import type { SpanMatch } from "../../api/trace_match";
import type { AssistantRow, UserRow } from "../../api/trajectory_rows";
import type { FireNowResult } from "../../api/triggers";
import { fmtDuration } from "../../pages/agent_detail/playground/duration_format";
import { EventCard } from "../EventCard";
import { FullTextModal, type FullTextState } from "../turn/FullTextModal";
import { DetailRow, DetailsFrame, HierLink } from "./DetailsFrame";
import { kindLabel } from "./kind_label";
import type { LedgerRecord, LedgerRequest } from "./ledger_types";
import {
  AssistantPreview,
  AssistantRawText,
  RowDetailPayload,
  RowDetailResult,
} from "./RowDetailPayloadResult";
import { schemaToolNameOf, SchemaPanel } from "./RowDetailSchema";
import { RowDetailTiming } from "./RowDetailTiming";
import type { ToolSchemaState } from "./useAgentTools";

import "./kind_tag.css";
import "./record_details.css";

const { Text } = Typography;

export type RecordTab =
  | "summary"
  | "payload"
  | "result"
  | "schema"
  | "preview"
  | "rawtext"
  | "timing"
  | "raw";

const USER_TABS: RecordTab[] = ["summary", "preview", "rawtext", "raw"];
const ASSISTANT_TABS: RecordTab[] = ["summary", "preview", "rawtext", "timing", "raw"];
const OTHER_TABS: RecordTab[] = ["summary", "payload", "result", "timing", "raw"];
/** TOOL rows, and PLAN rows for `update_plan` — the ones a tool schema
 *  resolves for (`schemaToolNameOf(record.row) !== null`). */
const TOOLISH_TABS: RecordTab[] = ["summary", "payload", "result", "schema", "timing", "raw"];

export function recordTabsOf(record: LedgerRecord): RecordTab[] {
  if (record.kind === "user") return USER_TABS;
  if (record.kind === "assistant") return ASSISTANT_TABS;
  if (schemaToolNameOf(record.row) !== null) return TOOLISH_TABS;
  return OTHER_TABS;
}

/** 概要里带预览的分节;占位记录一节都不出(没内容可预览)。 */
function sectionsOf(record: LedgerRecord): RecordTab[] {
  if (record.placeholder !== null) return [];
  if (record.kind === "user") return ["preview"];
  if (record.kind === "assistant") return ["preview", "timing"];
  return ["payload", "result", "timing"];
}

export interface RecordDetailsProps {
  record: LedgerRecord;
  /** `record.ownerRequestNo` 对应的请求;没有 `null`。 */
  ownerRequest: LedgerRequest | null;
  /** `record.parentId` 对应的记录;没有 `null`。 */
  parent: LedgerRecord | null;
  threadId: string | null;
  isSystemAdmin: boolean;
  /** 调用方算好的 Langfuse 深链(`buildLangfuseTraceUrl`);这里只渲染。 */
  langfuseUrl: string | null;
  match: SpanMatch;
  trace: RunTrace | null;
  traceLoading: boolean;
  onRefreshTrace: () => void;
  onOpenRecord: (id: string) => void;
  onOpenRequest: (no: number) => void;
  onFireResult?: (r: FireNowResult) => void;
  /** PR-A.3 §十.2 — the Schema tab's lazy fetch state; the parent owns the
   *  `useAgentTools` hook so it survives switching between records. */
  toolSchemas: ToolSchemaState;
  activeTab: RecordTab;
  onTabChange: (t: RecordTab) => void;
  onClose: () => void;
  width: number;
  onWidthChange: (w: number | null) => void;
  splitWidth: number;
}

/** 「预览」/「原文」两个 tab 只对 USER / ASSISTANT 行开,窄化在这里做一次。 */
function asTextRow(record: LedgerRecord): UserRow | AssistantRow | null {
  const row = record.row;
  return row.kind === "user" || row.kind === "assistant" ? row : null;
}

function DetailSection({
  tab,
  label,
  onOpen,
  children,
}: {
  tab: RecordTab;
  label: string;
  onOpen: () => void;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <section className="ew-detail__section" data-testid={`console-detail-section-${tab}`}>
      <h3 className="ew-detail__section-h">
        <button
          type="button"
          className="ew-detail__section-btn"
          aria-label={t("console.detail_open_tab", { tab: label })}
          onClick={onOpen}
        >
          <span>{label}</span>
          <ChevronRight size={12} strokeWidth={1.75} aria-hidden />
        </button>
      </h3>
      <div className="ew-detail__preview">{children}</div>
    </section>
  );
}

export function RecordDetails(props: RecordDetailsProps) {
  const { record, ownerRequest, parent, threadId, isSystemAdmin, langfuseUrl } = props;
  const { match, trace, traceLoading, onRefreshTrace, onOpenRecord, onOpenRequest } = props;
  const { activeTab, onTabChange, onFireResult, toolSchemas } = props;
  const { t } = useTranslation();
  const [fullText, setFullText] = useState<FullTextState | null>(null);

  const row = record.row;
  const textRow = asTextRow(record);
  const tabs = recordTabsOf(record);
  const active = tabs.includes(activeTab) ? activeTab : "summary";
  const dash = t("console.detail_none");
  const label = (tab: RecordTab): string => t(`console.detail_tab_${tab}`);

  const durationMs =
    record.startedAt !== null && record.endedAt !== null
      ? record.endedAt - record.startedAt
      : row.durationMs;

  const tabNode = (tab: RecordTab): ReactNode => {
    switch (tab) {
      case "payload":
        return <RowDetailPayload row={row} match={match} events={record.events} />;
      case "result":
        return <RowDetailResult row={row} onFireResult={onFireResult} onOpenFullText={setFullText} />;
      case "schema": {
        const toolName = schemaToolNameOf(row);
        return toolName === null ? null : <SchemaPanel toolName={toolName} state={toolSchemas} />;
      }
      case "preview":
        return textRow === null ? null : <AssistantPreview row={textRow} />;
      case "rawtext":
        return textRow === null ? null : (
          <AssistantRawText row={textRow} onOpenFullText={setFullText} />
        );
      case "timing":
        return (
          <RowDetailTiming
            row={row}
            match={match}
            trace={trace}
            traceLoading={traceLoading}
            onRefreshTrace={onRefreshTrace}
          />
        );
      case "raw":
        return <RawFrames record={record} />;
      default:
        return null;
    }
  };

  const hierarchy: ReactNode[] = [];
  if (row.kind === "assistant" && ownerRequest !== null) {
    hierarchy.push(
      <HierLink
        key="request"
        testId="console-detail-hier-request"
        label={t("console.detail_hier_request", { n: ownerRequest.no })}
        onClick={() => onOpenRequest(ownerRequest.no)}
      />,
    );
  }
  const parentIsAssistant =
    row.kind === "tool" ||
    row.kind === "plan" ||
    (row.kind === "memory" && row.direction === "writeback");
  if (parentIsAssistant && parent !== null) {
    hierarchy.push(
      <HierLink
        key="assistant"
        testId="console-detail-hier-assistant"
        label={t("console.detail_hier_assistant")}
        onClick={() => onOpenRecord(parent.id)}
      />,
    );
  }
  if (row.kind === "subagent" && parent !== null) {
    hierarchy.push(
      <HierLink
        key="tool"
        testId="console-detail-hier-tool"
        label={t("console.detail_hier_tool")}
        onClick={() => onOpenRecord(parent.id)}
      />,
    );
  }

  const summary = (
    <div data-testid="console-detail-summary" className="ew-detail__summary">
      <dl className="ew-detail__ov">
        {record.placeholder !== null && (
          <DetailRow label="">
            <Text type="secondary">{t("console.detail_placeholder")}</Text>
          </DetailRow>
        )}
        {hierarchy.length > 0 && (
          <DetailRow label={t("console.detail_hierarchy")}>
            <span className="ew-detail__hier-list">{hierarchy}</span>
          </DetailRow>
        )}
        <DetailRow label={t("console.detail_status")}>
          {t(`console.traj_status_${row.status}`)}
        </DetailRow>
        <DetailRow label={t("console.detail_duration")}>
          {durationMs !== null ? fmtDuration(durationMs) : dash}
        </DetailRow>
        {row.kind === "assistant" && (
          <>
            <DetailRow label={t("console.detail_model")}>{row.model ?? dash}</DetailRow>
            <DetailRow label={t("console.detail_usage_input")}>{row.inputTokens}</DetailRow>
            <DetailRow label={t("console.detail_usage_output")}>{row.outputTokens}</DetailRow>
            {row.reasoningTokens !== undefined && (
              <DetailRow label={t("console.detail_usage_reasoning")}>
                {row.reasoningTokens}
              </DetailRow>
            )}
            {row.cacheReadTokens !== undefined && (
              <DetailRow label={t("console.detail_usage_cache_read")}>
                {row.cacheReadTokens}
              </DetailRow>
            )}
          </>
        )}
        {threadId !== null && record.runId !== null && (
          <DetailRow label={t("console.detail_run")}>
            <Link data-testid="console-inspect-run-link" to={`/runs/${threadId}/${record.runId}`}>
              {record.runId} ↗
            </Link>
          </DetailRow>
        )}
        {isSystemAdmin && langfuseUrl !== null && (
          <DetailRow label="Langfuse">
            <a
              data-testid="playground-turn-langfuse"
              href={langfuseUrl}
              target="_blank"
              rel="noreferrer"
            >
              {t("trace_toolbar.open_in_langfuse")} ↗
            </a>
          </DetailRow>
        )}
      </dl>
      <div className="ew-detail__sections">
        {sectionsOf(record).map((tab) => (
          <DetailSection key={tab} tab={tab} label={label(tab)} onOpen={() => onTabChange(tab)}>
            {tabNode(tab)}
          </DetailSection>
        ))}
      </div>
    </div>
  );

  return (
    <DetailsFrame
      width={props.width}
      onWidthChange={props.onWidthChange}
      splitWidth={props.splitWidth}
      onClose={props.onClose}
      activeTab={active}
      onTabChange={(k) => onTabChange(k as RecordTab)}
      tabs={tabs.map((tab) => ({
        key: tab,
        label: label(tab),
        testId: `console-detail-tab-${tab}`,
      }))}
      header={
        <>
          {/* 标签文案与账本行、时间轴提示同一份(`kind_label.ts`);失败态靠
              `data-error` 交给样式表染红,与账本行那条选择器同一个变量。 */}
          <span className={`ew-kt ew-kt--${record.kind}`} data-error={record.isError ? "true" : undefined}>
            {kindLabel(record.kind, t)}
          </span>
          <span className="ew-detail__loc">
            {row.step === null
              ? t("console.detail_level_turn_only", { turn: record.turnSeq + 1 })
              : t("console.detail_level", { turn: record.turnSeq + 1, step: row.step })}
          </span>
        </>
      }
    >
      {active === "summary" ? summary : tabNode(active)}
      <FullTextModal state={fullText} onClose={() => setFullText(null)} />
    </DetailsFrame>
  );
}

/** 「原始」tab —— 该记录的帧下标解到本轮全部帧上,一帧一张 `EventCard`
 *  (从 `RowDetail.tsx` 的 `RawTab` 搬来,Task 11 删掉那份)。 */
function RawFrames({ record }: { record: LedgerRecord }) {
  const { t } = useTranslation();
  const frames = record.row.eventIndexes
    .map((i) => record.events[i])
    .filter((e): e is NonNullable<typeof e> => e !== undefined);

  return (
    <div
      data-testid="console-detail-raw"
      style={{ display: "flex", flexDirection: "column", gap: 8 }}
    >
      {frames.length === 0 ? (
        <Text type="secondary">{t("console.detail_no_frames")}</Text>
      ) : (
        frames.map((evt, i) => <EventCard key={i} evt={evt} />)
      )}
    </div>
  );
}
