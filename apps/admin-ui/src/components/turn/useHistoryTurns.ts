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

import { listThreadRuns, streamRunEvents } from "../../api/runs";
import {
  getSessionMessages,
  type HistoryMessage,
  type SseEvent,
} from "../../api/sessions";
import { buildHistoryTurns } from "../../pages/agent_detail/playground/history_turns";
import { useTenantScope } from "../../tenant/TenantScopeContext";
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
  /** Fetch + pair a thread's history. Never rejects (degrades internally). */
  load: (threadId: string) => Promise<void>;
  /** Clear the rebuilt history and tear down the observer (new draft / agent
   *  switch). Deliberately does NOT abort the in-flight fetch — same as the
   *  original ``resetDraft``. */
  reset: () => void;
}

export function useHistoryTurns(): UseHistoryTurns {
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

  // 历史重建:并行拉文本轮 + runs,按序配对成懒重建描述符;
  // 计数守卫失败或任一失败 → 落回扁平文本(setHistory 保留原行为)。
  const load = useCallback(async (threadId: string) => {
    setHistory([]);
    setHistoryTurns(null);
    setHistoryLoads({});
    historyAbortRef.current?.abort();
    const ac = new AbortController();
    historyAbortRef.current = ac;
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
      getSessionMessages(threadId),
      listThreadRuns(threadId).catch(() => null),
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
  }, []);

  // 历史懒重建 — replay one historical run's persisted event stream so its
  // TurnCard can render the full debug panels (rather than just the flat
  // fallback answer). Fire-and-forget from the IntersectionObserver callback
  // below; a replay failure leaves the turn on its fallback (degradation).
  // ``startedHistoryRunsRef`` makes this idempotent — a row's viewport
  // trigger can fire more than once (see ``registerRow``).
  const replayHistoryRun = useCallback(async (runId: string, threadId: string) => {
    if (startedHistoryRunsRef.current.has(runId)) return;
    startedHistoryRunsRef.current.add(runId);
    setHistoryLoads((prev) => ({
      ...prev,
      [runId]: { state: "loading", events: [] },
    }));
    try {
      const collected: SseEvent[] = [];
      for await (const frame of streamRunEvents(threadId, runId, {
        signal: historyAbortRef.current?.signal,
        tenantScope: apiTenantScope,
      })) {
        collected.push(frame);
        if (frame.event === "end") break;
      }
      // The terminal-replay endpoint ALWAYS appends an ``end`` frame after the
      // stored rows (runs.py ``_stream_replay``), so an empty run replays as a
      // lone ``[{event:"end"}]``. Only mark ``done`` when the replay actually
      // delivered renderable content AND completed; an empty replay
      // (no non-end frame) or a truncated one (no end frame) degrades to
      // ``error`` — which keeps the placeholder showing ``fallbackLines``
      // (the /messages text we already have) instead of the full render's
      // "no text" empty state. 不丢已有内容。
      const sawEnd = collected.some((f) => f.event === "end");
      const hasContent = collected.some((f) => f.event !== "end");
      setHistoryLoads((prev) => ({
        ...prev,
        [runId]:
          hasContent && sawEnd
            ? { state: "done", events: collected }
            : { state: "error", events: [] },
      }));
    } catch {
      setHistoryLoads((prev) => ({ ...prev, [runId]: { state: "error", events: [] } }));
    }
  }, [apiTenantScope]);

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
      observerRef.current?.disconnect();
      observerRef.current = null;
      runIdByEl.current.clear();
      startedHistoryRunsRef.current.clear();
    };
  }, []);

  return {
    messages: history,
    turns: historyTurns,
    loads: historyLoads,
    registerRow,
    load,
    reset,
  };
}
