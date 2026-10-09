/** B-168 B2 —— 后台记忆写回的结果按 run 取一次:只取已结束、且确有「已排队」写回行的轮。 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

import * as runsSdk from "../../../api/runs";
import type { RunDetail } from "../../../api/runs";
import type { SseEvent } from "../../../api/sessions";
import type { ConsoleTurn } from "../types";
import { useBackgroundWritebacks } from "../use_background_writebacks";

vi.mock("../../../tenant/TenantScopeContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../tenant/TenantScopeContext")>()),
  useTenantScope: () => ({ scope: "home", setScope: () => {}, apiTenantScope: undefined }),
}));

const getRunMock = vi.spyOn(runsSdk, "getRun");

afterEach(() => {
  getRunMock.mockReset();
});

function upd(node: string, channels: Record<string, unknown>): SseEvent {
  return { id: null, event: "updates", data: { [node]: channels }, rawData: "", receivedAt: "t" };
}

const QUEUED = upd("memory_writeback", { written_memory_count: 0, memory_writeback_queued: true });
const INLINE = upd("memory_writeback", { written_memory_count: 1 });

function turn(runId: string, events: SseEvent[], status: "done" | "running" = "done"): ConsoleTurn {
  return {
    key: runId, seq: 0, source: "history",
    turn: { id: runId, input: "q", attachments: [], inputs: {}, events, status, error: null, approval: null },
    runId, loadState: "done", fallbackLines: [], platformLines: [], tokens: null, timing: null,
    createdAt: null, finishedAt: null, runError: null, supersededBy: null, tombstone: false,
  };
}

const RESULT = { status: "done", written_count: 2, failed: false, queued_ms: 900, exec_ms: 3000 } as const;

function detail(runId: string): RunDetail {
  return {
    run_id: runId, thread_id: "th-1", status: "success", pending_approval: null,
    memory_writeback: RESULT,
  };
}

async function flush(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 5; i += 1) await Promise.resolve();
  });
}

describe("useBackgroundWritebacks", () => {
  it("fetches the run detail once for each finished turn with a queued writeback", async () => {
    getRunMock.mockImplementation(async (_thread, runId) => detail(runId));
    const turns = [turn("r1", [QUEUED]), turn("r2", [INLINE]), turn("r3", [QUEUED], "running")];

    const { result, rerender } = renderHook(
      ({ ts }: { ts: ConsoleTurn[] }) => useBackgroundWritebacks("th-1", ts),
      { initialProps: { ts: turns } },
    );
    await flush();

    expect(getRunMock).toHaveBeenCalledTimes(1);
    expect(getRunMock.mock.calls[0].slice(0, 2)).toEqual(["th-1", "r1"]);
    expect(result.current.get("r1")).toEqual(RESULT);
    expect(result.current.has("r2")).toBe(false);

    rerender({ ts: [...turns] });
    await flush();
    expect(getRunMock).toHaveBeenCalledTimes(1); // not re-fetched on re-render
  });

  it("a turns update while the lookup is in flight does not drop its result", async () => {
    let resolve: (d: RunDetail) => void = () => {};
    getRunMock.mockImplementation(
      () => new Promise<RunDetail>((r) => { resolve = r; }),
    );
    const turns = [turn("r1", [QUEUED])];
    const { result, rerender } = renderHook(
      ({ ts }: { ts: ConsoleTurn[] }) => useBackgroundWritebacks("th-1", ts),
      { initialProps: { ts: turns } },
    );
    await flush();
    rerender({ ts: [...turns, turn("r2", [INLINE])] }); // live frames keep arriving
    await flush();
    resolve(detail("r1"));
    await flush();

    expect(result.current.get("r1")).toEqual(RESULT);
  });

  it("a result for the previous conversation is dropped after switching", async () => {
    const resolvers: ((d: RunDetail) => void)[] = [];
    getRunMock.mockImplementation(
      () => new Promise<RunDetail>((r) => { resolvers.push(r); }),
    );
    const { result, rerender } = renderHook(
      ({ th }: { th: string }) => useBackgroundWritebacks(th, [turn("r1", [QUEUED])]),
      { initialProps: { th: "th-1" } },
    );
    await flush();
    rerender({ th: "th-2" });
    await flush();
    expect(getRunMock.mock.calls.map((c) => c[0])).toEqual(["th-1", "th-2"]);

    resolvers[0]({ ...detail("r1"), memory_writeback: { ...RESULT, written_count: 99 } });
    await flush();
    expect(result.current.size).toBe(0); // th-1's late answer never lands in th-2

    resolvers[1](detail("r1"));
    await flush();
    expect(result.current.get("r1")).toEqual(RESULT);
  });

  it("a failed lookup leaves the row on its queued text", async () => {
    getRunMock.mockRejectedValue(new Error("boom"));
    const { result } = renderHook(() => useBackgroundWritebacks("th-1", [turn("r1", [QUEUED])]));
    await flush();
    expect(result.current.size).toBe(0);
  });
});
