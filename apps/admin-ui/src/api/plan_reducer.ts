/**
 * plan_reducer — derives the thread's current plan from the run event
 * stream, for the console shell's plan panel.
 *
 * A plan reaches the client two ways: the top-level `plan` frame (PR1's
 * dedicated CM-8 stream event) or, on runs persisted before PR1 shipped,
 * an `updates` frame carrying a node's `plan` key. Either source normalizes
 * to a `PlanSnapshot`; `reducePlan` folds a whole event array down to the
 * latest one.
 */
import type { ThreadPlan } from "./plan";
import type { SseEvent } from "./sessions";

export interface PlanSnapshot {
  plan: ThreadPlan;
  /** 去重键:帧 id;无 id 时 `${index}` 兜底(调用方传 index)。 */
  sourceKey: string;
}

export function isPlan(v: unknown): v is ThreadPlan {
  return (
    v !== null && typeof v === "object" &&
    typeof (v as { goal?: unknown }).goal === "string" &&
    Array.isArray((v as { steps?: unknown }).steps)
  );
}

/** 单帧 → 计划快照。`plan` 顶层帧(PR1)直接取 data;`updates` 帧取任一节点值里非空的 `plan` 键(多个节点取最后一个)。其它帧 null。 */
export function planFromEvent(evt: SseEvent, index: number): PlanSnapshot | null {
  const key = evt.id ?? String(index);
  if (evt.event === "plan") return isPlan(evt.data) ? { plan: evt.data, sourceKey: key } : null;
  if (evt.event !== "updates" || evt.data === null || typeof evt.data !== "object") return null;
  let last: ThreadPlan | null = null;
  for (const value of Object.values(evt.data as Record<string, unknown>)) {
    if (value === null || typeof value !== "object") continue;
    const p = (value as { plan?: unknown }).plan;
    if (isPlan(p)) last = p;
  }
  return last === null ? null : { plan: last, sourceKey: key };
}

/** 一段帧 → 最后一份计划快照(按数组顺序,最后非 null 者胜);没有 → null。 */
export function reducePlan(events: readonly SseEvent[]): PlanSnapshot | null {
  let last: PlanSnapshot | null = null;
  events.forEach((e, i) => {
    const s = planFromEvent(e, i);
    if (s !== null) last = s;
  });
  return last;
}
