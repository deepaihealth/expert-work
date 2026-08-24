/**
 * useRunTrace — trace fetch / not_ready polling / re-arm-on-done / trace_id
 * lookup, lifted out of ``components/turn/TurnCard.tsx``. These tests pin
 * the five behaviours from the task-14 brief: lazy fetch gated on
 * `enabled`, `not_ready` auto-polling up to the retry cap, re-arming one
 * fetch when the turn finishes, mapping a rejection to `unavailable` (with
 * `refresh()` re-fetching), and the admin-only `trace_id` lookup resetting
 * on `runId` change.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

import * as runsSdk from "../../../api/runs";
import type { RunDetail } from "../../../api/runs";
import * as traceFacadeSdk from "../../../api/trace_facade";
import type { RunTrace } from "../../../api/trace_facade";
import type { TurnStatus } from "../../turn/types";
import {
  TRACE_NOT_READY_MAX_RETRIES,
  TRACE_NOT_READY_RETRY_MS,
  useRunTrace,
} from "../useRunTrace";

// Track C W2 — hook 内直取 tenant scope;renderHook 不挂 Provider,mock 成
// home 态(apiTenantScope undefined)。照 useHistoryTurns.test.ts。
vi.mock("../../../tenant/TenantScopeContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../tenant/TenantScopeContext")>()),
  useTenantScope: () => ({
    scope: "home",
    setScope: () => {},
    apiTenantScope: undefined,
  }),
}));

const getRunTraceMock = vi.spyOn(traceFacadeSdk, "getRunTrace");
const getRunMock = vi.spyOn(runsSdk, "getRun");

const notReady: RunTrace = { status: "not_ready" };
const okEmpty: RunTrace = {
  status: "ok",
  trace: { name: "run", latencyMs: 1, totalCostUsd: null, spanCount: 0 },
  spans: [],
};

function runDetail(runId: string, traceId: string | null): RunDetail {
  return { run_id: runId, thread_id: "th-1", status: "running", pending_approval: null, trace_id: traceId };
}

// Drains already-queued microtasks (a resolved mock's `.then()`, plus the
// React state update / effect commit it triggers) without moving the fake
// clock — `advanceTimersByTimeAsync(0)` alone doesn't reliably chain through
// a promise → setState → effect → setTimeout cycle spanning renders.
async function flushMicrotasks(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 5; i += 1) await Promise.resolve();
  });
}

// One full not_ready retry cycle: advance past the 1.5 s timer, then drain
// the fetch promise it re-triggers.
async function advanceOneRetry(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(TRACE_NOT_READY_RETRY_MS);
  });
  await flushMicrotasks();
}

beforeEach(() => {
  vi.useFakeTimers();
  getRunTraceMock.mockReset();
  getRunMock.mockReset();
});

afterEach(() => {
  // A test may leave one not_ready retry timer pending (its budget wasn't
  // exhausted / advanced) — discard rather than fire it, so a stray
  // post-assertion `setState` never fires outside `act(...)`.
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("useRunTrace", () => {
  it("fetches lazily once enabled and stops when disabled", async () => {
    getRunTraceMock.mockResolvedValue(okEmpty);

    const { result, rerender } = renderHook(
      (props: { enabled: boolean }) =>
        useRunTrace({
          threadId: "th-1",
          runId: "r1",
          enabled: props.enabled,
          turnStatus: "running",
          wantTraceId: false,
        }),
      { initialProps: { enabled: false } },
    );

    await flushMicrotasks();
    expect(getRunTraceMock).not.toHaveBeenCalled();
    expect(result.current.trace).toBeNull();
    expect(result.current.loading).toBe(false);

    rerender({ enabled: true });
    await flushMicrotasks();
    expect(getRunTraceMock).toHaveBeenCalledTimes(1);
    expect(result.current.trace).toEqual(okEmpty);
  });

  it("auto-polls not_ready up to 6 times at 1.5 s, then settles", async () => {
    getRunTraceMock.mockResolvedValue(notReady);

    renderHook(() =>
      useRunTrace({
        threadId: "th-1",
        runId: "r1",
        enabled: true,
        turnStatus: "running",
        wantTraceId: false,
      }),
    );
    await flushMicrotasks();
    expect(getRunTraceMock).toHaveBeenCalledTimes(1); // mount's lazy fetch

    for (let i = 1; i <= TRACE_NOT_READY_MAX_RETRIES; i += 1) {
      await advanceOneRetry();
      expect(getRunTraceMock).toHaveBeenCalledTimes(i + 1);
    }

    // Budget exhausted — no further calls no matter how much more time passes.
    await advanceOneRetry();
    expect(getRunTraceMock).toHaveBeenCalledTimes(TRACE_NOT_READY_MAX_RETRIES + 1);
  });

  it("re-arms one fetch when the turn turns done", async () => {
    getRunTraceMock.mockResolvedValue(notReady);

    const { rerender } = renderHook(
      (props: { turnStatus: TurnStatus }) =>
        useRunTrace({
          threadId: "th-1",
          runId: "r1",
          enabled: true,
          turnStatus: props.turnStatus,
          wantTraceId: false,
        }),
      { initialProps: { turnStatus: "running" as TurnStatus } },
    );

    // Burn the whole not_ready retry budget while the turn is still running.
    await flushMicrotasks();
    for (let i = 0; i < TRACE_NOT_READY_MAX_RETRIES; i += 1) await advanceOneRetry();
    expect(getRunTraceMock).toHaveBeenCalledTimes(TRACE_NOT_READY_MAX_RETRIES + 1);

    rerender({ turnStatus: "done" });
    await flushMicrotasks();
    // Exactly one re-armed fetch — the retry budget itself isn't reset by
    // `turnStatus`, so the fresh `not_ready` doesn't re-trigger polling.
    expect(getRunTraceMock).toHaveBeenCalledTimes(TRACE_NOT_READY_MAX_RETRIES + 2);

    await advanceOneRetry();
    expect(getRunTraceMock).toHaveBeenCalledTimes(TRACE_NOT_READY_MAX_RETRIES + 2);
  });

  it("maps a rejected fetch to unavailable; refresh() re-fetches", async () => {
    getRunTraceMock.mockRejectedValueOnce(new Error("boom"));

    const { result } = renderHook(() =>
      useRunTrace({
        threadId: "th-1",
        runId: "r1",
        enabled: true,
        turnStatus: "running",
        wantTraceId: false,
      }),
    );

    await flushMicrotasks();
    expect(result.current.trace).toEqual({ status: "unavailable" });
    expect(getRunTraceMock).toHaveBeenCalledTimes(1);

    getRunTraceMock.mockResolvedValueOnce(okEmpty);
    act(() => {
      result.current.refresh();
    });
    await flushMicrotasks();
    expect(getRunTraceMock).toHaveBeenCalledTimes(2);
    expect(result.current.trace).toEqual(okEmpty);
  });

  it("fetches trace_id via getRun only when wantTraceId; resets on runId change", async () => {
    getRunTraceMock.mockResolvedValue(notReady);
    getRunMock.mockResolvedValueOnce(runDetail("r1", "tr-1"));

    const { result, rerender } = renderHook(
      (props: { runId: string; wantTraceId: boolean }) =>
        useRunTrace({
          threadId: "th-1",
          runId: props.runId,
          enabled: true,
          turnStatus: "running",
          wantTraceId: props.wantTraceId,
        }),
      { initialProps: { runId: "r1", wantTraceId: false } },
    );

    await flushMicrotasks();
    expect(getRunMock).not.toHaveBeenCalled();
    expect(result.current.traceId).toBeNull();

    rerender({ runId: "r1", wantTraceId: true });
    await flushMicrotasks();
    expect(getRunMock).toHaveBeenCalledTimes(1);
    expect(result.current.traceId).toBe("tr-1");

    // Second run's getRun deliberately stays pending — proves the reset
    // happens on the `runId` change itself, not merely once the new fetch
    // eventually overwrites the old value.
    let resolveSecond!: (detail: RunDetail) => void;
    const second = new Promise<RunDetail>((resolve) => {
      resolveSecond = resolve;
    });
    getRunMock.mockReturnValueOnce(second);

    act(() => {
      rerender({ runId: "r2", wantTraceId: true });
    });
    expect(result.current.traceId).toBeNull();

    resolveSecond(runDetail("r2", "tr-2"));
    await flushMicrotasks();
    expect(getRunMock).toHaveBeenCalledTimes(2);
    expect(result.current.traceId).toBe("tr-2");
  });

  it("M5 — a null runId is not 'loading': the fetch effect never runs, so the Timing tab must not spin forever", async () => {
    getRunTraceMock.mockResolvedValue(okEmpty);

    const { result, rerender } = renderHook(
      (props: { runId: string | null }) =>
        useRunTrace({
          threadId: "th-1",
          runId: props.runId,
          enabled: true,
          turnStatus: "running",
          wantTraceId: false,
        }),
      { initialProps: { runId: null as string | null } },
    );

    await flushMicrotasks();
    expect(getRunTraceMock).not.toHaveBeenCalled();
    expect(result.current.trace).toBeNull();
    expect(result.current.loading).toBe(false);

    // Once the metadata frame lands the run id, loading is true again until
    // the fetch resolves.
    rerender({ runId: "r1" });
    expect(result.current.loading).toBe(true);
    await flushMicrotasks();
    expect(result.current.trace).toEqual(okEmpty);
    expect(result.current.loading).toBe(false);
  });
});
