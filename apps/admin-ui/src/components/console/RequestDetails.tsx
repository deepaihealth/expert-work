/**
 * RequestDetails —— 账本里一次主模型调用(「请求 #N」圆点)的详情
 * (PR-A.2 Task 9,spec §九「详情」)。壳走 `DetailsFrame`,四个 tab:
 *
 * - 概要:状态 / 模型 / 结束原因 / 工具调用 /「结果 ›」跳回该步的 ASSISTANT 记录
 *   + Run / Langfuse 链接;
 * - 输入:该步 Langfuse span 的渲染消息(没配到 span 就给现有那句提示);
 * - 用量:本次 输入 / 输出 / 思考 / 缓存读 + 累计至此 输入 / 输出;
 * - 计时:复用 `RowDetailTiming`(会话时间戳 vs Langfuse 精确两列)。
 *
 * 投影模型 / 交互参照 deepseek-harness ui-trajectory(MIT)重写。
 */
import { Typography } from "antd";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import type { RunTrace } from "../../api/trace_facade";
import type { SpanMatch } from "../../api/trace_match";
import { DetailRow, DetailsFrame, HierLink } from "./DetailsFrame";
import type { LedgerRecord, LedgerRequest } from "./ledger_types";
import { RenderedIo } from "./RowDetailPayloadResult";
import { RowDetailTiming } from "./RowDetailTiming";

import "./record_details.css";

const { Text } = Typography;

export type RequestTab = "summary" | "input" | "usage" | "timing";

const REQUEST_TABS: RequestTab[] = ["summary", "input", "usage", "timing"];

export interface RequestDetailsProps {
  request: LedgerRequest;
  /** 该请求的 assistant 记录(`request.recordId` 对应)。 */
  record: LedgerRecord;
  threadId: string | null;
  isSystemAdmin: boolean;
  langfuseUrl: string | null;
  match: SpanMatch;
  trace: RunTrace | null;
  traceLoading: boolean;
  onRefreshTrace: () => void;
  onOpenRecord: (id: string) => void;
  activeTab: RequestTab;
  onTabChange: (t: RequestTab) => void;
  onClose: () => void;
  width: number;
  onWidthChange: (w: number | null) => void;
  splitWidth: number;
}

function UsagePanel({ request }: { request: LedgerRequest }) {
  const { t } = useTranslation();
  return (
    <div data-testid="console-detail-usage">
      <dl className="ew-detail__ov">
        <DetailRow label={t("console.detail_usage_input")}>{request.usage.input}</DetailRow>
        <DetailRow label={t("console.detail_usage_output")}>{request.usage.output}</DetailRow>
        <DetailRow label={t("console.detail_usage_reasoning")}>{request.usage.reasoning}</DetailRow>
        <DetailRow label={t("console.detail_usage_cache_read")}>{request.usage.cacheRead}</DetailRow>
      </dl>
      <h4 className="ew-detail__usage-h">{t("console.detail_usage_cumulative")}</h4>
      <dl className="ew-detail__ov">
        <DetailRow label={t("console.detail_usage_input")}>{request.cumulative.input}</DetailRow>
        <DetailRow label={t("console.detail_usage_output")}>{request.cumulative.output}</DetailRow>
      </dl>
    </div>
  );
}

export function RequestDetails(props: RequestDetailsProps) {
  const { request, record, threadId, isSystemAdmin, langfuseUrl } = props;
  const { match, trace, traceLoading, onRefreshTrace, onOpenRecord, activeTab } = props;
  const { t } = useTranslation();
  const dash = t("console.detail_none");
  const io = match.reason === "matched" ? (match.span?.input ?? null) : null;

  const summary = (
    <div data-testid="console-detail-summary" className="ew-detail__summary">
      <dl className="ew-detail__ov">
        <DetailRow label={t("console.detail_status")}>
          {t(`console.traj_status_${request.status}`)}
        </DetailRow>
        <DetailRow label={t("console.detail_model")}>{request.model ?? dash}</DetailRow>
        <DetailRow label={t("console.detail_finish_reason")}>
          {request.finishReason ?? dash}
        </DetailRow>
        <DetailRow label={t("console.detail_tool_calls")}>{request.toolCalls}</DetailRow>
        <DetailRow label={t("console.detail_tab_result")}>
          <HierLink
            testId="console-detail-hier-assistant"
            label={t("console.detail_hier_assistant")}
            onClick={() => onOpenRecord(record.id)}
          />
        </DetailRow>
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
    </div>
  );

  let body = summary;
  if (activeTab === "input") {
    body = (
      <div data-testid="console-detail-input">
        {io === null ? <Text type="secondary">{t("console.detail_need_langfuse")}</Text> : <RenderedIo io={io} />}
      </div>
    );
  } else if (activeTab === "usage") {
    body = <UsagePanel request={request} />;
  } else if (activeTab === "timing") {
    body = (
      <RowDetailTiming
        row={record.row}
        match={match}
        trace={trace}
        traceLoading={traceLoading}
        onRefreshTrace={onRefreshTrace}
      />
    );
  }

  return (
    <DetailsFrame
      width={props.width}
      onWidthChange={props.onWidthChange}
      splitWidth={props.splitWidth}
      onClose={props.onClose}
      activeTab={activeTab}
      onTabChange={(k) => props.onTabChange(k as RequestTab)}
      tabs={REQUEST_TABS.map((tab) => ({
        key: tab,
        label: t(`console.detail_tab_${tab}`),
        testId: `console-detail-tab-${tab}`,
      }))}
      header={
        <>
          <span className="ew-detail__dot" aria-hidden />
          <span className="ew-detail__req-no">
            {t("console.detail_request_title", { n: request.no })}
          </span>
          <span className="ew-detail__loc">
            {request.step === null
              ? t("console.detail_level_turn_only", { turn: request.turnSeq + 1 })
              : t("console.detail_level", { turn: request.turnSeq + 1, step: request.step })}
          </span>
        </>
      }
    >
      {body}
    </DetailsFrame>
  );
}
