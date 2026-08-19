/**
 * use_ledger_window —— 轨迹账本的**加载窗口**(spec §九「窗口」):整个会话的
 * 轮只有尾部一页真正进账本,再往前靠「加载更早」一页一页拉;窗口里还没回放的
 * 历史轮由这里去请父级回放(同一个 run 只请一次)。
 *
 * 窗口只有起点没有终点 —— 运行中长出来的新轮永远在窗口里,`turns.length` 变化
 * **不动窗口**,否则读者刚展开的历史会被下一轮收回去。
 *
 * 从 `use_trajectory_state.ts` 拆出来是为了守住单文件 400 行的上限。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ConsoleTurn } from "./types";

/** 一页加载窗口装多少轮(「加载更早」一次也放这么多)。 */
export const TRAJECTORY_PAGE_TURNS = 20;

export interface LedgerWindow {
  windowStart: number;
  windowTurns: readonly ConsoleTurn[];
  hasEarlier: boolean;
  earlierCount: number;
  /** 窗口内还有历史轮在回放(首批自动回放也算)。 */
  loading: boolean;
  /** 只管「加载更早」那个按钮。 */
  loadingEarlier: boolean;
  loadEarlier: () => void;
  /** 把窗口起点降到 `at`(已经更早就不动)—— 跨栏跳转指向窗口外的轮时用。 */
  expandTo: (at: number) => void;
  /** 该轮还没回放就请父级回放一次(去重;进行中也算已请)。 */
  ensureTurnLoaded: (turn: ConsoleTurn) => void;
}

export function useLedgerWindow(args: {
  turns: readonly ConsoleTurn[];
  threadId: string | null;
  onEnsureLoaded: (runIds: readonly string[]) => Promise<void>;
}): LedgerWindow {
  const { turns, threadId, onEnsureLoaded } = args;

  const [windowStart, setWindowStart] = useState(() =>
    Math.max(0, turns.length - TRAJECTORY_PAGE_TURNS));
  const [loadingEarlier, setLoadingEarlier] = useState(false);

  // 起点夹到 `[0, turns.length - 20]`:同一会话里历史被重载(轮变少)、或换会话
  // 的过渡帧上,旧起点会大过新的轮数,不夹就把窗口滑成空的。
  const maxStart = Math.max(0, turns.length - TRAJECTORY_PAGE_TURNS);
  const start = Math.min(Math.max(windowStart, 0), maxStart);

  const turnsRef = useRef(turns);
  turnsRef.current = turns;
  const windowStartRef = useRef(start);
  windowStartRef.current = start;
  /** 已经发过回放请求的 runId —— 同一个不重复发。 */
  const requestedRef = useRef<Set<string>>(new Set());
  /** 本会话的窗口是否已经对齐过尾部(turns 异步到货时才需要补一次)。 */
  const anchoredRef = useRef(turns.length > 0);

  // 换会话 = 换一整套轮:窗口回到尾页,回放记账清空。
  useEffect(() => {
    const length = turnsRef.current.length;
    setWindowStart(Math.max(0, length - TRAJECTORY_PAGE_TURNS));
    setLoadingEarlier(false);
    requestedRef.current = new Set();
    anchoredRef.current = length > 0;
  }, [threadId]);

  // 首批轮异步到货时把窗口对到尾部;之后 `turns.length` 再变都不动窗口。
  useEffect(() => {
    if (anchoredRef.current || turns.length === 0) return;
    anchoredRef.current = true;
    setWindowStart(Math.max(0, turns.length - TRAJECTORY_PAGE_TURNS));
  }, [turns.length]);

  const windowTurns = useMemo(() => turns.slice(start), [turns, start]);
  const loading = useMemo(
    () => windowTurns.some((t) => t.loadState === "pending" || t.loadState === "loading"),
    [windowTurns],
  );

  // 窗口内还没回放的历史轮 → 请父级回放(去重,进行中不重复发)。
  useEffect(() => {
    const ids: string[] = [];
    for (const turn of windowTurns) {
      if (turn.loadState !== "pending" || turn.runId === null) continue;
      if (requestedRef.current.has(turn.runId)) continue;
      requestedRef.current.add(turn.runId);
      ids.push(turn.runId);
    }
    if (ids.length > 0) void onEnsureLoaded(ids);
  }, [windowTurns, onEnsureLoaded]);

  const loadEarlier = useCallback((): void => {
    const start = windowStartRef.current;
    if (loadingEarlier || start === 0) return;
    const nextStart = Math.max(0, start - TRAJECTORY_PAGE_TURNS);
    setWindowStart(nextStart);
    const ids = turnsRef.current
      .slice(nextStart, start)
      .map((turn) => turn.runId)
      .filter((id): id is string => id !== null && !requestedRef.current.has(id));
    if (ids.length === 0) return;
    // 先记账再发:上面那条「窗口内 pending」的 effect 随后会跑一遍新窗口,
    // 记过账它就不会把同一批再发一次。
    for (const id of ids) requestedRef.current.add(id);
    setLoadingEarlier(true);
    void onEnsureLoaded(ids).finally(() => setLoadingEarlier(false));
  }, [loadingEarlier, onEnsureLoaded]);

  const expandTo = useCallback((at: number): void => {
    if (at < windowStartRef.current) setWindowStart(at);
  }, []);

  const ensureTurnLoaded = useCallback((turn: ConsoleTurn): void => {
    if (turn.loadState !== "pending" || turn.runId === null) return;
    if (requestedRef.current.has(turn.runId)) return;
    requestedRef.current.add(turn.runId);
    void onEnsureLoaded([turn.runId]);
  }, [onEnsureLoaded]);

  return {
    windowStart: start,
    windowTurns,
    hasEarlier: start > 0,
    earlierCount: start,
    loading,
    loadingEarlier,
    loadEarlier,
    expandTo,
    ensureTurnLoaded,
  };
}
