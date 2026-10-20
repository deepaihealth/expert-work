/**
 * RowDetailTiming — the Timing tab's two-column table: SSE-observed values
 * (left) vs. the matched Langfuse "exact" span (right). Split out of
 * RowDetail.tsx (Task 17 of the debug-console PR-A plan) to keep that file
 * under its 400-line budget. See
 * .superpowers/sdd/2026-08-18-debug-console-pr-a-console/task-17-brief.md.
 *
 * The Langfuse column is governed by `match.reason`: "matched" renders real
 * per-row values (danger colour + `statusMessage` on `span.level === "error"`);
 * the other three reasons collapse the whole column into one explanatory
 * message spanning all rows (`rowSpan`), "not_ready" adding a refresh button
 * and "incomplete" a Langfuse link (B-201).
 */
import type { ReactNode } from "react";
import { Button, Typography } from "antd";
import { RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";

import { costCnyOfMainLineCall, type RateBook } from "../../api/cost";
import type { RunTrace } from "../../api/trace_facade";
import type { SpanMatch } from "../../api/trace_match";
import type { TrajectoryRow } from "../../api/trajectory_rows";
import { fmtDuration } from "../../pages/agent_detail/playground/duration_format";

const { Text } = Typography;

const DANGER = "var(--ew-text-danger, #cf1322)";
/** 开始 / 结束 / 时长三行,每种记录都有;模型 / tokens / 成本三行只给模型调用。 */
const TIME_ROW_COUNT = 3;
const MODEL_ROW_COUNT = 3;
/** B-203 —— 标签列与其它 tab(概要 `dl` 的 14px)对齐,不再贴着面板边。 */
const LABEL_PAD = "4px 8px 4px 14px";

/** 「输入 / 输出」两个 token 数,带千分位(与轮脚「1,119,191 tok」同一写法)。 */
function fmtTokens(input: number, output: number): string {
  return `${input.toLocaleString()} / ${output.toLocaleString()}`;
}

function fmtClock(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number, len = 2): string => String(n).padStart(len, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
}

export interface RowDetailTimingProps {
  row: TrajectoryRow;
  match: SpanMatch;
  trace: RunTrace | null;
  traceLoading: boolean;
  onRefreshTrace: () => void;
  /** Langfuse deep link (system_admin only, else null) — offered when the
   *  trace is `incomplete`. */
  langfuseUrl?: string | null;
  /** B-202 —— 平台价目表(`useRateBook`;非 system_admin 为 null)。模型调用行的
   *  「成本」按它折成人民币;省略 / null → 「—」。 */
  rateBook?: RateBook | null;
  /** B-203 —— 账本给这条记录排出来的绝对起点(`LedgerRecord.startedAt`);
   *  省略 / null 时按「结束 − 时长」推。 */
  startedAtMs?: number | null;
}

export function RowDetailTiming(props: RowDetailTimingProps) {
  const { row, match, trace, traceLoading, onRefreshTrace, langfuseUrl = null, rateBook = null } =
    props;
  const startedAtMs = props.startedAtMs ?? null;
  const { t } = useTranslation();

  const dash = t("console.detail_none");
  const sseEnd = row.serverMs !== null ? fmtClock(row.serverMs) : dash;
  // B-203 —— 原来只有结束时刻。起点优先用账本排出来的,其次「结束 − 时长」。
  const startMs = startedAtMs ?? (row.serverMs !== null && row.durationMs !== null ? row.serverMs - row.durationMs : null);
  const sseStart = startMs !== null ? fmtClock(startMs) : dash;
  const sseDuration = row.durationMs !== null ? fmtDuration(row.durationMs) : dash;
  // 账本投影(spec §九 D2)把模型 / tokens 从 think 行挪到了一步一条的
  // assistant 行,SSE 列两种行都要取到值。
  const metered = row.kind === "think" || row.kind === "assistant";
  const sseModel = metered ? (row.model ?? dash) : dash;
  const sseTokens = metered ? fmtTokens(row.inputTokens, row.outputTokens) : dash;
  // B-203 —— ask_image 另调了一次看图模型,用量随工具结果回来(只有 token 数,
  // 看图模型的名字不在帧里,所以不写模型、也算不了成本)。
  const visionUsage = row.kind === "tool" ? (row.entry.vlUsage ?? null) : null;
  // B-202 —— 用户拍板:成本按平台价目表、人民币算,不再只靠 Langfuse 的美元
  // (拿不到 trace 时整行都是「—」)。
  const sseCostCny = metered
    ? costCnyOfMainLineCall(
        { input: row.inputTokens, output: row.outputTokens, cacheRead: row.cacheReadTokens ?? 0 },
        rateBook,
      )
    : null;

  const span = match.reason === "matched" ? match.span : null;
  const isError = span !== null && span.level === "error";
  const lfColor = isError ? DANGER : undefined;

  let lfMessage: string | null = null;
  let showRefresh = false;
  let showLangfuseLink = false;
  if (match.reason === "count_mismatch") {
    lfMessage = t("console.timing_mismatch");
  } else if (match.reason === "unsupported") {
    lfMessage = t("console.timing_unsupported");
  } else if (match.reason === "no_trace") {
    if (traceLoading) {
      lfMessage = t("console.timing_loading");
    } else if (trace?.status === "not_ready") {
      lfMessage = t("console.timing_not_ready");
      showRefresh = true;
    } else if (trace?.status === "incomplete") {
      lfMessage = t("console.timing_incomplete");
      showLangfuseLink = true;
    } else if (trace?.status === "unavailable") {
      lfMessage = t("console.timing_unavailable");
    } else {
      lfMessage = t("console.timing_no_trace");
    }
  }

  const lfCost = span?.costUsd ?? null;
  const lfTokens = span !== null && span.inputTokens !== null && span.outputTokens !== null
    ? fmtTokens(span.inputTokens, span.outputTokens)
    : dash;
  // B-203 —— 模型 / tokens / 成本只对模型调用有意义:工具行原来三行全是「—」。
  // 配上的是 LLM span(记忆 / 反思这类辅助调用)时 Langfuse 列有数,照样给。
  const showModelRows = metered || span?.kind === "llm";
  const rowCount = TIME_ROW_COUNT + (showModelRows ? MODEL_ROW_COUNT : 0) + (visionUsage !== null ? 1 : 0);

  const th = (children: ReactNode) => (
    <th scope="row" style={{ textAlign: "left", fontWeight: 400, color: "var(--ew-text-tertiary)", padding: LABEL_PAD }}>
      {children}
    </th>
  );
  const td = (children: ReactNode, color?: string) => (
    <td style={{ padding: "4px 8px", color }}>{children}</td>
  );

  return (
    <table
      data-testid="console-detail-timing"
      style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}
    >
      <thead>
        <tr>
          <th style={{ padding: LABEL_PAD }} />
          <th style={{ textAlign: "left", padding: "4px 8px" }}>{t("console.timing_col_sse")}</th>
          <th style={{ textAlign: "left", padding: "4px 8px" }}>{t("console.timing_col_langfuse")}</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          {th(t("console.timing_row_start_at"))}
          {td(sseStart)}
          {lfMessage !== null ? (
            <td rowSpan={rowCount} style={{ padding: "4px 8px", verticalAlign: "top" }}>
              <Text>{lfMessage}</Text>
              {showRefresh && (
                <Button
                  size="small"
                  icon={<RefreshCw size={11} strokeWidth={1.75} />}
                  onClick={onRefreshTrace}
                  data-testid="console-timing-refresh"
                  style={{ marginLeft: 8 }}
                >
                  {t("common.refresh")}
                </Button>
              )}
              {showLangfuseLink && langfuseUrl !== null && (
                <a
                  data-testid="console-timing-langfuse"
                  href={langfuseUrl}
                  target="_blank"
                  rel="noreferrer"
                  style={{ marginLeft: 8 }}
                >
                  {t("console.timing_open_langfuse")} ↗
                </a>
              )}
            </td>
          ) : (
            td(`${t("console.timing_row_start")}: ${span ? fmtDuration(span.startMs) : dash}`, lfColor)
          )}
        </tr>
        <tr>
          {th(t("console.timing_row_end"))}
          {td(sseEnd)}
          {lfMessage === null &&
            td(
              span
                ? `${t("console.timing_row_end_rel")}: ${fmtDuration(span.startMs + span.latencyMs)}`
                : dash,
              lfColor,
            )}
        </tr>
        <tr>
          {th(t("console.timing_row_latency"))}
          {td(sseDuration)}
          {lfMessage === null && td(span ? fmtDuration(span.latencyMs) : dash, lfColor)}
        </tr>
        {showModelRows && (
          <>
            <tr>
              {th(t("console.timing_row_model"))}
              {td(sseModel)}
              {lfMessage === null && td(span?.model ?? dash, lfColor)}
            </tr>
            <tr>
              {th(t("console.timing_row_tokens"))}
              {td(sseTokens)}
              {lfMessage === null && td(lfTokens, lfColor)}
            </tr>
            <tr>
              {th(t("console.timing_row_cost"))}
              {td(sseCostCny !== null ? `¥${sseCostCny.toFixed(4)}` : dash)}
              {lfMessage === null && td(lfCost !== null ? `$${lfCost.toFixed(4)}` : dash, lfColor)}
            </tr>
          </>
        )}
        {visionUsage !== null && (
          <tr>
            {th(t("console.timing_row_vision_tokens"))}
            {td(fmtTokens(visionUsage.input, visionUsage.output))}
            {lfMessage === null && td(dash)}
          </tr>
        )}
        {isError && span?.statusMessage && (
          <tr>
            {th(null)}
            {td(null)}
            {td(span.statusMessage, DANGER)}
          </tr>
        )}
      </tbody>
    </table>
  );
}
