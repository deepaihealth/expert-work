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
 * message spanning all rows (`rowSpan`), "not_ready" adding a refresh button.
 */
import type { ReactNode } from "react";
import { Button, Typography } from "antd";
import { RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { RunTrace } from "../../api/trace_facade";
import type { SpanMatch } from "../../api/trace_match";
import type { TrajectoryRow } from "../../api/trajectory_rows";
import { fmtDuration } from "../../pages/agent_detail/playground/duration_format";

const { Text } = Typography;

const DANGER = "var(--ew-text-danger, #cf1322)";
const ROW_COUNT = 5;

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
}

export function RowDetailTiming(props: RowDetailTimingProps) {
  const { row, match, trace, traceLoading, onRefreshTrace } = props;
  const { t } = useTranslation();

  const dash = t("console.detail_none");
  const sseEnd = row.serverMs !== null ? fmtClock(row.serverMs) : dash;
  const sseDuration = row.durationMs !== null ? fmtDuration(row.durationMs) : dash;
  // 账本投影(spec §九 D2)把模型 / tokens 从 think 行挪到了一步一条的
  // assistant 行,SSE 列两种行都要取到值。
  const metered = row.kind === "think" || row.kind === "assistant";
  const sseModel = metered ? (row.model ?? dash) : dash;
  const sseTokens = metered ? `${row.inputTokens} / ${row.outputTokens}` : dash;

  const span = match.reason === "matched" ? match.span : null;
  const isError = span !== null && span.level === "error";
  const lfColor = isError ? DANGER : undefined;

  let lfMessage: string | null = null;
  let showRefresh = false;
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
    } else if (trace?.status === "unavailable") {
      lfMessage = t("console.timing_unavailable");
    } else {
      lfMessage = t("console.timing_no_trace");
    }
  }

  const lfCost = span?.costUsd ?? null;
  const lfTokens = span !== null && span.inputTokens !== null && span.outputTokens !== null
    ? `${span.inputTokens} / ${span.outputTokens}`
    : dash;

  const th = (children: ReactNode) => (
    <th scope="row" style={{ textAlign: "left", fontWeight: 400, color: "var(--ew-text-tertiary)", padding: "4px 8px 4px 0" }}>
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
          <th style={{ padding: "4px 8px 4px 0" }} />
          <th style={{ textAlign: "left", padding: "4px 8px" }}>{t("console.timing_col_sse")}</th>
          <th style={{ textAlign: "left", padding: "4px 8px" }}>{t("console.timing_col_langfuse")}</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          {th(null)}
          {td(`${t("console.timing_row_end")}: ${sseEnd}`)}
          {lfMessage !== null ? (
            <td rowSpan={ROW_COUNT} style={{ padding: "4px 8px", verticalAlign: "top" }}>
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
            </td>
          ) : (
            td(`${t("console.timing_row_start")}: ${span ? fmtDuration(span.startMs) : dash}`, lfColor)
          )}
        </tr>
        <tr>
          {th(t("console.timing_row_latency"))}
          {td(sseDuration)}
          {lfMessage === null && td(span ? fmtDuration(span.latencyMs) : dash, lfColor)}
        </tr>
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
          {td(dash)}
          {lfMessage === null && td(lfCost !== null ? `$${lfCost.toFixed(4)}` : dash, lfColor)}
        </tr>
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
