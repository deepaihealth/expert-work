/**
 * Trace facade SDK tests — Batch 4b.
 *
 * Tests for the getRunTrace SDK that consumes the raw-payload
 * endpoint GET /v1/sessions/{thread_id}/runs/{run_id}/trace.
 */
import { afterEach, describe, expect, it, vi, type Mock } from "vitest";

import { apiClient } from "../client";
import { fetchRunTraceRaw, getRunTrace, type RunTrace } from "../trace_facade";

vi.mock("../client");

afterEach(() => {
  vi.clearAllMocks();
});

describe("getRunTrace", () => {
  it("requests /v1/sessions/{threadId}/runs/{runId}/trace", async () => {
    const mockTrace: RunTrace = {
      status: "ok",
      trace: {
        name: "test-trace",
        latencyMs: 1000,
        totalCostUsd: 0.01,
        spanCount: 3,
      },
      spans: [],
    };

    (apiClient.get as Mock).mockResolvedValue({ data: mockTrace });

    const result = await getRunTrace("thread-123", "run-456");

    expect(apiClient.get).toHaveBeenCalledWith("/v1/sessions/thread-123/runs/run-456/trace");
    expect(result).toEqual(mockTrace);
  });

  it("returns status=ok with trace and spans", async () => {
    const mockTrace: RunTrace = {
      status: "ok",
      trace: {
        name: "agent-run",
        latencyMs: 5000,
        totalCostUsd: 0.05,
        spanCount: 2,
      },
      spans: [
        {
          id: "span-1",
          parentId: null,
          kind: "session",
          label: "session",
          detail: null,
          startMs: 0,
          latencyMs: 5000,
          model: null,
          inputTokens: null,
          outputTokens: null,
          costUsd: null,
          input: null,
          output: null,
          level: "default",
          statusMessage: null,
          purpose: "",
          group: null,
        },
        {
          id: "span-2",
          parentId: "span-1",
          kind: "llm",
          label: "claude-3-5-sonnet",
          detail: null,
          startMs: 100,
          latencyMs: 4900,
          model: "claude-3-5-sonnet-20241022",
          inputTokens: 100,
          outputTokens: 200,
          costUsd: 0.05,
          input: { kind: "text", text: '{"prompt":"hello"}', truncated: false, fullChars: 19 },
          output: { kind: "text", text: '{"response":"world"}', truncated: false, fullChars: 21 },
          level: "default",
          statusMessage: null,
          purpose: "",
          group: null,
        },
      ],
    };

    (apiClient.get as Mock).mockResolvedValue({ data: mockTrace });

    const result = await getRunTrace("thread-a", "run-b");

    expect(result.status).toBe("ok");
    expect(result.trace).toBeDefined();
    expect(result.spans).toHaveLength(2);
    expect(result.spans![0].kind).toBe("session");
    expect(result.spans![1].model).toBe("claude-3-5-sonnet-20241022");
  });

  it("returns status=not_ready without trace/spans", async () => {
    const mockTrace: RunTrace = {
      status: "not_ready",
    };

    (apiClient.get as Mock).mockResolvedValue({ data: mockTrace });

    const result = await getRunTrace("thread-x", "run-y");

    expect(result.status).toBe("not_ready");
    expect(result.trace).toBeUndefined();
    expect(result.spans).toBeUndefined();
  });

  it("returns status=unavailable without trace/spans", async () => {
    const mockTrace: RunTrace = {
      status: "unavailable",
    };

    (apiClient.get as Mock).mockResolvedValue({ data: mockTrace });

    const result = await getRunTrace("thread-p", "run-q");

    expect(result.status).toBe("unavailable");
    expect(result.trace).toBeUndefined();
    expect(result.spans).toBeUndefined();
  });

  it("returns status=no_trace without trace/spans", async () => {
    const mockTrace: RunTrace = {
      status: "no_trace",
    };

    (apiClient.get as Mock).mockResolvedValue({ data: mockTrace });

    const result = await getRunTrace("thread-m", "run-n");

    expect(result.status).toBe("no_trace");
    expect(result.trace).toBeUndefined();
    expect(result.spans).toBeUndefined();
  });
});

describe("fetchRunTraceRaw", () => {
  it("hits the raw endpoint and returns content", async () => {
    (apiClient.get as Mock).mockResolvedValue({
      data: { spanId: "o1", field: "input", content: "FULL" },
    });

    const result = await fetchRunTraceRaw("t1", "r1", "o1", "input");

    expect(apiClient.get).toHaveBeenCalledWith(
      "/v1/sessions/t1/runs/r1/trace/raw?span=o1&field=input",
    );
    expect(result).toBe("FULL");
  });

  it("URL-encodes the span id", async () => {
    (apiClient.get as Mock).mockResolvedValue({
      data: { spanId: "a/b c", field: "output", content: "RAW" },
    });

    const result = await fetchRunTraceRaw("t1", "r1", "a/b c", "output");

    expect(apiClient.get).toHaveBeenCalledWith(
      "/v1/sessions/t1/runs/r1/trace/raw?span=a%2Fb%20c&field=output",
    );
    expect(result).toBe("RAW");
  });
});
