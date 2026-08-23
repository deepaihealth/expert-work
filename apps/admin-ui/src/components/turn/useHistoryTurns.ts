/**
 * 历史懒重建 (#980) — a resumed thread's prior conversation, rebuilt as
 * count-paired lazy ``TurnCard``s whose persisted event streams replay only
 * when the row scrolls into view.
 *
 * Lifted verbatim out of ``pages/agent_detail/PlaygroundTab.tsx`` so the
 * conversation detail page can drive the same read-only transcript. The four
 * degradation levels and the stale-request guards are unchanged:
 *
 *   1. ``listThreadRuns`` failed / count mismatch → ``turns`` stays ``null``
 *      and the caller renders the flat ``messages`` block.
 *   2. ``getSessionMessages`` failed → both cleared (nothing to show).
 *   3. A row's replay failed / delivered nothing → that turn's load state is
 *      ``error`` and its ``TurnCard`` keeps the flat ``fallbackLines``.
 *   4. A newer ``load()`` superseded this one → the stale result is dropped.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import {
  listThreadRuns,
  NON_TERMINAL_RUN_STATUSES,
  streamRunEvents,
  type RunTokens,
} from "../../api/runs";
import {
  getSessionMessages,
  type HistoryMessage,
  type SseEvent,
} from "../../api/sessions";
import { buildHistoryTurns } from "../../pages/agent_detail/playground/history_turns";
import { concreteTenantScope, useTenantScope } from "../../tenant/TenantScopeContext";
import type { HistoryLoad, HistoryTurn } from "./types";

export interface UseHistoryTurns {
  /** Flat ``/messages`` text — the always-available degradation payload. */
  messages: HistoryMessage[];
  /** Count-paired historical turns (``null`` = not built / count mismatch →
   *  the caller falls back to the flat ``messages`` block). */
  turns: HistoryTurn[] | null;
  /** Each turn's lazy replay state, keyed by runId. */
  loads: Record<string, HistoryLoad>;
  /** Curried ``ref`` callback for a history row — registers it with the shared
   *  IntersectionObserver so scrolling it into view replays its run. */
  registerRow: (runId: string, threadId: string) => (el: HTMLElement | null) => void;
  /** 主动回放一批 run(轨迹视图按页用):跳过已开始的,最多 4 路并发,全部
   *  结束(成败都算)才 resolve;不抛。 */
  loadRuns: (runIds: readonly string[], threadId: string) => Promise<void>;
  /** Fetch + pair a thread's history. Never rejects (degrades internally).
   *  ``tenantId`` is the thread's own tenant (authoritative — correct even
   *  from the "*" aggregate); omitted, the ambient tenant scope applies. */
  load: (threadId: string, tenantId?: string) => Promise<void>;
  /** Clear the rebuilt history and tear down the observer (new draft / agent
   *  switch). Deliberately does NOT abort the in-flight fetch — same as the
   *  original ``resetDraft``. */
  reset: () => void;
  /** D-5 — patch already-built turns' status/tokens from a fresh run-list
   *  read WITHOUT re-pairing (re-pairing a thread whose paused run's
   *  continuation has finished would flatten the whole page — ROADMAP D-7).
   *  Identity-stable: returns the same ``turns`` array when nothing changed. */
  patchRuns: (
    runs: readonly { runId: string; status: string; tokens: RunTokens | null }[],
  ) => void;
}

export interface UseHistoryTurnsOptions {
  /** D-5 — called once when a live-attached (non-terminal) run's stream
   *  delivers its ``end`` frame, i.e. the run just reached a terminal
   *  status. The conversation page uses it to refresh run statuses /
   *  the summary header without re-pairing. */
  onRunTerminal?: (runId: string) => void;
}

/** I-5 — delay before re-attaching a dropped live stream. */
const LIVE_RETRY_DELAY_MS = 5000;

export function useHistoryTurns(options: UseHistoryTurnsOptions = {}): UseHistoryTurns {
  // Track C W2 — 切入态读透传:replay 的事件流要带 ?tenant_id=
  // (组件内直取,不穿 prop)。
  const { apiTenantScope } = useTenantScope();
  // #6 — prior conversation loaded when resuming an existing thread.
  const [history, setHistory] = useState<HistoryMessage[]>([]);
  // 历史懒重建 — count-paired historical turns (null = not built / count
  // mismatch, caller falls back to the flat ``history`` block above) + each
  // turn's lazy replay state, keyed by runId.
  const [historyTurns, setHistoryTurns] = useState<HistoryTurn[] | null>(null);
  const [historyLoads, setHistoryLoads] = useState<Record<string, HistoryLoad>>(
    {},
  );

  // 历史懒重建 — aborts in-flight history fetch/replays when resuming to
  // another thread. ``observerRef``/``runIdByEl`` back the shared
  // IntersectionObserver that triggers each history row's replay when it
  // scrolls into view; torn down explicitly by ``reset``/``load``
  // (and on unmount) since its callback closure is bound to one thread.
  const historyAbortRef = useRef<AbortController | null>(null);
  const observerRef = useRef<IntersectionObserver | null>(null);
  const runIdByEl = useRef<Map<Element, string>>(new Map());
  // A row's ref callback is a fresh closure every render (curried by
  // ``registerRow``), so React re-invokes it — and ``registerRow``
  // re-``observe()``s — on every re-render of that row, not just when it first
  // mounts. Tracking "already started" runIds here (checked synchronously,
  // unlike state) makes ``replayHistoryRun`` a true one-shot regardless of how
  // many times its trigger fires.
  const startedHistoryRunsRef = useRef<Set<string>>(new Set());
  // D-5 — each run's status at pairing time, so ``replayHistoryRun`` knows
  // whether to collect-then-commit (terminal) or live-flush (non-terminal).
  const runStatusRef = useRef<Map<string, string>>(new Map());
  // Latest ``onRunTerminal`` without re-creating the replay callback.
  const onRunTerminalRef = useRef<UseHistoryTurnsOptions["onRunTerminal"]>(undefined);
  onRunTerminalRef.current = options.onRunTerminal;

  const reset = useCallback(() => {
    setHistory([]);
    setHistoryTurns(null);
    setHistoryLoads({});
    // A stale observer's callback closure is bound to the previous thread's
    // id (see ``registerRow``) — tear it down so a later resume
    // rebuilds it fresh, and so no leftover row keeps replaying here.
    observerRef.current?.disconnect();
    observerRef.current = null;
    runIdByEl.current.clear();
    startedHistoryRunsRef.current.clear();
  }, []);

  // The tenant the current rebuild reads under — pinned at ``load`` time so
  // the fetch and every lazy replay it spawns hit the same tenant even if
  // the ambient scope switches mid-rebuild.
  const tenantIdRef = useRef<string | undefined>(undefined);

  // 历史重建:并行拉文本轮 + runs,按序配对成懒重建描述符;
  // 计数守卫失败或任一失败 → 落回扁平文本(setHistory 保留原行为)。
  const load = useCallback(async (threadId: string, tenantId?: string) => {
    setHistory([]);
    setHistoryTurns(null);
    setHistoryLoads({});
    historyAbortRef.current?.abort();
    const ac = new AbortController();
    historyAbortRef.current = ac;
    const effectiveTenant = tenantId ?? concreteTenantScope(apiTenantScope);
    tenantIdRef.current = effectiveTenant;
    // Tear down the previous thread's observer/registry — its callback
    // closure is bound to that thread id (see ``registerRow``), and
    // any in-flight replay's runId must not block this thread's own runs
    // (run ids are otherwise assumed globally unique, but this keeps the
    // one-shot guard scoped to the thread actually being viewed).
    observerRef.current?.disconnect();
    observerRef.current = null;
    runIdByEl.current.clear();
    startedHistoryRunsRef.current.clear();
    await Promise.all([
      getSessionMessages(threadId, effectiveTenant),
      listThreadRuns(threadId, effectiveTenant).catch(() => null),
    ])
      .then(([messages, runs]) => {
        // A newer resume replaced ``historyAbortRef.current`` before this
        // fetch resolved — drop the stale write so it can't clobber the
        // current thread's history (or trigger a wrong-thread replay).
        if (historyAbortRef.current !== ac) return;
        setHistory(messages); // 降级路径永远有数据
        if (!runs) return;
        const built = buildHistoryTurns(messages, runs);
        // 计数守卫失败(null)或空 thread(0 轮)→ 保持扁平文本(null 分支
        // 什么都不渲,不会漏出「以下为本次新消息」分隔线)。
        if (!built || built.length === 0) return;
        runStatusRef.current = new Map(built.map((h) => [h.runId, h.status]));
        setHistoryTurns(built);
        setHistoryLoads(
          Object.fromEntries(
            built.map((h) => [h.runId, { state: "pending", events: [] }]),
          ),
        );
      })
      .catch(() => {
        // Same staleness guard — a stale failed resume must not wipe the
        // history a newer resume already loaded.
        if (historyAbortRef.current !== ac) return;
        setHistory([]);
        setHistoryTurns(null);
      });
  }, [apiTenantScope]);

  // 历史懒重建 — replay one historical run's persisted event stream so its
  // TurnCard can render the full debug panels (rather than just the flat
  // fallback answer). Fire-and-forget from the IntersectionObserver callback
  // below; a replay failure leaves the turn on its fallback (degradation).
  // ``startedHistoryRunsRef`` makes this idempotent — a row's viewport
  // trigger can fire more than once (see ``registerRow``).
  // I-5 (终审) — a live attach that drops (idle proxy cut, network blip)
  // must re-attach, or the turn card sits on "running" forever. The retry
  // clears the one-shot guard and re-enters ``replayHistoryRun`` (via ref —
  // the two callbacks are mutually recursive) unless the thread was切走
  // (abort) or the run has since been patched terminal.
  const replayRef = useRef<((runId: string, threadId: string) => Promise<void>) | null>(null);
  const scheduleLiveRetry = useCallback((runId: string, threadId: string) => {
    const signal = historyAbortRef.current?.signal;
    setTimeout(() => {
      if (signal?.aborted) return;
      if (!NON_TERMINAL_RUN_STATUSES.has(runStatusRef.current.get(runId) ?? "")) return;
      startedHistoryRunsRef.current.delete(runId);
      void replayRef.current?.(runId, threadId);
    }, LIVE_RETRY_DELAY_MS);
  }, []);

  const replayHistoryRun = useCallback(async (runId: string, threadId: string) => {
    if (startedHistoryRunsRef.current.has(runId)) return;
    startedHistoryRunsRef.current.add(runId);
    setHistoryLoads((prev) => ({
      ...prev,
      [runId]: { state: "loading", events: [] },
    }));
    // D-5 — a non-terminal run's attach is LIVE: frames flush incrementally
    // (rAF-batched) instead of waiting for ``end``. Note the backend treats
    // PAUSED as replay-able (PAUSED ∈ TERMINAL_RUN_STATUSES server-side), so
    // a paused run's attach replays its stored frames, gets ``end`` and
    // commits — the live path here mainly serves running/pending/queued.
    const isLive = NON_TERMINAL_RUN_STATUSES.has(runStatusRef.current.get(runId) ?? "");
    const collected: SseEvent[] = [];
    try {
      let flushScheduled = false;
      // A flush scheduled just before the ``end`` frame would otherwise fire
      // AFTER the final commit and stamp the turn back to "live".
      let committed = false;
      const flushLive = () => {
        flushScheduled = false;
        if (committed) return;
        const snapshot = collected.slice();
        setHistoryLoads((prev) => ({
          ...prev,
          [runId]: { state: "live", events: snapshot },
        }));
      };
      const scheduleFlush = () => {
        if (flushScheduled) return;
        flushScheduled = true;
        if (typeof requestAnimationFrame === "function") requestAnimationFrame(flushLive);
        else setTimeout(flushLive, 16);
      };
      for await (const frame of streamRunEvents(threadId, runId, {
        signal: historyAbortRef.current?.signal,
        // Pinned by ``load`` — a replay must read the same tenant its
        // rebuild was fetched under, not whatever the scope switched to.
        tenantScope: tenantIdRef.current,
      })) {
        collected.push(frame);
        if (frame.event === "end") break;
        if (isLive) scheduleFlush();
      }
      // The terminal-replay endpoint appends an ``end`` frame after the stored
      // rows (``_run_event_stream.py`` ``_stream_replay``), so an empty run
      // replays as a lone ``[{event:"end"}]``. 一页装不下的长 run 以 ``truncated``
      // 收尾而不发 ``end``,``streamRunEvents`` 会自己带 ``next_seq`` 翻页续拉,
      // 所以到这里时"看得到 ``end``"依旧等价于"流走完了"—— 除非翻页也走不动了
      // (那时 SDK 把 ``truncated`` 吐出来,这里照旧降级)。
      // Only mark ``done`` when the replay actually
      // delivered renderable content AND completed; an empty replay
      // (no non-end frame) or a truncated one (no end frame) degrades to
      // ``error`` — which keeps the placeholder showing ``fallbackLines``
      // (the /messages text we already have) instead of the full render's
      // "no text" empty state. 不丢已有内容。
      committed = true;
      const sawEnd = collected.some((f) => f.event === "end");
      const hasContent = collected.some((f) => f.event !== "end");
      if (isLive) {
        // Live attach: ``end`` means the run just went terminal — commit and
        // tell the page. A close WITHOUT ``end`` (idle drop) keeps whatever
        // streamed so far, still marked live, and schedules a re-attach.
        setHistoryLoads((prev) => ({
          ...prev,
          [runId]: sawEnd
            ? { state: "done", events: collected }
            : { state: "live", events: collected },
        }));
        if (sawEnd) onRunTerminalRef.current?.(runId);
        else scheduleLiveRetry(runId, threadId);
        return;
      }
      setHistoryLoads((prev) => ({
        ...prev,
        [runId]:
          hasContent && sawEnd
            ? { state: "done", events: collected }
            : { state: "error", events: [] },
      }));
    } catch {
      if (isLive) {
        // I-5 — a hard break on a live attach keeps the frames already on
        // screen and re-attaches; blanking to "error" would throw away live
        // content for a run that is still executing.
        setHistoryLoads((prev) => ({ ...prev, [runId]: { state: "live", events: collected } }));
        scheduleLiveRetry(runId, threadId);
        return;
      }
      setHistoryLoads((prev) => ({ ...prev, [runId]: { state: "error", events: [] } }));
    }
  }, [scheduleLiveRetry]);

  replayRef.current = replayHistoryRun;

  // 轨迹视图按页主动回放(非滚动触发):一个 worker 池,worker 数
  // = min(4, 待回放数),每个 worker 顺序从队列取下一个 runId 调用
  // ``replayHistoryRun``(一次性守卫 + 内部降级都复用,不再重复);跳过
  // ``startedHistoryRunsRef`` 里已经开始过的 run。``replayHistoryRun``
  // 本身不抛(失败落 ``error`` 状态),所以这里不需要额外 try/catch。
  const loadRuns = useCallback(
    async (runIds: readonly string[], threadId: string) => {
      const pending = runIds.filter(
        (runId) => !startedHistoryRunsRef.current.has(runId),
      );
      let next = 0;
      const worker = async () => {
        while (next < pending.length) {
          const runId = pending[next];
          next += 1;
          await replayHistoryRun(runId, threadId);
        }
      };
      const workerCount = Math.min(4, pending.length);
      await Promise.all(Array.from({ length: workerCount }, () => worker()));
    },
    [replayHistoryRun],
  );

  // Each history row's ref registers itself with a shared IntersectionObserver
  // (created lazily on first row); a row scrolling into view triggers its
  // replay. The observer's callback closure is bound to one ``threadId``, so
  // ``reset``/``load`` tear it down explicitly on a new resume or
  // agent switch (see the effect below for the unmount case) — otherwise a
  // later resume's rows would replay against the first-captured (now stale)
  // thread. ``runIdByEl`` also guards against re-observing the same element:
  // the ref prop is a fresh closure every render (curried below), so React
  // re-invokes it on every re-render of the row, not just on mount.
  const registerRow = useCallback(
    (runId: string, threadId: string) => (el: HTMLElement | null) => {
      if (el === null) return;
      if (runIdByEl.current.has(el)) return;
      if (observerRef.current === null) {
        observerRef.current = new IntersectionObserver((entries) => {
          for (const e of entries) {
            if (!e.isIntersecting) continue;
            const rid = runIdByEl.current.get(e.target);
            if (rid) void replayHistoryRun(rid, threadId);
          }
        });
      }
      runIdByEl.current.set(el, runId);
      observerRef.current.observe(el);
    },
    [replayHistoryRun],
  );

  // Tear down the shared observer on unmount only — its callback closure is
  // bound to one thread id (see ``registerRow``), so ``reset``
  // and ``load`` also tear it down explicitly whenever they clear
  // ``turns`` for a fresh resume/agent switch. (Keying this effect on
  // ``turns`` instead would also fire on the null→built transition
  // *within* the same resume — right after a row registers — wiping the
  // just-created observer and racing a duplicate replay.)
  useEffect(() => {
    return () => {
      // D-5 — a live attach holds its SSE open indefinitely; leaving the
      // page must tear it down (terminal replays end on their own, but the
      // abort is harmless for them too).
      historyAbortRef.current?.abort();
      observerRef.current?.disconnect();
      observerRef.current = null;
      runIdByEl.current.clear();
      startedHistoryRunsRef.current.clear();
    };
  }, []);

  const patchRuns = useCallback(
    (runs: readonly { runId: string; status: string; tokens: RunTokens | null }[]) => {
      const byId = new Map(runs.map((r) => [r.runId, r]));
      for (const r of runs) {
        if (runStatusRef.current.has(r.runId)) runStatusRef.current.set(r.runId, r.status);
      }
      setHistoryTurns((prev) => {
        if (prev === null) return prev;
        let changed = false;
        const next = prev.map((h) => {
          const r = byId.get(h.runId);
          if (r === undefined) return h;
          // Only in-flight turns take the patch: a terminal status never
          // changes again, and the pairing source (``listThreadRuns``) stays
          // authoritative for turns it already settled.
          if (!NON_TERMINAL_RUN_STATUSES.has(h.status)) return h;
          if (r.status === h.status && r.tokens === h.tokens) return h;
          changed = true;
          return { ...h, status: r.status, tokens: r.tokens };
        });
        return changed ? next : prev;
      });
    },
    [],
  );

  return {
    messages: history,
    turns: historyTurns,
    loads: historyLoads,
    registerRow,
    loadRuns,
    load,
    reset,
    patchRuns,
  };
}
