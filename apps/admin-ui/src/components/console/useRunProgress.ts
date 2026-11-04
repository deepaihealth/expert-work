/**
 * B-269 / B-270 —— 气泡上的进度用到的两个计时:
 *
 * * ``useNow`` —— 让「已进行 N 分钟」跟着走;页面不可见时停。
 * * ``useQueuedBehind`` —— 排队中的一轮定时读 run 详情的 ``queued_behind``(排队帧只是
 *   受理那一刻的快照)。只在页面可见、且看的是自己租户时读:切到别的租户时每次读都会
 *   记一条租户切换审计,轮询会刷屏,这时只用排队帧。读失败就保留上一次的值。
 *
 * 运行中的一轮不读库:它的事件流就在手里(``progressFromEvents``)。
 */
import { useEffect, useState } from "react";

import { getRun, type QueuedBehind } from "../../api/runs";

export const QUEUED_POLL_MS = 30_000;

function documentVisible(): boolean {
  return typeof document === "undefined" || document.visibilityState === "visible";
}

/** 页面(浏览器标签)当前是否可见。 */
export function useDocumentVisible(): boolean {
  const [visible, setVisible] = useState(documentVisible);
  useEffect(() => {
    const onChange = () => setVisible(documentVisible());
    document.addEventListener("visibilitychange", onChange);
    return () => document.removeEventListener("visibilitychange", onChange);
  }, []);
  return visible;
}

/** 每 ``intervalMs`` 推进一次「现在」;``enabled`` 为 false 时停(重新打开时立刻补一拍)。 */
export function useNow(intervalMs: number, enabled: boolean): number {
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    if (!enabled) return;
    setNowMs(Date.now());
    const timer = setInterval(() => setNowMs(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs, enabled]);
  return nowMs;
}

interface Polled {
  runId: string;
  queuedBehind: QueuedBehind | null;
}

/** ``undefined`` = 还没读到(调用方用排队帧);``null`` = 前面已经没人了。 */
export function useQueuedBehind(
  threadId: string,
  runId: string,
  enabled: boolean,
): QueuedBehind | null | undefined {
  const [polled, setPolled] = useState<Polled | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    const tick = () => {
      getRun(threadId, runId)
        .then((detail) => {
          if (!cancelled) setPolled({ runId, queuedBehind: detail.queued_behind ?? null });
        })
        .catch(() => {
          // Best-effort: keep the last value (or the queued frame) until the next tick.
        });
    };
    tick();
    const timer = setInterval(tick, QUEUED_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [threadId, runId, enabled]);
  return polled !== null && polled.runId === runId ? polled.queuedBehind : undefined;
}
