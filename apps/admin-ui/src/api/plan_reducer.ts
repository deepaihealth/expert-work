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

/** 一次增量归约的结果,也是下一次的续跑点。 */
export interface PlanFold {
  /** 已归约到的事件条数。 */
  count: number;
  /** 已归约前缀的最后一个事件对象 —— 「前缀原样没动」的哨兵。 */
  lastEvent: SseEvent | null;
  /** 到 `count` 为止的最后一份快照(等同 `reducePlan(events.slice(0, count))`)。 */
  snapshot: PlanSnapshot | null;
  /** 本次真正扫过的条数 —— 增量是否生效的可断言证据。 */
  scanned: number;
}

/** 增量归约。live 事件数组每帧重建但只追加(`useRunEngine` 的
 *  `events: [...tn.events, frame]`),所以上一次的前缀对象原样还在 → 只扫
 *  尾部;前缀对不上(历史重建)或数组变短(重置/换会话)则整段重扫。
 *
 *  `planFromEvent` 拿到的始终是**整段的绝对下标**,所以无 id 帧的
 *  `${index}` 兜底 sourceKey 与整段 `reducePlan` 逐字一致 —— 归约本身是
 *  「最后非 null 者胜」的左折叠,续跑与整扫同义。 */
export function foldPlan(
  events: readonly SseEvent[],
  prev: PlanFold | null,
): PlanFold {
  const resumable =
    prev !== null &&
    prev.count > 0 &&
    events.length >= prev.count &&
    events[prev.count - 1] === prev.lastEvent;
  const from = resumable ? prev.count : 0;
  let last = resumable ? prev.snapshot : null;
  for (let i = from; i < events.length; i += 1) {
    const s = planFromEvent(events[i], i);
    if (s !== null) last = s;
  }
  return {
    count: events.length,
    lastEvent: events.length > 0 ? events[events.length - 1] : null,
    snapshot: last,
    scanned: events.length - from,
  };
}

/** 一段帧 → 最后一份计划快照(按数组顺序,最后非 null 者胜);没有 → null。 */
export function reducePlan(events: readonly SseEvent[]): PlanSnapshot | null {
  return foldPlan(events, null).snapshot;
}
