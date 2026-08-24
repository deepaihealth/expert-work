/**
 * useRunTrace — Langfuse trace fetch / not_ready polling / re-arm-on-done /
 * system_admin `trace_id` lookup, lifted verbatim out of
 * ``components/turn/TurnCard.tsx:121-126`` (retry constants) and ``:555-640``
 * (the five effects below) so the redesigned console can drive the same
 * "exact" trace view without importing TurnCard (retired in PR-B).
 *
 * Unlike TurnCard — where each mounted instance owns a single, immutable
 * `runId` for its lifetime — this hook is shared across turns switching in
 * place, so it additionally resets `trace` / `traceId` when `runId` changes
 * (TurnCard never needed that: a new turn was always a new TurnCard mount).
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { getRun } from "../../api/runs";
import { getRunTrace, type RunTrace } from "../../api/trace_facade";
import { concreteTenantScope, useTenantScope } from "../../tenant/TenantScopeContext";
import type { TurnStatus } from "../turn/types";

// A just-finished run's Langfuse trace lands as `not_ready` for a moment
// (ingestion isn't atomic — the root closes before its child observations
// land). Auto-poll a few times so the waterfall appears without a manual
// refresh, then settle on whatever we last got.
export const TRACE_NOT_READY_MAX_RETRIES = 6;
export const TRACE_NOT_READY_RETRY_MS = 1500;

export interface RunTraceState {
  /** `null` = not fetched yet / fetch in flight. */
  trace: RunTrace | null;
  loading: boolean;
  /** Manual refresh: resets the retry budget and clears `trace` (which
   *  re-triggers the fetch effect below). */
  refresh: () => void;
  /** Langfuse `trace_id`, fetched via `getRun` only when `wantTraceId` —
   *  `null` otherwise (and while the fetch is in flight / on failure). */
  traceId: string | null;
}

export function useRunTrace(args: {
  threadId: string | null;
  runId: string | null;
  /** The panel showing this turn is actually displayed — the fetch is lazy. */
  enabled: boolean;
  turnStatus: TurnStatus;
  /** system_admin — Langfuse has no per-tenant isolation, so the deep link
   *  (and the `trace_id` lookup behind it) is admin-only. */
  wantTraceId: boolean;
}): RunTraceState {
  const { threadId, runId, enabled, turnStatus, wantTraceId } = args;
  const { apiTenantScope } = useTenantScope();

  const [trace, setTrace] = useState<RunTrace | null>(null);
  const traceRetriesRef = useRef(0);
  const [traceId, setTraceId] = useState<string | null>(null);

  // Lazy fetch — only once this hook is `enabled` and the trace hasn't
  // already landed (`trace === null` re-arms it; see the effects below).
  useEffect(() => {
    if (!enabled || !threadId || !runId || trace !== null) return;
    let cancelled = false;
    void getRunTrace(threadId, runId, concreteTenantScope(apiTenantScope))
      .then((data) => {
        if (!cancelled) setTrace(data);
      })
      .catch(() => {
        if (!cancelled) setTrace({ status: "unavailable" });
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, threadId, runId, trace, apiTenantScope]);

  // Fresh retry budget whenever the run or the enabled state changes.
  useEffect(() => {
    traceRetriesRef.current = 0;
  }, [runId, enabled]);

  // A new `runId` means a different run's trace — don't keep showing the
  // previous run's (possibly stale) trace / trace_id while the fresh fetch
  // is in flight.
  useEffect(() => {
    setTrace(null);
    setTraceId(null);
  }, [runId]);

  // Auto-poll while the trace is still ingesting (`not_ready`), up to the
  // cap — clearing `trace` re-triggers the fetch effect above. Stops the
  // moment the trace resolves to any non-`not_ready` state (or the budget
  // runs out).
  useEffect(() => {
    if (trace?.status !== "not_ready" || traceRetriesRef.current >= TRACE_NOT_READY_MAX_RETRIES) {
      return;
    }
    const timer = setTimeout(() => {
      traceRetriesRef.current += 1;
      setTrace(null);
    }, TRACE_NOT_READY_RETRY_MS);
    return () => clearTimeout(timer);
  }, [trace]);

  // A finished run's trace only lands in Langfuse after the run closes, so
  // the lazy fetch above may have run (and burned its not-ready retry
  // budget) while the run was still in flight. Re-arm one fresh fetch the
  // moment this turn completes, so the exact view auto-refreshes instead of
  // needing a manual click.
  useEffect(() => {
    // BUG-9 — interrupted is terminal too: its partial trace has closed in
    // Langfuse, so re-arm the fetch the same way a completed turn does.
    if (turnStatus === "done" || turnStatus === "interrupted") setTrace(null);
  }, [turnStatus]);

  // system_admin only — Langfuse has no per-tenant isolation. Best-effort:
  // a failed getRun just leaves traceId (and the deep link) hidden.
  useEffect(() => {
    if (!wantTraceId || !threadId || !runId) return;
    let cancelled = false;
    void getRun(threadId, runId, concreteTenantScope(apiTenantScope))
      .then((detail) => {
        if (!cancelled) setTraceId(detail.trace_id ?? null);
      })
      .catch(() => {
        // Best-effort — the link simply stays hidden on failure.
      });
    return () => {
      cancelled = true;
    };
  }, [wantTraceId, threadId, runId, apiTenantScope]);

  const refresh = useCallback(() => {
    traceRetriesRef.current = 0;
    setTrace(null);
  }, []);

  // `runId === null`(metadata 帧还没到)时上面的 fetch effect 直接返回,
  // 不加这一项 loading 就永真,Timing tab 一直挂在 `console.timing_loading`。
  return { trace, loading: enabled && runId !== null && trace === null, refresh, traceId };
}
