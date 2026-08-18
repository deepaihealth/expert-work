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
 *
 * PR-A.1 Task 6(spec §八.6 / §八.8):头部收成一行(轮次 · 状态 · 工具数 ·
 * 总耗时 · Run 详情 · Langfuse · 顺序/时长),并在这里持有泳道与行表共用的
 * hover / 拖选筛选状态。见
 * .superpowers/sdd/2026-08-18-debug-console-pr-a1-feedback/task-6-brief.md。
 */
import { useEffect, useMemo, useState, type JSX, type KeyboardEvent } from "react";
import { Empty, Segmented, Splitter, Tag } from "antd";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { parseTimeline } from "../../api/timeline";
import { toolStatusSummary } from "../../api/tool_timeline";
import { matchTraceSpans } from "../../api/trace_match";
import { trajectoryRowsOf } from "../../api/trajectory_rows";
import type { FireNowResult } from "../../api/triggers";
import { summarizeTurn } from "../../api/turn_summary";
import { buildLangfuseTraceUrl } from "../../config/env";
import { fmtDuration } from "../../pages/agent_detail/playground/duration_format";
import { RunStatusBanner } from "../../pages/agent_detail/playground/RunStatusBanner";
import { timelineBannerModel } from "../../pages/agent_detail/playground/timeline_banner";
import type { LiveStep } from "../../pages/agent_detail/playground/useTokenStream";
import { LaneStrip } from "./LaneStrip";
import type { LaneMode } from "./lane_strip_model";
import { liveSyntheticRows } from "./live_rows";
import { RowDetail } from "./RowDetail";
import { rowSummary, TrajectoryRows } from "./TrajectoryRows";
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
const HEADER_STYLE: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  padding: "6px 4px",
};

/** 泳道投影记在本地,下次打开调试台还是上次那个视角(spec §八.6)。 */
const LANE_MODE_KEY = "expert_work.console.lane_mode";

/** 读不到 / 读不动(jsdom、隐私模式、SSR)都退回默认的顺序投影。 */
function storedLaneMode(): LaneMode {
  try {
    return window.localStorage.getItem(LANE_MODE_KEY) === "duration" ? "duration" : "sequence";
  } catch {
    return "sequence";
  }
}

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
  const [hoveredRowId, setHoveredRowId] = useState<string | null>(null);
  const [range, setRange] = useState<{ from: number; to: number } | null>(null);
  const [laneMode, setLaneMode] = useState<LaneMode>(storedLaneMode);
  // 换轮 = 换一整套行:选中 / 悬停 / 筛选三个都得清,否则上一轮的行序号会
  // 把新一轮的表筛成空的。
  useEffect(() => {
    setSelectedRowId(null);
    setHoveredRowId(null);
    setRange(null);
  }, [turn?.key]);
  useEffect(() => {
    if (focusRowId) setSelectedRowId(focusRowId);
  }, [focusRowId]);
  const selectedRow = rows.find((r) => r.id === selectedRowId) ?? null;

  const changeLaneMode = (next: LaneMode): void => {
    setLaneMode(next);
    try {
      window.localStorage.setItem(LANE_MODE_KEY, next);
    } catch {
      // 存不进去(隐私模式 / 配额满)不该拖垮切换本身,本次会话内照样生效。
    }
  };
  const handleKeyDown = (e: KeyboardEvent<HTMLDivElement>): void => {
    if (e.key !== "Escape") return;
    setSelectedRowId(null);
  };

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
    <div data-testid="console-trajectory-panel" style={ROOT_STYLE} onKeyDown={handleKeyDown}>
      <div data-testid="console-inspect-turn-header" style={HEADER_STYLE}>
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
        {summary.latencyMs !== null && (
          <span data-testid="console-inspect-duration" style={{ color: "var(--ew-text-secondary)" }}>
            {fmtDuration(summary.latencyMs)}
          </span>
        )}
        {threadId !== null && turn.runId !== null && (
          <Link data-testid="console-inspect-run-link" to={`/runs/${threadId}/${turn.runId}`}>
            {t("console.inspect_run_detail")} ↗
          </Link>
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
        <Segmented
          size="small"
          value={laneMode}
          onChange={(value) => changeLaneMode(value as LaneMode)}
          data-testid="console-lane-mode"
          style={{ marginLeft: "auto" }}
          options={[
            {
              value: "sequence",
              label: <span data-testid="console-lane-mode-sequence">{t("console.lane_mode_sequence")}</span>,
            },
            {
              value: "duration",
              label: <span data-testid="console-lane-mode-duration">{t("console.lane_mode_duration")}</span>,
            },
          ]}
        />
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
              ? () =>
                  setSelectedRowId(
                    // think 行的 error 是**继承**自本步失败的工具(见 Task 3
                    // 裁决),而且排在工具行之前 —— 直接 find 第一条 error 会
                    // 永远停在 think 上,读者点「跳转」是想看那个炸掉的工具。
                    // 找不到非 think 的错误行(例如顶层 error 帧)才退回原判据。
                    (rows.find((r) => r.kind !== "think" && r.status === "error") ??
                      rows.find((r) => r.status === "error"))?.id ?? null,
                  )
              : undefined
          }
        />
      )}

      <LaneStrip
        events={events}
        rows={rows}
        running={running}
        mode={laneMode}
        selectedRowId={selectedRowId}
        hoveredRowId={hoveredRowId}
        onHoverRow={setHoveredRowId}
        onSelectRow={setSelectedRowId}
        range={range}
        onRangeChange={setRange}
        summaryOf={(row) => rowSummary(row, t)}
      />

      {selectedRow === null ? (
        <div style={BODY_STYLE}>
          <TrajectoryRows
            rows={rows}
            selectedRowId={selectedRowId}
            hoveredRowId={hoveredRowId}
            onHoverRow={setHoveredRowId}
            onSelectRow={setSelectedRowId}
            running={running}
            range={range}
            onClearRange={() => setRange(null)}
          />
        </div>
      ) : (
        <Splitter layout="vertical" style={BODY_STYLE}>
          <Splitter.Panel defaultSize="55%" min="25%">
            <TrajectoryRows
              rows={rows}
              selectedRowId={selectedRowId}
              hoveredRowId={hoveredRowId}
              onHoverRow={setHoveredRowId}
              onSelectRow={setSelectedRowId}
              running={running}
              range={range}
              onClearRange={() => setRange(null)}
            />
          </Splitter.Panel>
          <Splitter.Panel min="20%">
            <RowDetail
              row={selectedRow}
              rowIndex={rows.findIndex((r) => r.id === selectedRow.id) + 1}
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
