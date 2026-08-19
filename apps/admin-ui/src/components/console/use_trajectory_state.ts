/**
 * use_trajectory_state —— 轨迹视图(`TrajectoryView`)的唯一状态源:加载窗口、
 * 账本 / 时间轴 / 搜索 / 折叠的派生、选中(记录 ⟂ 请求)、时间轴选区、以及
 * 「中栏点了查看轨迹」的跨栏联动。**所有派生都过 `useMemo`** —— 运行中每秒
 * 一帧,少一层 memo 就是账本每帧重排、时间轴每帧重建、账本视口每帧被抢。
 *
 * 从 `TrajectoryView.tsx` 拆出来只为守住单文件 400 行上限;组件那边只剩装配。
 * 投影模型 / 交互参照 deepseek-harness ui-trajectory(MIT)重写,见 spec §九。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { SseEvent } from "../../api/sessions";
import type { LiveStep } from "../../pages/agent_detail/playground/useTokenStream";
import { buildLedger, lastKnownFrame, ledgerRecordId } from "./ledger";
import {
  collapsibleOwnerIds, collapsibleTurnKeys, displayRowsOf, type DisplayRow,
} from "./ledger_collapse";
import { searchLedger } from "./ledger_search";
import {
  deriveTimeline, focusIndexes, type TimelineMode, type TimelineModel, type TimeRange,
} from "./ledger_timeline";
import type { Ledger, LedgerRecord, LedgerRequest } from "./ledger_types";
import type { ConsoleTurn } from "./types";
import { useLedgerWindow } from "./use_ledger_window";

/** 时间轴投影记在本地,下次打开调试台还是上次那个视角(spec §八.6)。
 *  键名与旧 `TrajectoryPanel` 一致 —— 读者的旧偏好不该因为改版被清掉。 */
export const LANE_MODE_KEY = "expert_work.console.lane_mode";

/** 读不到 / 读不动(jsdom、隐私模式、SSR)都退回默认的顺序投影。 */
export function storedLaneMode(): TimelineMode {
  try {
    return window.localStorage.getItem(LANE_MODE_KEY) === "duration" ? "duration" : "sequence";
  } catch {
    return "sequence";
  }
}

const EMPTY_STRINGS: ReadonlySet<string> = new Set();
const EMPTY_EVENTS: readonly SseEvent[] = [];

export interface FocusRequest {
  turnKey: string;
  rowId: string | null;
  nonce: number;
}

export interface TrajectoryStateArgs {
  turns: readonly ConsoleTurn[];
  threadId: string | null;
  streamTurnKey: string | null;
  liveByStep: ReadonlyMap<number, LiveStep>;
  running: boolean;
  focusRequest: FocusRequest | null;
  onEnsureLoaded: (runIds: readonly string[]) => Promise<void>;
}

export interface TrajectoryState {
  mode: TimelineMode;
  changeMode: (m: TimelineMode) => void;
  ledger: Ledger;
  timeline: TimelineModel | null;
  displayRows: DisplayRow[];
  requestsByRecordId: ReadonlyMap<string, LedgerRequest>;
  matches: ReadonlySet<number> | null;
  focus: ReadonlySet<number> | null;
  range: TimeRange | null;
  setRange: (r: TimeRange | null) => void;
  query: string;
  setQuery: (q: string) => void;
  selectedRecord: LedgerRecord | null;
  selectedRequest: LedgerRequest | null;
  /** 详情面板落在哪条记录上(选中记录,或选中请求的 assistant 记录)。 */
  detailRecord: LedgerRecord | null;
  detailTurnStatus: "running" | "done" | "error";
  /** 详情那条记录所在轮的全部记录 —— `matchTraceSpans` 按轮配 span。 */
  detailTurnRecords: readonly LedgerRecord[];
  selectedId: string | null;
  selectedRequestNo: number | null;
  selectedIndex: number | null;
  hoveredId: string | null;
  hoveredIndex: number | null;
  hoverIndex: (i: number | null) => void;
  setHoveredId: (id: string | null) => void;
  selectRecord: (id: string) => void;
  selectRecordAt: (index: number) => void;
  selectRequest: (no: number) => void;
  /** 时间轴点空白:把最近的记录带进账本视口,不开详情。 */
  focusRecordAt: (index: number) => void;
  closeDetails: () => void;
  onEscape: () => void;
  activeTurnKey: string | null;
  allTurnsCollapsed: boolean;
  turnsCollapsible: boolean;
  toggleAllTurns: () => void;
  toggleTurn: (turnKey: string) => void;
  allCallsCollapsed: boolean;
  callsCollapsible: boolean;
  toggleAllCalls: () => void;
  toggleOwner: (ownerId: string) => void;
  hasEarlier: boolean;
  earlierCount: number;
  loadingEarlier: boolean;
  loadEarlier: () => void;
  scrollTo: { id: string; nonce: number } | null;
}

/** 集合增删都返回新集合(不可变;`displayRowsOf` 的 memo 靠引用变化重算)。 */
function toggled(set: ReadonlySet<string>, key: string): ReadonlySet<string> {
  const next = new Set(set);
  if (!next.delete(key)) next.add(key);
  return next;
}

export function useTrajectoryState(args: TrajectoryStateArgs): TrajectoryState {
  const { turns, threadId, streamTurnKey, liveByStep, running, focusRequest, onEnsureLoaded } = args;

  const [mode, setMode] = useState<TimelineMode>(storedLaneMode);
  const [range, setRange] = useState<TimeRange | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedRequestNo, setSelectedRequestNo] = useState<number | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [collapsedTurns, setCollapsedTurns] = useState<ReadonlySet<string>>(EMPTY_STRINGS);
  const [collapsedOwners, setCollapsedOwners] = useState<ReadonlySet<string>>(EMPTY_STRINGS);
  const [query, setQuery] = useState("");
  const [nowTick, setNowTick] = useState(0);
  const [scrollTo, setScrollTo] = useState<{ id: string; nonce: number } | null>(null);
  /** 已受理但还没落到具体记录上的跨栏跳转(等扩窗 / 等回放)。 */
  const [pendingFocus, setPendingFocus] = useState<{ turnKey: string; rowId: string | null } | null>(null);

  // 变量别叫 `window` —— 会把全局的 `window.localStorage` 遮掉。
  const { windowStart, windowTurns, expandTo, ensureTurnLoaded, ...paging } = useLedgerWindow({
    turns, threadId, onEnsureLoaded,
  });
  const scrollNonceRef = useRef(0);
  const handledNonceRef = useRef<number | null>(null);

  const requestScroll = useCallback((id: string): void => {
    scrollNonceRef.current += 1;
    setScrollTo({ id, nonce: scrollNonceRef.current });
  }, []);

  // 换会话 = 换一整本账:选中 / 选区 / 搜索 / 折叠 / 跨栏跳转全部复位
  //(窗口与回放记账由 `useLedgerWindow` 自己复位)。
  useEffect(() => {
    setSelectedId(null);
    setSelectedRequestNo(null);
    setHoveredId(null);
    setRange(null);
    setQuery("");
    setCollapsedTurns(EMPTY_STRINGS);
    setCollapsedOwners(EMPTY_STRINGS);
    setScrollTo(null);
    setPendingFocus(null);
    handledNonceRef.current = null;
  }, [threadId]);

  // 运行中每秒推一次「现在」,让尾块跟着长。
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setNowTick((n) => n + 1), 1000);
    return () => clearInterval(timer);
  }, [running]);

  const latestEvents = turns.length > 0 ? turns[turns.length - 1].turn.events : EMPTY_EVENTS;
  // 服务端时钟校准过的「现在」:客户端与服务端有时钟差,直接拿 `Date.now()`
  // 当轴末端会让运行中的尾块跳。**不运行时 `nowTick` 不动** —— 这个 memo 因此
  // 只在轮变化时重算一次,不会每秒把整本账重建一遍。
  const nowMs = useMemo(() => {
    const frame = lastKnownFrame(latestEvents);
    return frame === null ? Date.now() : frame.serverMs + (Date.now() - frame.receivedAtMs);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [latestEvents, nowTick]);

  // 账本一次构建就把 gantt 跑一轮(`absoluteSpans` 每轮一次),所以这层 memo 是
  // 性能命门,不是锦上添花。
  const ledger = useMemo(
    () => buildLedger({ turns: windowTurns, streamTurnKey, liveByStep, nowMs }),
    [windowTurns, streamTurnKey, liveByStep, nowMs],
  );
  const timeline = useMemo(() => deriveTimeline(ledger.records, mode), [ledger.records, mode]);
  const matches = useMemo(() => searchLedger(ledger.records, query), [ledger.records, query]);
  // 引用必须稳定:账本拿它决定要不要把段内首行拽进视口,每渲染换一个新 Set
  // 等于每帧抢一次读者的视口。
  const focus = useMemo(
    () => (timeline === null || range === null ? null : focusIndexes(timeline, range)),
    [timeline, range],
  );
  const displayRows = useMemo(
    () => displayRowsOf(ledger, { collapsedTurns, collapsedOwners, matches }),
    [ledger, collapsedTurns, collapsedOwners, matches],
  );
  const requestsByRecordId = useMemo(
    () => new Map(ledger.requests.map((r) => [r.recordId, r])),
    [ledger.requests],
  );
  const turnKeys = useMemo(() => collapsibleTurnKeys(ledger), [ledger]);
  const ownerIds = useMemo(() => collapsibleOwnerIds(ledger), [ledger]);

  // 换加载窗口 / 换投影 = 换一套坐标,旧选区指的已经不是同一批记录了。
  useEffect(() => {
    setRange(null);
  }, [windowStart, mode]);
  // 域本身变了(记录增减)而旧选区整段落在域外 → 清掉(与时间轴内部把视口退回
  // 全景用的是同一条判据)。
  useEffect(() => {
    if (range === null) return;
    if (timeline === null || range.end < timeline.start || range.start > timeline.end) setRange(null);
  }, [timeline, range]);

  // 中栏「查看轨迹」:同一 nonce 只处理一次。目标轮不在窗口里先扩窗,该轮还没
  // 回放先请父级回放 —— 两件事都要等下一次渲染才见效,所以这里只「挂号」,
  // 真正落到哪条记录交给下面那条 effect。
  useEffect(() => {
    if (focusRequest === null || handledNonceRef.current === focusRequest.nonce) return;
    handledNonceRef.current = focusRequest.nonce;
    const at = turns.findIndex((turn) => turn.key === focusRequest.turnKey);
    if (at === -1) return;
    expandTo(at);
    ensureTurnLoaded(turns[at]);
    setPendingFocus({ turnKey: focusRequest.turnKey, rowId: focusRequest.rowId });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusRequest]);

  useEffect(() => {
    if (pendingFocus === null) return;
    const turnRecords = ledger.records.filter((r) => r.turnKey === pendingFocus.turnKey);
    // 窗口还没扩到目标轮 → 等下一次渲染。
    if (turnRecords.length === 0) return;
    // 还在回放(只有占位记录)→ 等真记录落地,否则会停在占位行上。
    if (turnRecords.some((r) => r.placeholder === "loading")) return;
    const wanted = pendingFocus.rowId === null
      ? null
      : ledgerRecordId(pendingFocus.turnKey, pendingFocus.rowId);
    const target = (wanted === null ? undefined : turnRecords.find((r) => r.id === wanted))
      // rowId 为 null(或那条行在账本里没有对应记录)→ 该轮最后一条 assistant。
      ?? [...turnRecords].reverse().find((r) => r.kind === "assistant")
      ?? turnRecords[0];
    setPendingFocus(null);
    setSelectedId(target.id);
    setSelectedRequestNo(null);
    setRange(null);
    requestScroll(target.id);
  }, [pendingFocus, ledger.records, requestScroll]);

  const selectedRecord = useMemo(
    () => ledger.records.find((r) => r.id === selectedId) ?? null,
    [ledger.records, selectedId],
  );
  const selectedRequest = useMemo(
    () => ledger.requests.find((r) => r.no === selectedRequestNo) ?? null,
    [ledger.requests, selectedRequestNo],
  );
  const requestRecord = useMemo(
    () => (selectedRequest === null
      ? null
      : ledger.records.find((r) => r.id === selectedRequest.recordId) ?? null),
    [ledger.records, selectedRequest],
  );
  const detailRecord = selectedRecord ?? requestRecord;
  const detailTurnStatus = useMemo(
    () => ledger.turns.find((t) => t.key === detailRecord?.turnKey)?.status ?? "done",
    [ledger.turns, detailRecord],
  );
  const detailTurnRecords = useMemo(
    () => (detailRecord === null
      ? []
      : ledger.records.filter((r) => r.turnKey === detailRecord.turnKey)),
    [ledger.records, detailRecord],
  );
  const hoveredIndex = useMemo(
    () => ledger.records.find((r) => r.id === hoveredId)?.index ?? null,
    [ledger.records, hoveredId],
  );

  const selectRecord = useCallback((id: string): void => {
    setSelectedId(id);
    setSelectedRequestNo(null);
    // deepseek 同款:点到选区之外的一行 = 读者离开了这段选区。
    setRange((cur) => {
      if (cur === null) return cur;
      const record = ledger.records.find((r) => r.id === id);
      return record !== undefined && focus !== null && !focus.has(record.index) ? null : cur;
    });
  }, [ledger.records, focus]);

  const selectRecordAt = useCallback((index: number): void => {
    const record = ledger.records.find((r) => r.index === index);
    if (record !== undefined) selectRecord(record.id);
  }, [ledger.records, selectRecord]);

  const focusRecordAt = useCallback((index: number): void => {
    const record = ledger.records.find((r) => r.index === index);
    if (record !== undefined) requestScroll(record.id);
  }, [ledger.records, requestScroll]);

  const hoverIndex = useCallback((index: number | null): void => {
    setHoveredId(index === null
      ? null
      : ledger.records.find((r) => r.index === index)?.id ?? null);
  }, [ledger.records]);

  const selectRequest = useCallback((no: number): void => {
    setSelectedRequestNo(no);
    setSelectedId(null);
  }, []);

  const closeDetails = useCallback((): void => {
    setSelectedId(null);
    setSelectedRequestNo(null);
  }, []);

  const hasDetails = detailRecord !== null;
  const onEscape = useCallback((): void => {
    if (hasDetails) closeDetails();
    else setRange(null);
  }, [hasDetails, closeDetails]);

  const changeMode = useCallback((next: TimelineMode): void => {
    setMode(next);
    try {
      window.localStorage.setItem(LANE_MODE_KEY, next);
    } catch {
      // 存不进去(隐私模式 / 配额满)不该拖垮切换本身,本次会话内照样生效。
    }
  }, []);

  const allTurnsCollapsed = turnKeys.length > 0 && turnKeys.every((k) => collapsedTurns.has(k));
  const allCallsCollapsed = ownerIds.length > 0 && ownerIds.every((id) => collapsedOwners.has(id));
  const toggleAllTurns = useCallback(
    () => setCollapsedTurns(allTurnsCollapsed ? EMPTY_STRINGS : new Set(turnKeys)),
    [allTurnsCollapsed, turnKeys],
  );
  const toggleAllCalls = useCallback(
    () => setCollapsedOwners(allCallsCollapsed ? EMPTY_STRINGS : new Set(ownerIds)),
    [allCallsCollapsed, ownerIds],
  );
  const toggleTurn = useCallback(
    (turnKey: string) => setCollapsedTurns((cur) => toggled(cur, turnKey)),
    [],
  );
  const toggleOwner = useCallback(
    (ownerId: string) => setCollapsedOwners((cur) => toggled(cur, ownerId)),
    [],
  );

  return {
    mode, changeMode,
    ledger, timeline, displayRows, requestsByRecordId, matches, focus,
    range, setRange, query, setQuery,
    selectedRecord, selectedRequest, detailRecord, detailTurnStatus, detailTurnRecords,
    selectedId, selectedRequestNo,
    selectedIndex: detailRecord?.index ?? null,
    hoveredId, hoveredIndex, hoverIndex, setHoveredId,
    selectRecord, selectRecordAt, selectRequest, focusRecordAt, closeDetails, onEscape,
    activeTurnKey: detailRecord?.turnKey ?? ledger.turns.at(-1)?.key ?? null,
    allTurnsCollapsed,
    turnsCollapsible: turnKeys.length > 0,
    toggleAllTurns,
    toggleTurn,
    allCallsCollapsed,
    callsCollapsible: ownerIds.length > 0,
    toggleAllCalls,
    toggleOwner,
    hasEarlier: paging.hasEarlier,
    earlierCount: paging.earlierCount,
    loadingEarlier: paging.loadingEarlier,
    loadEarlier: paging.loadEarlier,
    scrollTo,
  };
}
