/**
 * usePlanCard — the console shell task card's three-source precedence
 * (R6, Stream CM-8): a GET baseline on thread mount, live `plan` stream
 * frames (Task 3's ``reducePlan``) overlaid on top of it as they arrive,
 * and a PUT echo as the last writer after a save.
 *
 * A live snapshot is applied at most once per distinct ``sourceKey``
 * (the reducer's frame-id / index dedupe key — see plan_reducer.ts).
 * Once any live snapshot has been applied for the current thread, the
 * (possibly still in-flight, now-stale) baseline fetch must not clobber
 * it — the live stream and the PUT echo both outrank the initial GET.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { getThreadPlan, updateThreadPlan, type ThreadPlan } from "../../api/plan";
import { foldPlan, isPlan, type PlanFold } from "../../api/plan_reducer";
import type { SseEvent } from "../../api/sessions";

export interface UsePlanCardArgs {
  threadId: string | null;
  /** Only live-turn events (no lazily-rebuilt history); callers pass a
   *  memo of `liveTurns.flatMap(t => t.events)`. */
  liveEvents: readonly SseEvent[];
  /** DI seams (tests) — default to the real SDK. */
  fetchPlan?: typeof getThreadPlan;
  savePlan?: typeof updateThreadPlan;
}

export interface UsePlanCard {
  plan: ThreadPlan | null;
  loaded: boolean;
  save: (next: ThreadPlan) => Promise<void>;
  saving: boolean;
}

export function usePlanCard({
  threadId,
  liveEvents,
  fetchPlan = getThreadPlan,
  savePlan = updateThreadPlan,
}: UsePlanCardArgs): UsePlanCard {
  const [plan, setPlan] = useState<ThreadPlan | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  // The last-applied live sourceKey for the current thread — also doubles
  // as "a live snapshot has spoken" so a slow baseline fetch that resolves
  // afterwards knows not to overwrite it.
  const appliedRef = useRef<string | null>(null);
  // I2 — `liveEvents` 每帧重建,整段重扫就是每帧 O(全会话事件数)。留住上
  // 一次的折叠点,前缀原样时只扫新追加的尾部(语义与整段 `reducePlan`
  // 等同,见 plan_reducer.ts 的 foldPlan)。
  const foldRef = useRef<PlanFold | null>(null);

  useEffect(() => {
    setPlan(null);
    setLoaded(false);
    appliedRef.current = null;
    foldRef.current = null;
    if (threadId === null) return;
    let cancelled = false;
    (async () => {
      try {
        const fetched = await fetchPlan(threadId);
        // Shape-guard the baseline: ``getThreadPlan`` hands back the raw
        // body, and a wrong-shaped one (mis-routed envelope, proxy page)
        // must degrade to "no plan" instead of crashing the whole page on
        // ``plan.steps``.
        const result = isPlan(fetched) ? fetched : null;
        // A live snapshot may have already arrived while this was in
        // flight (or the thread may have changed — `cancelled` guards
        // that); either way the baseline is stale, so skip it.
        if (!cancelled && appliedRef.current === null) setPlan(result);
      } catch {
        // Best-effort card — it still renders without a baseline plan.
      } finally {
        if (!cancelled) setLoaded(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [threadId, fetchPlan]);

  useEffect(() => {
    const fold = foldPlan(liveEvents, foldRef.current);
    foldRef.current = fold;
    const s = fold.snapshot;
    if (s !== null && s.sourceKey !== appliedRef.current) {
      appliedRef.current = s.sourceKey;
      setPlan(s.plan);
    }
  }, [liveEvents]);

  const save = useCallback(
    async (next: ThreadPlan) => {
      if (threadId === null) return;
      setSaving(true);
      try {
        const stored = await savePlan(threadId, next);
        setPlan(stored);
      } finally {
        setSaving(false);
      }
    },
    [threadId, savePlan],
  );

  return { plan, loaded, save, saving };
}
