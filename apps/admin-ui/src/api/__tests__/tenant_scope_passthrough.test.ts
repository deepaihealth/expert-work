/**
 * Track C W2 — SDK ``tenantScope`` 可选末参透传。
 *
 * 系统管理员切入目标租户后,详情页读接口要带 ``?tenant_id=``。逐个断言
 * 请求 query:给了 scope 就拼 ``tenant_id``,undefined 则不拼(既有调用
 * 零破坏)。capture-adapter 写法照 sdks.test.ts。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import type { InternalAxiosRequestConfig } from "axios";

import { apiClient } from "../client";
import { getAgent, getRevision, listRevisions } from "../agents";
import { downloadArtifact, listArtifactVersions } from "../artifacts";
import { getRun, streamRunEvents } from "../runs";
import { fetchRunTraceRaw, getRunTrace } from "../trace_facade";

const TENANT = "22222222-2222-2222-2222-222222222222";

interface Capture {
  url: string;
  params: Record<string, unknown> | undefined;
}

function captureAdapter(body: unknown, headers: Record<string, string> = {}) {
  const calls: Capture[] = [];
  apiClient.defaults.adapter = (config: InternalAxiosRequestConfig) => {
    calls.push({
      url: config.url ?? "",
      params: config.params as Record<string, unknown> | undefined,
    });
    return Promise.resolve({
      data: body,
      status: 200,
      statusText: "OK",
      headers,
      config,
      request: {},
    });
  };
  return calls;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("agents SDK — tenantScope passthrough", () => {
  it("getAgent threads tenant_id when scoped and omits it when not", async () => {
    const body = { success: true, data: { record: {} }, error: null };
    let calls = captureAdapter(body);
    await getAgent("a", "1.0", TENANT);
    expect(calls[0].url).toBe("/v1/agents/a/1.0");
    expect(calls[0].params?.tenant_id).toBe(TENANT);

    calls = captureAdapter(body);
    await getAgent("a", "1.0");
    expect(calls[0].params?.tenant_id).toBeUndefined();
  });

  it("listRevisions threads tenant_id", async () => {
    const calls = captureAdapter({ success: true, data: { items: [] }, error: null });
    await listRevisions("a", "1.0", TENANT);
    expect(calls[0].url).toBe("/v1/agents/a/1.0/revisions");
    expect(calls[0].params?.tenant_id).toBe(TENANT);
  });

  it("getRevision threads tenant_id", async () => {
    const calls = captureAdapter({ success: true, data: { record: {} }, error: null });
    await getRevision("a", "1.0", 3, TENANT);
    expect(calls[0].url).toBe("/v1/agents/a/1.0/revisions/3");
    expect(calls[0].params?.tenant_id).toBe(TENANT);
  });
});

describe("runs SDK — tenantScope passthrough", () => {
  it("getRun threads tenant_id when scoped and omits it when not", async () => {
    const body = { run_id: "r1", thread_id: "t1", status: "success", pending_approval: null };
    let calls = captureAdapter(body);
    await getRun("t1", "r1", TENANT);
    expect(calls[0].url).toBe("/v1/sessions/t1/runs/r1");
    expect(calls[0].params?.tenant_id).toBe(TENANT);

    calls = captureAdapter(body);
    await getRun("t1", "r1");
    expect(calls[0].params?.tenant_id).toBeUndefined();
  });

  it("streamRunEvents appends tenant_id to the hand-built SSE URL", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      body: new ReadableStream({ start: (c) => c.close() }),
    });
    vi.stubGlobal("fetch", fetchMock);

    for await (const _ of streamRunEvents("t1", "r1", { tenantScope: TENANT })) {
      // empty stream — nothing yielded
    }
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe(
      `/v1/sessions/t1/runs/r1/events?tenant_id=${TENANT}`,
    );
  });

  it("streamRunEvents omits tenant_id when scope is undefined", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      body: new ReadableStream({ start: (c) => c.close() }),
    });
    vi.stubGlobal("fetch", fetchMock);

    for await (const _ of streamRunEvents("t1", "r1", { sinceSeq: 5 })) {
      // empty stream — nothing yielded
    }
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/sessions/t1/runs/r1/events?since_seq=5");
  });
});

describe("trace facade SDK — tenantScope passthrough", () => {
  it("getRunTrace threads tenant_id", async () => {
    const calls = captureAdapter({ status: "no_trace" });
    await getRunTrace("t1", "r1", TENANT);
    expect(calls[0].url).toBe("/v1/sessions/t1/runs/r1/trace");
    expect(calls[0].params?.tenant_id).toBe(TENANT);
  });

  it("fetchRunTraceRaw appends tenant_id to the hand-built query string", async () => {
    let calls = captureAdapter({ spanId: "s1", field: "input", content: "X" });
    await fetchRunTraceRaw("t1", "r1", "s1", "input", TENANT);
    expect(calls[0].url).toBe(
      `/v1/sessions/t1/runs/r1/trace/raw?span=s1&field=input&tenant_id=${TENANT}`,
    );

    calls = captureAdapter({ spanId: "s1", field: "input", content: "X" });
    await fetchRunTraceRaw("t1", "r1", "s1", "input");
    expect(calls[0].url).toBe("/v1/sessions/t1/runs/r1/trace/raw?span=s1&field=input");
  });
});

describe("artifacts SDK — tenantScope passthrough", () => {
  it("downloadArtifact threads tenant_id alongside name/user_id", async () => {
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn(() => "blob:x"),
      revokeObjectURL: vi.fn(),
    });
    const calls = captureAdapter(new Blob(["x"]));
    await downloadArtifact("report.md", "user-1", TENANT);
    expect(calls[0].url).toBe("/v1/artifacts/download");
    expect(calls[0].params).toMatchObject({
      name: "report.md",
      user_id: "user-1",
      tenant_id: TENANT,
    });
  });

  it("listArtifactVersions threads tenant_id and omits it when undefined", async () => {
    let calls = captureAdapter({ name: "report.md", versions: [] });
    await listArtifactVersions("report.md", "user-1", TENANT);
    expect(calls[0].url).toBe("/v1/artifacts/report.md/versions");
    expect(calls[0].params?.tenant_id).toBe(TENANT);

    calls = captureAdapter({ name: "report.md", versions: [] });
    await listArtifactVersions("report.md");
    expect(calls[0].params?.tenant_id).toBeUndefined();
  });
});
