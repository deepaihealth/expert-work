/**
 * TrajectoryPanel — the debug console's right-rail trajectory container:
 * header (turn label + tool-count chips + admin-only Langfuse link) / status
 * banner / lane strip / row list, with an inline `Splitter` opening
 * `RowDetail` below the row list once a row is selected. Composes Task 4's
 * `trajectoryRowsOf` + Task 5's `liveSyntheticRows` into one row list, Task
 * 14's `useRunTrace` + `matchTraceSpans` for the Timing tab's span pairing,
 * and Tasks 15-17's `LaneStrip` / `TrajectoryRows` / `RowDetail`. Task 18 of
 * the debug-console PR-A plan. See
 * .superpowers/sdd/2026-08-18-debug-console-pr-a-console/task-18-brief.md.
 */
import { useEffect, useMemo, useState, type JSX } from "react";
import { Empty, Splitter, Tag } from "antd";
import { useTranslation } from "react-i18next";

import { parseTimeline } from "../../api/timeline";
import { toolStatusSummary } from "../../api/tool_timeline";
import { matchTraceSpans } from "../../api/trace_match";
import { trajectoryRowsOf } from "../../api/trajectory_rows";
import type { FireNowResult } from "../../api/triggers";
import { summarizeTurn } from "../../api/turn_summary";
import { buildLangfuseTraceUrl } from "../../config/env";
import { RunStatusBanner } from "../../pages/agent_detail/playground/RunStatusBanner";
import { timelineBannerModel } from "../../pages/agent_detail/playground/timeline_banner";
import type { LiveStep } from "../../pages/agent_detail/playground/useTokenStream";
import { LaneStrip } from "./LaneStrip";
import { liveSyntheticRows } from "./live_rows";
import { RowDetail } from "./RowDetail";
import { TrajectoryRows } from "./TrajectoryRows";
import type { ConsoleTurn } from "./types";
import { useRunTrace } from "./useRunTrace";

export interface TrajectoryPanelProps {
  turn: ConsoleTurn | null;
  threadId: string | null;
  isSystemAdmin: boolean;
  /** 仅当前流式 live 轮传;其它 undefined。 */
  liveByStep?: ReadonlyMap<number, LiveStep>;
  /** 中栏「检查」传来的行 id(父级已换算成本轮的 rowId 或 null);变化且非
   *  null → 选中并滚到该行。 */
  focusRowId: string | null;
  onFireResult?: (r: FireNowResult) => void;
}

const ROOT_STYLE: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  height: "100%",
  minHeight: 0,
};
const BODY_STYLE: React.CSSProperties = { flex: 1, minHeight: 0 };

export function TrajectoryPanel(props: TrajectoryPanelProps): JSX.Element {
  const { turn, threadId, isSystemAdmin, liveByStep, focusRowId, onFireResult } = props;
  const { t } = useTranslation();

  const events = useMemo(() => turn?.turn.events ?? [], [turn?.turn.events]);
  const turnStatus = turn?.turn.status ?? "done";
  const turnInput = turn?.turn.input ?? "";
  const turnAttachments = turn?.turn.attachments;
  const turnInputs = turn?.turn.inputs;

  const summary = useMemo(() => summarizeTurn(events), [events]);
  const answer = useMemo(
    () => (summary.segments.length ? summary.segments.map((s) => s.text).join("\n\n") : null),
    [summary],
  );
  const baseRows = useMemo(
    () =>
      trajectoryRowsOf(
        events,
        {
          text: turnInput,
          attachmentNames: (turnAttachments ?? []).map((a) => a.name),
          inputs: turnInputs ?? {},
        },
        answer,
        turnStatus,
      ),
    [events, turnInput, turnAttachments, turnInputs, answer, turnStatus],
  );
  const rows = useMemo(
    () => [...baseRows, ...liveSyntheticRows(events, liveByStep)],
    [baseRows, events, liveByStep],
  );
  const banner = useMemo(() => timelineBannerModel(parseTimeline(events)), [events]);
  const toolSummary = useMemo(() => toolStatusSummary(events), [events]);

  const { trace, loading, refresh, traceId } = useRunTrace({
    threadId,
    runId: turn?.runId ?? null,
    enabled: true,
    turnStatus,
    wantTraceId: isSystemAdmin,
  });
  const matches = useMemo(() => matchTraceSpans(rows, trace), [rows, trace]);
  const langfuseUrl = isSystemAdmin ? buildLangfuseTraceUrl(traceId) : null;

  const [selectedRowId, setSelectedRowId] = useState<string | null>(null);
  useEffect(() => setSelectedRowId(null), [turn?.key]);
  useEffect(() => {
    if (focusRowId) setSelectedRowId(focusRowId);
  }, [focusRowId]);
  const selectedRow = rows.find((r) => r.id === selectedRowId) ?? null;

  if (turn === null) {
    return (
      <div data-testid="console-trajectory-panel" style={ROOT_STYLE}>
        <div data-testid="console-traj-empty">
          <Empty description={t("console.inspect_no_turn")} />
        </div>
      </div>
    );
  }

  const running = turn.turn.status === "running";

  return (
    <div data-testid="console-trajectory-panel" style={ROOT_STYLE}>
      <div
        data-testid="console-inspect-turn-header"
        style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 4px" }}
      >
        <span>
          {t("console.inspect_turn_header", {
            n: turn.seq + 1,
            status: t(`console.footer_status_${turn.turn.status}`),
          })}
        </span>
        {toolSummary.total > 0 && (
          <Tag bordered={false} style={{ margin: 0 }} data-testid="playground-tool-count">
            {t(
              toolSummary.total === 1 ? "playground.tool_count_one" : "playground.tool_count_other",
              { count: toolSummary.total },
            )}
          </Tag>
        )}
        {toolSummary.failed > 0 && (
          <Tag color="error" bordered={false} style={{ margin: 0 }} data-testid="playground-tool-failed">
            {t("playground.tool_failed_count", { count: toolSummary.failed })}
          </Tag>
        )}
        {langfuseUrl !== null && (
          <a
            data-testid="playground-turn-langfuse"
            href={langfuseUrl}
            target="_blank"
            rel="noreferrer"
          >
            {t("trace_toolbar.open_in_langfuse")}
          </a>
        )}
      </div>

      {banner !== null && (
        <RunStatusBanner
          status={banner.status}
          summary={t("playground.rb_ok")}
          errorLabel={
            banner.status === "error"
              ? banner.errorStepCount != null
                ? t("playground.tl_step", { n: banner.errorStepCount })
                : (banner.errorText ?? t("playground.rb_ok"))
              : undefined
          }
          errorMessage={
            banner.status === "error" && banner.errorStepCount != null
              ? (banner.errorText ?? undefined)
              : undefined
          }
          onJump={
            banner.status === "error"
              ? () => setSelectedRowId(rows.find((r) => r.status === "error")?.id ?? null)
              : undefined
          }
        />
      )}

      {/* PR-A.1 Task 5 占位接线 —— `mode` / hover 联动 / 拖选筛选的真正状态由
          Task 6 在本组件里接;这里先给必填 props 喂中性值,保证类型与既有行为
          (顺序泳道 + 点击选行)不变。 */}
      <LaneStrip
        events={events}
        rows={rows}
        running={running}
        mode="sequence"
        selectedRowId={selectedRowId}
        hoveredRowId={null}
        onHoverRow={() => {}}
        onSelectRow={setSelectedRowId}
        range={null}
        onRangeChange={() => {}}
        summaryOf={() => ""}
      />

      {selectedRow === null ? (
        <div style={BODY_STYLE}>
          <TrajectoryRows rows={rows} selectedRowId={selectedRowId} onSelectRow={setSelectedRowId} running={running} />
        </div>
      ) : (
        <Splitter layout="vertical" style={BODY_STYLE}>
          <Splitter.Panel defaultSize="55%" min="25%">
            <TrajectoryRows rows={rows} selectedRowId={selectedRowId} onSelectRow={setSelectedRowId} running={running} />
          </Splitter.Panel>
          <Splitter.Panel min="20%">
            <RowDetail
              row={selectedRow}
              turnSeq={turn.seq}
              events={events}
              match={matches.get(selectedRow.id) ?? { span: null, reason: "no_trace" }}
              trace={trace}
              traceLoading={loading}
              onRefreshTrace={refresh}
              onFireResult={onFireResult}
              onClose={() => setSelectedRowId(null)}
            />
          </Splitter.Panel>
        </Splitter>
      )}
    </div>
  );
}
