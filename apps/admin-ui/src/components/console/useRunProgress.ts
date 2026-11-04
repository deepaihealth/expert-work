/**
 * B-269 / B-270 —— 一轮还在跑 / 在排队时,定时读 run 详情里的 ``progress`` 与
 * ``queued_behind``(服务端从落库帧聚合,跨副本一致)。同一个定时器也推进 ``nowMs``,
 * 让「已进行 N 分钟」跟着走。读失败就保留上一次的值:气泡退回原文案,不报错。
 */
import { useEffect, useState } from "react";

import { getRun, type QueuedBehind, type RunProgress } from "../../api/runs";
import { concreteTenantScope, useTenantScope } from "../../tenant/TenantScopeContext";

export const RUN_PROGRESS_POLL_MS = 15_000;

export interface RunProgressState {
  progress: RunProgress | null;
  queuedBehind: QueuedBehind | null;
  nowMs: number;
}

interface Fetched {
  runId: string;
  progress: RunProgress | null;
  queuedBehind: QueuedBehind | null;
}

export function useRunProgress(threadId: string, runId: string): RunProgressState {
  const { apiTenantScope } = useTenantScope();
  const [fetched, setFetched] = useState<Fetched | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    let cancelled = false;
    const tick = () => {
      setNowMs(Date.now());
      getRun(threadId, runId, concreteTenantScope(apiTenantScope))
        .then((detail) => {
          if (cancelled) return;
          setFetched({
            runId,
            progress: detail.progress ?? null,
            queuedBehind: detail.queued_behind ?? null,
          });
        })
        .catch(() => {
          // Best-effort: the bubble keeps its plain text until the next tick.
        });
    };
    tick();
    const timer = setInterval(tick, RUN_PROGRESS_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [threadId, runId, apiTenantScope]);

  // 换了一轮(同一个气泡复用)时,上一轮读到的值不算数。
  const current = fetched !== null && fetched.runId === runId ? fetched : null;
  return {
    progress: current?.progress ?? null,
    queuedBehind: current?.queuedBehind ?? null,
    nowMs,
  };
}

/** 只推进「现在」(live 排队轮:领头那一轮的开始时刻在排队帧里,不用读库)。 */
export function useNow(intervalMs: number): number {
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNowMs(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return nowMs;
}
