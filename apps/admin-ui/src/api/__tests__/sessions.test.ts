/**
 * Sessions SDK tests — Stream H.2 PR 3.
 *
 * Pure parser tests: the network layer is tested via the higher-level
 * ``PlaygroundTab`` component test, but the SSE frame parser is purely
 * functional and worth covering directly.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import type { InternalAxiosRequestConfig } from "axios";

import { apiClient } from "../client";
import { listSessions, parseSseStream } from "../sessions";

function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

async function collect<T>(it: AsyncIterable<T>): Promise<T[]> {
  const out: T[] = [];
  for await (const v of it) out.push(v);
  return out;
}

describe("parseSseStream", () => {
  it("parses one frame with id + event + JSON data", async () => {
    const body = streamOf([
      "id: 42\nevent: metadata\ndata: {\"run_id\":\"abc\"}\n\n",
    ]);
    const frames = await collect(parseSseStream(body));
    expect(frames).toHaveLength(1);
    expect(frames[0].id).toBe("42");
    expect(frames[0].event).toBe("metadata");
    expect(frames[0].data).toEqual({ run_id: "abc" });
  });

  it("yields multiple frames split across two chunks", async () => {
    const body = streamOf([
      "event: updates\ndata: {\"step\":1}\n",
      "\nevent: updates\ndata: {\"step\":2}\n\n",
    ]);
    const frames = await collect(parseSseStream(body));
    expect(frames).toHaveLength(2);
    expect(frames[0].data).toEqual({ step: 1 });
    expect(frames[1].data).toEqual({ step: 2 });
  });

  it("falls back to raw string when data isn't JSON", async () => {
    const body = streamOf(["event: end\ndata: bye\n\n"]);
    const frames = await collect(parseSseStream(body));
    expect(frames).toHaveLength(1);
    expect(frames[0].event).toBe("end");
    expect(frames[0].data).toBe("bye");
    expect(frames[0].rawData).toBe("bye");
  });

  it("defaults event to 'message' when the frame omits it", async () => {
    const body = streamOf(["data: {\"x\":1}\n\n"]);
    const frames = await collect(parseSseStream(body));
    expect(frames[0].event).toBe("message");
  });

  it("skips comment lines starting with ':'", async () => {
    const body = streamOf([": heartbeat\n\nevent: metadata\ndata: {}\n\n"]);
    const frames = await collect(parseSseStream(body));
    expect(frames).toHaveLength(1);
    expect(frames[0].event).toBe("metadata");
  });
});

// M9 — the `orderBy` union has to name the values the backend actually knows
// (`created_at`, not `created`). capture-adapter 写法照 sdks.test.ts /
// tenant_scope_passthrough.test.ts。
function captureAdapter(): { params: Record<string, unknown> | undefined }[] {
  const calls: { params: Record<string, unknown> | undefined }[] = [];
  apiClient.defaults.adapter = (config: InternalAxiosRequestConfig) => {
    calls.push({ params: config.params as Record<string, unknown> | undefined });
    return Promise.resolve({
      data: { success: true, data: { items: [] }, error: null },
      status: 200,
      statusText: "OK",
      headers: {},
      config,
      request: {},
    });
  };
  return calls;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("listSessions order_by", () => {
  it("sends order_by=last_activity, and omits it for the backend default created_at", async () => {
    let calls = captureAdapter();
    await listSessions({ orderBy: "last_activity" });
    expect(calls[0].params?.order_by).toBe("last_activity");

    // `created_at` is the backend's own default — naming it must not change
    // the request, but it must be a legal value of the union.
    calls = captureAdapter();
    await listSessions({ orderBy: "created_at" });
    expect(calls[0].params?.order_by).toBeUndefined();

    calls = captureAdapter();
    await listSessions();
    expect(calls[0].params?.order_by).toBeUndefined();
  });
});
