/**
 * TrajectoryView —— 调试台右栏的轨迹视图(spec §九):把工具条 / 概览时间轴 /
 * 会话级账本 / 详情侧栏装成一件,状态全在这里(`use_trajectory_state.ts`)。
 * 子组件一律纯展示,只往上回调。
 *
 * 装配上的几件硬事:
 *  - 传给账本的 `focusIndexes` 与传给时间轴的 `records` / `searchMatches` 都是
 *    同一份 memo 结果 —— 引用一变账本就抢视口、时间轴就重建块;
 *  - 账本按 `threadId` 加 `key` 强制重挂,换会话时「初始尾随到最新」重新生效;
 *  - `useRunTrace` 的 `runId` 跟着**详情那条记录所在轮**走(不是最新轮),
 *    span 配对也只在那一轮的记录里做。
 *
 * 投影模型 / 交互参照 deepseek-harness ui-trajectory(MIT)重写。见
 * .superpowers/sdd/2026-08-19-debug-console-pr-a2-trajectory/task-10-brief.md。
 */
import { useEffect, useMemo, useRef, useState, type JSX, type KeyboardEvent } from "react";
import { Empty } from "antd";
import { useTranslation } from "react-i18next";

import { matchTraceSpans, type SpanMatch } from "../../api/trace_match";
import type { FireNowResult } from "../../api/triggers";
import { buildLangfuseTraceUrl } from "../../config/env";
import type { LiveStep } from "../../pages/agent_detail/playground/useTokenStream";
import { DETAILS_DEFAULT_WIDTH } from "./DetailsFrame";
import { RecordDetails, type RecordTab } from "./RecordDetails";
import { RequestDetails, type RequestTab } from "./RequestDetails";
import { TrajectoryLedger } from "./TrajectoryLedger";
import { TrajectoryTimeline } from "./TrajectoryTimeline";
import { TrajectoryToolbar } from "./TrajectoryToolbar";
import type { ConsoleTurn } from "./types";
import { useRunTrace } from "./useRunTrace";
import { useTrajectoryState, type FocusRequest } from "./use_trajectory_state";
import "./trajectory_view.css";

export { TRAJECTORY_PAGE_TURNS } from "./use_ledger_window";
export { LANE_MODE_KEY } from "./use_trajectory_state";

const NO_TRACE: SpanMatch = { span: null, reason: "no_trace" };
/** `.ew-traj__split` 还没量到(挂载首帧 / jsdom)时的假定可用宽。 */
const FALLBACK_SPLIT_WIDTH = 1200;

export interface TrajectoryViewProps {
  turns: readonly ConsoleTurn[];
  threadId: string | null;
  streamTurnKey: string | null;
  liveByStep: ReadonlyMap<number, LiveStep>;
  running: boolean;
  /** 视图当前是否可见(调试台把它藏起来而不是卸载)。只用来在看不见时停掉
   *  「运行中每秒推一次『现在』」的定时器,不改任何显示语义;缺省 true。 */
  visible?: boolean;
  isSystemAdmin: boolean;
  /** 中栏「查看轨迹」/ 过程条「轨迹」:rowId null = 该轮最后一条 assistant;
   *  nonce 变才处理。 */
  focusRequest: FocusRequest | null;
  /** 让父级回放这些 run(未回放的);resolve 表示都结束了。 */
  onEnsureLoaded: (runIds: readonly string[]) => Promise<void>;
  onFireResult?: (r: FireNowResult) => void;
}

/** 详情侧栏的可用宽:`ResizeObserver` 量 `.ew-traj__split`,jsdom 没有它就退到
 *  window resize(global-constraints)。量不到(宽 0)一律按 1200 算。 */
function useSplitWidth(ref: React.RefObject<HTMLDivElement | null>, enabled: boolean): number {
  const [width, setWidth] = useState(FALLBACK_SPLIT_WIDTH);
  useEffect(() => {
    const el = ref.current;
    if (!enabled || el === null) return;
    const measure = (): void => setWidth(el.getBoundingClientRect().width || FALLBACK_SPLIT_WIDTH);
    measure();
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", measure);
      return () => window.removeEventListener("resize", measure);
    }
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref, enabled]);
  return width;
}

export function TrajectoryView(props: TrajectoryViewProps): JSX.Element {
  const { turns, threadId, isSystemAdmin, onFireResult } = props;
  const { t } = useTranslation();
  const splitRef = useRef<HTMLDivElement>(null);
  const [detailsWidth, setDetailsWidth] = useState(DETAILS_DEFAULT_WIDTH);
  const [recordTab, setRecordTab] = useState<RecordTab>("summary");
  const [requestTab, setRequestTab] = useState<RequestTab>("summary");

  const state = useTrajectoryState({
    turns,
    threadId,
    streamTurnKey: props.streamTurnKey,
    liveByStep: props.liveByStep,
    running: props.running,
    visible: props.visible ?? true,
    focusRequest: props.focusRequest,
    onEnsureLoaded: props.onEnsureLoaded,
  });
  const { ledger, timeline, detailRecord, selectedRecord, selectedRequest } = state;

  const empty = turns.length === 0;
  const splitWidth = useSplitWidth(splitRef, !empty);

  // trace 只为详情那条记录所在的**那一轮**拉:`runId` 跟着它走,span 配对也只在
  // 那一轮的记录里做(整本账混着几十轮,按轮配才对得上 nth-of-kind 的规则)。
  const { trace, loading: traceLoading, refresh: refreshTrace, traceId } = useRunTrace({
    threadId,
    runId: detailRecord?.runId ?? null,
    enabled: detailRecord !== null,
    turnStatus: state.detailTurnStatus,
    wantTraceId: isSystemAdmin,
  });
  const spanMatches = useMemo(
    () => matchTraceSpans(state.detailTurnRecords.map((r) => r.row), trace),
    [state.detailTurnRecords, trace],
  );
  const langfuseUrl = isSystemAdmin ? buildLangfuseTraceUrl(traceId) : null;

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (event.key !== "Escape") return;
    // 时间轴轨道自己也吃 Escape(清选区)并 `preventDefault` —— 焦点在轨道上时
    // 不让位,一次 Escape 会既清选区又关详情,读者按一下丢两样东西。
    if (event.defaultPrevented) return;
    event.preventDefault();
    state.onEscape();
  };

  if (empty) {
    return (
      <div className="ew-traj" data-testid="console-trajectory-panel">
        <div className="ew-traj__empty" data-testid="console-trajectory-empty">
          <Empty description={t("console.trajectory_empty")} />
        </div>
      </div>
    );
  }

  const details = ((): JSX.Element | null => {
    if (selectedRequest !== null && detailRecord !== null) {
      return (
        <RequestDetails
          request={selectedRequest}
          record={detailRecord}
          threadId={threadId}
          isSystemAdmin={isSystemAdmin}
          langfuseUrl={langfuseUrl}
          match={spanMatches.get(detailRecord.row.id) ?? NO_TRACE}
          trace={trace}
          traceLoading={traceLoading}
          onRefreshTrace={refreshTrace}
          onOpenRecord={state.selectRecord}
          activeTab={requestTab}
          onTabChange={setRequestTab}
          onClose={state.closeDetails}
          width={detailsWidth}
          onWidthChange={(w) => setDetailsWidth(w ?? DETAILS_DEFAULT_WIDTH)}
          splitWidth={splitWidth}
        />
      );
    }
    if (selectedRecord === null) return null;
    return (
      <RecordDetails
        record={selectedRecord}
        ownerRequest={ledger.requests.find((r) => r.no === selectedRecord.ownerRequestNo) ?? null}
        parent={ledger.records.find((r) => r.id === selectedRecord.parentId) ?? null}
        threadId={threadId}
        isSystemAdmin={isSystemAdmin}
        langfuseUrl={langfuseUrl}
        match={spanMatches.get(selectedRecord.row.id) ?? NO_TRACE}
        trace={trace}
        traceLoading={traceLoading}
        onRefreshTrace={refreshTrace}
        onOpenRecord={state.selectRecord}
        onOpenRequest={state.selectRequest}
        onFireResult={onFireResult}
        activeTab={recordTab}
        onTabChange={setRecordTab}
        onClose={state.closeDetails}
        width={detailsWidth}
        onWidthChange={(w) => setDetailsWidth(w ?? DETAILS_DEFAULT_WIDTH)}
        splitWidth={splitWidth}
      />
    );
  })();

  return (
    <div className="ew-traj" data-testid="console-trajectory-panel" onKeyDown={onKeyDown}>
      <TrajectoryToolbar
        mode={state.mode}
        onModeChange={state.changeMode}
        // 时长投影退化只可能来自「有记录没时序」,与 `ledger.timed` 同源;
        // duration 模式下把时间轴自己报的退化也算进来,两处口径不分家。
        degraded={!ledger.timed || (state.mode === "duration" && timeline?.degraded === true)}
        allTurnsCollapsed={state.allTurnsCollapsed}
        onToggleAllTurns={state.toggleAllTurns}
        turnsCollapsible={state.turnsCollapsible}
        allCallsCollapsed={state.allCallsCollapsed}
        onToggleAllCalls={state.toggleAllCalls}
        callsCollapsible={state.callsCollapsible}
        query={state.query}
        onQueryChange={state.setQuery}
        matchCount={state.matches?.size ?? null}
      />
      <TrajectoryTimeline
        model={timeline}
        records={ledger.records}
        range={state.range}
        onRangeChange={state.setRange}
        selectedIndex={state.selectedIndex}
        hoveredIndex={state.hoveredIndex}
        onHoverIndex={state.hoverIndex}
        onSelectRecord={state.selectRecordAt}
        onFocusRecord={state.focusRecordAt}
        searchMatches={state.matches}
        hasEarlier={state.hasEarlier}
        loadingEarlier={state.loadingEarlier}
        onLoadEarlier={state.loadEarlier}
      />
      <div className="ew-traj__split" ref={splitRef}>
        <TrajectoryLedger
          // 换会话 = 换一整本账:重挂让「初始尾随到最新」重新生效。
          key={threadId ?? "none"}
          rows={state.displayRows}
          requestsByRecordId={state.requestsByRecordId}
          selectedId={state.selectedId}
          onSelect={state.selectRecord}
          selectedRequestNo={state.selectedRequestNo}
          onSelectRequest={state.selectRequest}
          hoveredId={state.hoveredId}
          onHover={state.setHoveredId}
          focusIndexes={state.focus}
          activeTurnKey={state.activeTurnKey}
          onToggleTurn={state.toggleTurn}
          onToggleOwner={state.toggleOwner}
          running={props.running}
          hasEarlier={state.hasEarlier}
          earlierCount={state.earlierCount}
          loadingEarlier={state.loadingEarlier}
          onLoadEarlier={state.loadEarlier}
          scrollTo={state.scrollTo}
          loading={state.loading}
        />
        {details}
      </div>
    </div>
  );
}
