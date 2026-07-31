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
