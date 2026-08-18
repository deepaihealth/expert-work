import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

import { useTokenStream } from "../useTokenStream";
import type { SseEvent } from "../../../../api/sessions";

function contentFrame(step: number, text: string): SseEvent {
  return { id: null, event: "token", data: { step, channel: "content", text }, rawData: "", receivedAt: "t" };
}
function reasoningFrame(step: number, text: string): SseEvent {
  return { id: null, event: "token", data: { step, channel: "reasoning", text }, rawData: "", receivedAt: "t" };
}
function toolFrame(step: number, toolIndex: number, name: string): SseEvent {
  return {
    id: null,
    event: "token",
    data: { step, channel: "tool_args", tool_index: toolIndex, name },
    rawData: "",
    receivedAt: "t",
  };
}

// Deterministic rAF: capture the scheduled callback; tests flush it manually.
let rafCbs: FrameRequestCallback[] = [];
beforeEach(() => {
  rafCbs = [];
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
    rafCbs.push(cb);
    return rafCbs.length;
  });
  vi.stubGlobal("cancelAnimationFrame", () => {});
});
afterEach(() => vi.unstubAllGlobals());
function flushRaf(): void {
  const cbs = rafCbs;
  rafCbs = [];
  cbs.forEach((cb) => cb(0));
}

describe("useTokenStream", () => {
  it("accumulates content tokens per step after a rAF flush", () => {
    const { result } = renderHook(() => useTokenStream());
    act(() => result.current.reset());
    act(() => {
      result.current.push(contentFrame(0, "Hel"));
      result.current.push(contentFrame(0, "lo"));
    });
    expect(result.current.liveByStep.size).toBe(0); // batched
    act(() => flushRaf());
    expect(result.current.liveByStep.get(0)?.content).toBe("Hello");
  });

  it("accumulates reasoning tokens per step (separate channel)", () => {
    const { result } = renderHook(() => useTokenStream());
    act(() => result.current.reset());
    act(() => {
      result.current.push(reasoningFrame(0, "think"));
      result.current.push(reasoningFrame(0, "ing"));
    });
    act(() => flushRaf());
    expect(result.current.liveByStep.get(0)?.reasoning).toBe("thinking");
    expect(result.current.liveByStep.get(0)?.content).toBe("");
  });

  it("records tool names by index", () => {
    const { result } = renderHook(() => useTokenStream());
    act(() => result.current.reset());
    act(() => {
      result.current.push(toolFrame(0, 0, "search_web"));
      result.current.push(toolFrame(0, 1, "read_file"));
    });
    act(() => flushRaf());
    const names = result.current.liveByStep.get(0)?.toolNames;
    expect(names?.get(0)).toBe("search_web");
    expect(names?.get(1)).toBe("read_file");
  });

  it("coalesces many pushes into a single flush (one rAF scheduled)", () => {
    const { result } = renderHook(() => useTokenStream());
    act(() => result.current.reset());
    act(() => {
      result.current.push(contentFrame(0, "a"));
      result.current.push(reasoningFrame(0, "b"));
      result.current.push(toolFrame(0, 0, "t"));
    });
    expect(rafCbs.length).toBe(1); // batched, not 3
    act(() => flushRaf());
    expect(result.current.liveByStep.get(0)?.content).toBe("a");
  });

  it("ignores non-token frames and unknown channels", () => {
    const { result } = renderHook(() => useTokenStream());
    act(() => result.current.reset());
    act(() => {
      result.current.push({ id: null, event: "updates", data: {}, rawData: "", receivedAt: "t" });
      result.current.push({ id: null, event: "token", data: { step: 0, channel: "bogus", text: "x" }, rawData: "", receivedAt: "t" });
      result.current.push({ id: null, event: "token", data: { step: 0, channel: "content" }, rawData: "", receivedAt: "t" }); // no text
    });
    act(() => flushRaf());
    expect(result.current.liveByStep.size).toBe(0);
  });

  it("captures TTFT on the first token (any channel)", () => {
    vi.spyOn(Date, "now").mockReturnValueOnce(1000).mockReturnValueOnce(1250).mockReturnValue(1250);
    const { result } = renderHook(() => useTokenStream());
    act(() => result.current.reset()); // Date.now() → 1000 (start)
    act(() => result.current.push(reasoningFrame(0, "hmm"))); // Date.now() → 1250
    act(() => flushRaf());
    expect(result.current.ttftMs).toBe(250);
  });

  it("computes reasoningMs from reasoning-start to content-start", () => {
    // push(reasoning) calls Date.now() TWICE: once for ttft, once for
    // reasoningStart. push(content) calls it once (ttft already set) for
    // contentStart. Mock the exact call sequence.
    vi.spyOn(Date, "now")
      .mockReturnValueOnce(1000) // #1 reset → start
      .mockReturnValueOnce(1100) // #2 ttft base
      .mockReturnValueOnce(1100) // #3 reasoningStart
      .mockReturnValue(1900); // #4 contentStart (and any later)
    const { result } = renderHook(() => useTokenStream());
    act(() => result.current.reset());
    act(() => result.current.push(reasoningFrame(0, "r")));
    act(() => result.current.push(contentFrame(0, "c")));
    act(() => flushRaf());
    expect(result.current.liveByStep.get(0)?.reasoningMs).toBe(800); // 1900 - 1100
  });

  it("computes reasoningMs to finalize time for a reasoning-only step", () => {
    // push(reasoning) calls Date.now() twice (ttft, reasoningStart); finalize
    // calls it once (finalizeTime). Mock the exact sequence.
    vi.spyOn(Date, "now")
      .mockReturnValueOnce(1000) // #1 reset → start
      .mockReturnValueOnce(1100) // #2 ttft base
      .mockReturnValueOnce(1100) // #3 reasoningStart
      .mockReturnValue(1600); // #4 finalizeTime (and any later)
    const { result } = renderHook(() => useTokenStream());
    act(() => result.current.reset());
    act(() => result.current.push(reasoningFrame(0, "r")));
    act(() => result.current.finalize());
    expect(result.current.liveByStep.get(0)?.reasoningMs).toBe(500); // 1600 - 1100
    expect(result.current.finalized).toBe(true);
  });

  it("leaves reasoningMs null while still reasoning (no content yet)", () => {
    const { result } = renderHook(() => useTokenStream());
    act(() => result.current.reset());
    act(() => result.current.push(reasoningFrame(0, "r")));
    act(() => flushRaf());
    expect(result.current.liveByStep.get(0)?.reasoningMs).toBe(null);
  });

  it("finalize marks finalized and keeps the buffered text", () => {
    const { result } = renderHook(() => useTokenStream());
    act(() => result.current.reset());
    act(() => result.current.push(contentFrame(1, "partial")));
    act(() => result.current.finalize());
    expect(result.current.finalized).toBe(true);
    expect(result.current.liveByStep.get(1)?.content).toBe("partial");
  });

  it("reset clears buffers and finalized flag", () => {
    const { result } = renderHook(() => useTokenStream());
    act(() => result.current.reset());
    act(() => result.current.push(contentFrame(0, "x")));
    act(() => result.current.finalize());
    act(() => result.current.reset());
    expect(result.current.finalized).toBe(false);
    expect(result.current.liveByStep.size).toBe(0);
    expect(result.current.ttftMs).toBe(null);
  });

  it("reschedules a new rAF for a push after a flush (typewriter keeps flowing)", () => {
    const { result } = renderHook(() => useTokenStream());
    act(() => result.current.reset());
    act(() => result.current.push(contentFrame(0, "a")));
    expect(rafCbs.length).toBe(1);
    act(() => flushRaf());
    act(() => result.current.push(contentFrame(0, "b")));
    expect(rafCbs.length).toBe(1); // a NEW rAF was scheduled — handle reset, not stuck
    act(() => flushRaf());
    expect(result.current.liveByStep.get(0)?.content).toBe("ab");
  });

  it("a stale queued flush after finalize does not clobber the finalized snapshot", () => {
    const { result } = renderHook(() => useTokenStream());
    act(() => result.current.reset());
    act(() => result.current.push(contentFrame(0, "partial")));
    act(() => result.current.finalize());
    expect(result.current.finalized).toBe(true);
    act(() => flushRaf());
    expect(result.current.finalized).toBe(true);
    expect(result.current.liveByStep.get(0)?.content).toBe("partial");
  });

  it("records firstTokenAt / lastTokenAt (epoch ms) across channels and keeps them after finalize", () => {
    const now = vi.spyOn(Date, "now");
    now.mockReturnValue(1_000);
    const { result } = renderHook(() => useTokenStream());
    act(() => result.current.reset());
    now.mockReturnValue(1_250);
    act(() => result.current.push(reasoningFrame(1, "…")));
    now.mockReturnValue(2_000);
    act(() => result.current.push(contentFrame(1, "hi")));
    act(() => flushRaf());
    expect(result.current.firstTokenAt).toBe(1_250);
    expect(result.current.lastTokenAt).toBe(2_000);
    act(() => result.current.finalize());
    expect(result.current.firstTokenAt).toBe(1_250);
    expect(result.current.lastTokenAt).toBe(2_000);
    now.mockRestore();
  });

  it("reset clears firstTokenAt / lastTokenAt", () => {
    const { result } = renderHook(() => useTokenStream());
    act(() => result.current.reset());
    act(() => result.current.push(contentFrame(1, "a")));
    act(() => result.current.reset());
    expect(result.current.firstTokenAt).toBeNull();
    expect(result.current.lastTokenAt).toBeNull();
  });
});
