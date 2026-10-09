/**
 * B-168 B2 —— 后台记忆写回在 `end` 之后才跑,结果记在任务行上、不进事件流;控制台的
 * run 详情(`getRun` 的 `memory_writeback`)带着它。这里按 run 取一次:只取已结束、
 * 且事件里确有「已排队」写回行的轮。取失败就当没有 —— 那一行停在「已排队」。
 * 不轮询:任务还在排队 / 执行时取到的就是那个状态,刷新页面再看(设计稿 §2)。
 */
import { useEffect, useRef, useState } from "react";

import { getRun, type MemoryWritebackResult } from "../../api/runs";
import type { SseEvent } from "../../api/sessions";
import { concreteTenantScope, useTenantScope } from "../../tenant/TenantScopeContext";
import type { ConsoleTurn } from "./types";

const EMPTY: ReadonlyMap<string, MemoryWritebackResult> = new Map();

function hasQueuedWriteback(events: readonly SseEvent[]): boolean {
  return events.some((e) => {
    if (e.event !== "updates" || e.data === null || typeof e.data !== "object") return false;
    const writes = (e.data as { memory_writeback?: unknown }).memory_writeback;
    return (
      writes !== null &&
      typeof writes === "object" &&
      (writes as { memory_writeback_queued?: unknown }).memory_writeback_queued === true
    );
  });
}

export function useBackgroundWritebacks(
  threadId: string | null,
  turns: readonly ConsoleTurn[],
): ReadonlyMap<string, MemoryWritebackResult> {
  const { apiTenantScope } = useTenantScope();
  const [results, setResults] = useState<ReadonlyMap<string, MemoryWritebackResult>>(EMPTY);
  /** 已经发过请求的 run(成功与否都不再发)。换会话清空。 */
  const requested = useRef<Set<string>>(new Set());
  /** 当前会话。请求回来时会话已经换了就丢掉 —— 不能靠 effect 的清理来作废:
   *  live 帧每到一帧 `turns` 就换一次引用,清理会把还在路上的请求作废,而
   *  `requested` 已经记过它,那一行就永远停在「已排队」。 */
  const currentThread = useRef(threadId);

  useEffect(() => {
    currentThread.current = threadId;
    requested.current = new Set();
    setResults(EMPTY);
  }, [threadId]);

  useEffect(() => {
    if (threadId === null) return;
    for (const t of turns) {
      const runId = t.runId;
      if (runId === null || requested.current.has(runId)) continue;
      if (t.turn.status === "running" || !hasQueuedWriteback(t.turn.events)) continue;
      requested.current.add(runId);
      void getRun(threadId, runId, concreteTenantScope(apiTenantScope))
        .then((detail) => {
          const result = detail.memory_writeback;
          if (currentThread.current !== threadId || result === null || result === undefined) return;
          setResults((prev) => new Map(prev).set(runId, result));
        })
        .catch(() => {
          // Best-effort —— 那一行停在「已排队」。
        });
    }
  }, [threadId, turns, apiTenantScope]);

  return results;
}
