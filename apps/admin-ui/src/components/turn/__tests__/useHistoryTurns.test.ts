/**
 * useHistoryTurns — the #980 lazy history rebuild, extracted out of
 * ``PlaygroundTab``. These tests pin the three behaviours that are easy to
 * break when the flow is re-wired into another page: count pairing, the
 * per-run one-shot replay guard, and the stale-request guard.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import * as runsSdk from "../../../api/runs";
import * as sessionsSdk from "../../../api/sessions";
import type { HistoryMessage, SseEvent } from "../../../api/sessions";
import { useHistoryTurns } from "../useHistoryTurns";

// Track C W2 — hook 内直取 tenant scope;renderHook 不挂 Provider,mock 成
// home 态(apiTenantScope undefined)。
vi.mock("../../../tenant/TenantScopeContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../tenant/TenantScopeContext")>()),
  useTenantScope: () => ({
    scope: "home",
    setScope: () => {},
    apiTenantScope: undefined,
  }),
}));

const getMessagesMock = vi.spyOn(sessionsSdk, "getSessionMessages");
const listThreadRunsMock = vi.spyOn(runsSdk, "listThreadRuns");
const streamRunEventsMock = vi.spyOn(runsSdk, "streamRunEvents");

// Records every ``observe(el)`` across stub instances so the re-registration
// guard (``runIdByEl``) can be asserted directly, not just via its replay
// side effect.
const observeSpy = vi.fn<(el: Element) => void>();

// jsdom has no IntersectionObserver — stub one that treats every observed
// element as immediately visible (fires its callback synchronously from
// ``observe``), mirroring ``PlaygroundTab.test.tsx``.
class IOStub {
  private cb: IntersectionObserverCallback;
  constructor(cb: IntersectionObserverCallback) {
    this.cb = cb;
  }
  observe = (el: Element) => {
    observeSpy(el);
    this.cb(
      [{ isIntersecting: true, target: el } as IntersectionObserverEntry],
      this as unknown as IntersectionObserver,
    );
  };
  unobserve = () => {};
  disconnect = () => {};
  takeRecords = () => [];
  root = null;
  rootMargin = "";
  thresholds: number[] = [];
}

function makeStream(events: SseEvent[]): AsyncGenerator<SseEvent, void, void> {
  return (async function* () {
    for (const e of events) yield e;
  })();
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

const oneRun = [
  {
    runId: "r1",
    status: "success" as const,
    isResume: false,
    createdAt: "2026-05-25T00:00:00Z",
    tokens: null,
  },
];

const oneTurnOfMessages: HistoryMessage[] = [
  { role: "user", content: "q1" },
  { role: "assistant", content: "a1" },
];

beforeEach(() => {
  getMessagesMock.mockReset();
  getMessagesMock.mockResolvedValue([]);
  listThreadRunsMock.mockReset();
  listThreadRunsMock.mockResolvedValue([]);
  streamRunEventsMock.mockReset();
  streamRunEventsMock.mockImplementation(() => makeStream([]));
  observeSpy.mockReset();
  vi.stubGlobal("IntersectionObserver", IOStub);
});

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("useHistoryTurns", () => {
  it("pairs a count-matched thread into lazy history turns", async () => {
    getMessagesMock.mockResolvedValue(oneTurnOfMessages);
    listThreadRunsMock.mockResolvedValue(oneRun);

    const { result } = renderHook(() => useHistoryTurns());
    await act(async () => {
      await result.current.load("th-1");
    });

    expect(result.current.messages).toEqual(oneTurnOfMessages);
    expect(result.current.turns).toEqual([
      {
        key: "r1",
        input: "q1",
        fallbackLines: [{ text: "a1", channel: null }],
        runId: "r1",
        status: "success",
        tokens: null,
        createdAt: "2026-05-25T00:00:00Z",
      },
    ]);
    expect(result.current.loads).toEqual({
      r1: { state: "pending", events: [] },
    });
  });

  it("returns null turns (flat-text degradation) when the counts disagree", async () => {
    getMessagesMock.mockResolvedValue(oneTurnOfMessages);
    listThreadRunsMock.mockResolvedValue([]); // 1 user turn vs 0 runs

    const { result } = renderHook(() => useHistoryTurns());
    await act(async () => {
      await result.current.load("th-1");
    });

    expect(result.current.turns).toBeNull();
    // The degradation path always keeps the flat text.
    expect(result.current.messages).toEqual(oneTurnOfMessages);
  });

  it("threads the caller-supplied tenant through the fetch and each replay", async () => {
    getMessagesMock.mockResolvedValue(oneTurnOfMessages);
    listThreadRunsMock.mockResolvedValue(oneRun);
    streamRunEventsMock.mockImplementation(() =>
      makeStream([
        {
          id: "1",
          event: "updates",
          data: { agent: { messages: [{ type: "ai", content: "replayed" }] } },
          rawData: "",
          receivedAt: "",
        },
        { id: "2", event: "end", data: {}, rawData: "", receivedAt: "" },
      ]),
    );

    const { result } = renderHook(() => useHistoryTurns());
    await act(async () => {
      await result.current.load("th-1", "tenant-x");
    });

    expect(getMessagesMock).toHaveBeenCalledWith("th-1", "tenant-x");
    expect(listThreadRunsMock).toHaveBeenCalledWith("th-1", "tenant-x");

    // The replay a row triggers later reads under the same pinned tenant.
    const row = document.createElement("div");
    await act(async () => {
      result.current.registerRow("r1", "th-1")(row);
    });
    await waitFor(() => expect(result.current.loads.r1.state).toBe("done"));
    expect(streamRunEventsMock).toHaveBeenCalledWith(
      "th-1",
      "r1",
      expect.objectContaining({ tenantScope: "tenant-x" }),
    );
  });

  it("replays a runId only once even when two rows register it", async () => {
    getMessagesMock.mockResolvedValue(oneTurnOfMessages);
    listThreadRunsMock.mockResolvedValue(oneRun);
    streamRunEventsMock.mockImplementation(() =>
      makeStream([
        {
          id: "1",
          event: "updates",
          data: { agent: { messages: [{ type: "ai", content: "replayed" }] } },
          rawData: "",
          receivedAt: "",
        },
        { id: "2", event: "end", data: {}, rawData: "", receivedAt: "" },
      ]),
    );

    const { result } = renderHook(() => useHistoryTurns());
    await act(async () => {
      await result.current.load("th-1");
    });

    // Two DISTINCT elements carrying the same runId — the ``runIdByEl``
    // registry can't dedupe these, so only the per-run one-shot guard keeps
    // the replay from firing twice.
    const rowA = document.createElement("div");
    const rowB = document.createElement("div");
    await act(async () => {
      result.current.registerRow("r1", "th-1")(rowA);
      result.current.registerRow("r1", "th-1")(rowB);
    });

    await waitFor(() => {
      expect(result.current.loads.r1.state).toBe("done");
    });
    expect(streamRunEventsMock).toHaveBeenCalledTimes(1);
    expect(result.current.loads.r1.events).toHaveLength(2);
  });

  it("observes a row element only once when its ref re-registers", async () => {
    getMessagesMock.mockResolvedValue(oneTurnOfMessages);
    listThreadRunsMock.mockResolvedValue(oneRun);

    const { result } = renderHook(() => useHistoryTurns());
    await act(async () => {
      await result.current.load("th-1");
    });

    // A row's ref prop is a fresh closure every render (curried by
    // ``registerRow``), so React re-invokes it on every re-render of that row —
    // not just on mount. ``runIdByEl`` is the guard that keeps the shared
    // observer from accumulating duplicate registrations for the SAME element.
    const row = document.createElement("div");
    await act(async () => {
      result.current.registerRow("r1", "th-1")(row);
      result.current.registerRow("r1", "th-1")(row);
    });

    expect(observeSpy).toHaveBeenCalledTimes(1);
  });

  it("loadRuns caps concurrent replays at 4 across a page of runs", async () => {
    let inFlight = 0;
    let peak = 0;
    const gate = deferred<void>();
    streamRunEventsMock.mockImplementation(() =>
      (async function* () {
        inFlight += 1;
        peak = Math.max(peak, inFlight);
        await gate.promise;
        inFlight -= 1;
        yield {
          id: "1",
          event: "updates",
          data: { agent: { messages: [{ type: "ai", content: "replayed" }] } },
          rawData: "",
          receivedAt: "",
        } as SseEvent;
        yield { id: "2", event: "end", data: {}, rawData: "", receivedAt: "" } as SseEvent;
      })(),
    );

    const runIds = Array.from({ length: 8 }, (_, i) => `r${i}`);
    const { result } = renderHook(() => useHistoryTurns());

    let done!: Promise<void>;
    act(() => {
      done = result.current.loadRuns(runIds, "th-1");
    });

    // Only the first 4 workers should have reached the gate — the rest are
    // queued behind them, not fired concurrently.
    await waitFor(() => expect(inFlight).toBe(4));
    expect(peak).toBe(4);

    await act(async () => {
      gate.resolve();
      await done;
    });

    for (const runId of runIds) {
      expect(result.current.loads[runId]?.state).toBe("done");
    }
    expect(peak).toBe(4);
  });

  it("loadRuns skips a run whose replay already started and does not replay it twice on a repeat call", async () => {
    streamRunEventsMock.mockImplementation(() =>
      makeStream([
        {
          id: "1",
          event: "updates",
          data: { agent: { messages: [{ type: "ai", content: "replayed" }] } },
          rawData: "",
          receivedAt: "",
        },
        { id: "2", event: "end", data: {}, rawData: "", receivedAt: "" },
      ]),
    );

    const { result } = renderHook(() => useHistoryTurns());

    // r-started's replay was already triggered by scrolling its row into view.
    const row = document.createElement("div");
    await act(async () => {
      result.current.registerRow("r-started", "th-1")(row);
    });
    await waitFor(() => expect(result.current.loads["r-started"]?.state).toBe("done"));
    streamRunEventsMock.mockClear();

    await act(async () => {
      await result.current.loadRuns(["r-started", "r-new"], "th-1");
    });
    expect(streamRunEventsMock).toHaveBeenCalledTimes(1);
    expect(streamRunEventsMock).toHaveBeenCalledWith(
      "th-1",
      "r-new",
      expect.anything(),
    );
    expect(result.current.loads["r-new"]?.state).toBe("done");

    // A second loadRuns call for the same runs must not replay either again.
    await act(async () => {
      await result.current.loadRuns(["r-started", "r-new"], "th-1");
    });
    expect(streamRunEventsMock).toHaveBeenCalledTimes(1);
  });

  it("loadRuns marks a failing run's load as error, leaves the rest done, and resolves without throwing", async () => {
    streamRunEventsMock.mockImplementation((_threadId: string, runId: string) => {
      if (runId === "r-fail") {
        return (async function* () {
          throw new Error("boom");
        })();
      }
      return makeStream([
        {
          id: "1",
          event: "updates",
          data: { agent: { messages: [{ type: "ai", content: "replayed" }] } },
          rawData: "",
          receivedAt: "",
        },
        { id: "2", event: "end", data: {}, rawData: "", receivedAt: "" },
      ]);
    });

    const { result } = renderHook(() => useHistoryTurns());

    await expect(
      act(async () => {
        await result.current.loadRuns(["r-ok", "r-fail"], "th-1");
      }),
    ).resolves.not.toThrow();

    expect(result.current.loads["r-ok"]?.state).toBe("done");
    expect(result.current.loads["r-fail"]?.state).toBe("error");
  });

  it("drops a stale load's result when a newer load superseded it", async () => {
    const slow = deferred<HistoryMessage[]>();
    getMessagesMock.mockReturnValueOnce(slow.promise);
    getMessagesMock.mockResolvedValueOnce([
      { role: "user", content: "fresh" },
      { role: "assistant", content: "fresh-a" },
    ]);
    listThreadRunsMock.mockResolvedValue([]);

    const { result } = renderHook(() => useHistoryTurns());

    let stale!: Promise<void>;
    act(() => {
      stale = result.current.load("th-stale");
    });
    await act(async () => {
      await result.current.load("th-fresh");
    });
    await act(async () => {
      slow.resolve([{ role: "user", content: "stale" }]);
      await stale;
    });

    expect(result.current.messages).toEqual([
      { role: "user", content: "fresh" },
      { role: "assistant", content: "fresh-a" },
    ]);
  });
});

// ---------------------------------------------------------------------------
// D-5 — non-terminal (live) replay
// ---------------------------------------------------------------------------

const liveThread = {
  messages: [
    { role: "user", content: "q1" },
    { role: "assistant", content: "a1" },
    { role: "user", content: "q2" },
  ] as HistoryMessage[],
  runs: [
    { runId: "r1", status: "success", isResume: false, createdAt: "2026-05-25T00:00:00Z", tokens: null },
    { runId: "r2", status: "running", isResume: true, createdAt: "2026-05-25T00:01:00Z", tokens: null },
  ],
};

const rafFlush = () =>
  act(async () => {
    await new Promise((r) => requestAnimationFrame(() => r(null)));
  });

describe("useHistoryTurns live attach (D-5)", () => {
  it("flushes a non-terminal run's frames incrementally as state 'live'", async () => {
    getMessagesMock.mockResolvedValue(liveThread.messages);
    listThreadRunsMock.mockResolvedValue(liveThread.runs as never);
    const gate = deferred<void>();
    streamRunEventsMock.mockImplementation((_t, runId) =>
      runId === "r2"
        ? (async function* () {
            yield { id: "1", event: "metadata", data: { run_id: "r2" } } as SseEvent;
            await gate.promise; // stream stays open — no end frame yet
          })()
        : makeStream([]),
    );

    const { result } = renderHook(() => useHistoryTurns());
    await act(async () => {
      await result.current.load("th-1");
    });
    expect(result.current.turns).toHaveLength(2);

    act(() => {
      void result.current.loadRuns(["r2"], "th-1");
    });
    await rafFlush();
    expect(result.current.loads.r2).toEqual({
      state: "live",
      events: [{ id: "1", event: "metadata", data: { run_id: "r2" } }],
    });
    gate.resolve();
  });

  it("commits 'done' + fires onRunTerminal when the live stream delivers end", async () => {
    getMessagesMock.mockResolvedValue(liveThread.messages);
    listThreadRunsMock.mockResolvedValue(liveThread.runs as never);
    streamRunEventsMock.mockImplementation((_t, runId) =>
      runId === "r2"
        ? makeStream([
            { id: "1", event: "updates", data: { step: 1 } } as SseEvent,
            { id: "2", event: "end", data: { status: "success" } } as SseEvent,
          ])
        : makeStream([]),
    );
    const onRunTerminal = vi.fn();

    const { result } = renderHook(() => useHistoryTurns({ onRunTerminal }));
    await act(async () => {
      await result.current.load("th-1");
    });
    await act(async () => {
      await result.current.loadRuns(["r2"], "th-1");
    });

    expect(result.current.loads.r2.state).toBe("done");
    expect(result.current.loads.r2.events).toHaveLength(2);
    expect(onRunTerminal).toHaveBeenCalledExactlyOnceWith("r2");
    // A flush queued just before the end frame must not stamp it back to
    // "live" after the commit.
    await rafFlush();
    expect(result.current.loads.r2.state).toBe("done");
  });

  it("keeps a closed-without-end live stream as 'live' with the collected frames", async () => {
    getMessagesMock.mockResolvedValue(liveThread.messages);
    listThreadRunsMock.mockResolvedValue(liveThread.runs as never);
    streamRunEventsMock.mockImplementation((_t, runId) =>
      runId === "r2"
        ? makeStream([{ id: "1", event: "updates", data: { step: 1 } } as SseEvent])
        : makeStream([]),
    );
    const onRunTerminal = vi.fn();

    const { result } = renderHook(() => useHistoryTurns({ onRunTerminal }));
    await act(async () => {
      await result.current.load("th-1");
    });
    await act(async () => {
      await result.current.loadRuns(["r2"], "th-1");
    });

    // The run is still in flight server-side — no terminal signal, no wipe.
    expect(result.current.loads.r2.state).toBe("live");
    expect(result.current.loads.r2.events).toHaveLength(1);
    expect(onRunTerminal).not.toHaveBeenCalled();
    await rafFlush();
    expect(result.current.loads.r2.state).toBe("live");
  });

  it("terminal runs keep the collect-then-commit path (no live state ever)", async () => {
    getMessagesMock.mockResolvedValue(oneTurnOfMessages);
    listThreadRunsMock.mockResolvedValue(oneRun);
    streamRunEventsMock.mockImplementation(() =>
      makeStream([
        { id: "1", event: "updates", data: {} } as SseEvent,
        { id: "2", event: "end", data: {} } as SseEvent,
      ]),
    );
    const { result } = renderHook(() => useHistoryTurns());
    await act(async () => {
      await result.current.load("th-1");
    });
    await act(async () => {
      await result.current.loadRuns(["r1"], "th-1");
    });
    expect(result.current.loads.r1.state).toBe("done");
  });
});

describe("patchRuns (D-5)", () => {
  it("flips an in-flight turn terminal but never rewrites an already-terminal one", async () => {
    getMessagesMock.mockResolvedValue(liveThread.messages);
    listThreadRunsMock.mockResolvedValue(liveThread.runs as never);

    const { result } = renderHook(() => useHistoryTurns());
    await act(async () => {
      await result.current.load("th-1");
    });

    act(() => {
      result.current.patchRuns([
        // r1 is terminal (success) — a disagreeing summary must NOT win.
        { runId: "r1", status: "error", tokens: null },
        // r2 is in flight — the patch flips it terminal.
        { runId: "r2", status: "success", tokens: null },
      ]);
    });
    expect(result.current.turns?.map((h) => [h.runId, h.status])).toEqual([
      ["r1", "success"],
      ["r2", "success"],
    ]);

    // Identity-stable when nothing changes.
    const before = result.current.turns;
    act(() => {
      result.current.patchRuns([{ runId: "r2", status: "success", tokens: null }]);
    });
    expect(result.current.turns).toBe(before);
  });
});
