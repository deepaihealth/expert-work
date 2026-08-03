/**
 * Track C W2 + W3 — SDK ``tenantScope`` 可选末参透传。
 *
 * 系统管理员切入目标租户后,读接口要带 ``?tenant_id=``。逐个断言
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
import { getBase, listBases, listChunks, listDocuments, testRetrieval } from "../knowledge";
import {
  exportSkillVersion,
  getSkill,
  getSupportingFile,
  listSkillVersions,
} from "../skills";
import { getEvalRun, getEvalRunCases, listEvalRuns } from "../eval_runs";
import { getCandidate } from "../curation";
import { listQualityDriftAlerts, listQualityScores } from "../quality";
import { getUsageCost, getUsageTokens } from "../usage";
import {
  downloadUserWorkspaceFile,
  getUserWorkspace,
  getUserWorkspaceFiles,
} from "../workspace";
import {
  listAvailableMcpServers,
  listMcpServers,
  listMcpServerTools,
} from "../mcp-servers";
import { listMembers } from "../members";

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

// ─── Track E W3 — 其余读面 SDK 透传 ─────────────────────────────────────

describe("knowledge SDK — tenantScope passthrough", () => {
  it("listBases threads tenant_id and omits it when undefined", async () => {
    let calls = captureAdapter({ bases: [] });
    await listBases(TENANT);
    expect(calls[0].url).toBe("/v1/knowledge/bases");
    expect(calls[0].params?.tenant_id).toBe(TENANT);

    calls = captureAdapter({ bases: [] });
    await listBases();
    expect(calls[0].params?.tenant_id).toBeUndefined();
  });

  it("getBase threads tenant_id", async () => {
    const calls = captureAdapter({ id: "b1", name: "kb" });
    await getBase("kb", TENANT);
    expect(calls[0].url).toBe("/v1/knowledge/bases/kb");
    expect(calls[0].params?.tenant_id).toBe(TENANT);
  });

  it("listDocuments threads tenant_id", async () => {
    const calls = captureAdapter({ documents: [] });
    await listDocuments("kb", TENANT);
    expect(calls[0].url).toBe("/v1/knowledge/bases/kb/documents");
    expect(calls[0].params?.tenant_id).toBe(TENANT);
  });

  it("listChunks threads tenant_id alongside paging params", async () => {
    const calls = captureAdapter({ chunks: [], total: 0, offset: 0, limit: 20 });
    await listChunks("kb", "d1", { offset: 5, limit: 20 }, TENANT);
    expect(calls[0].url).toBe("/v1/knowledge/bases/kb/documents/d1/chunks");
    expect(calls[0].params).toMatchObject({ offset: 5, limit: 20, tenant_id: TENANT });
  });

  it("testRetrieval threads tenant_id as a query param on the POST", async () => {
    const calls = captureAdapter({ query: "q", results: [], count: 0 });
    await testRetrieval("kb", { query: "q" }, TENANT);
    expect(calls[0].url).toBe("/v1/knowledge/bases/kb/test");
    expect(calls[0].params?.tenant_id).toBe(TENANT);
  });
});

describe("skills SDK — tenantScope passthrough", () => {
  it("getSkill threads tenant_id and omits it when undefined", async () => {
    let calls = captureAdapter({ id: "s1" });
    await getSkill("s1", TENANT);
    expect(calls[0].url).toBe("/v1/skills/s1");
    expect(calls[0].params?.tenant_id).toBe(TENANT);

    calls = captureAdapter({ id: "s1" });
    await getSkill("s1");
    expect(calls[0].params?.tenant_id).toBeUndefined();
  });

  it("listSkillVersions threads tenant_id", async () => {
    const calls = captureAdapter({ items: [] });
    await listSkillVersions("s1", TENANT);
    expect(calls[0].url).toBe("/v1/skills/s1/versions");
    expect(calls[0].params?.tenant_id).toBe(TENANT);
  });

  it("exportSkillVersion threads tenant_id", async () => {
    const calls = captureAdapter(new Blob(["zip"]));
    await exportSkillVersion("s1", 3, TENANT);
    expect(calls[0].url).toBe("/v1/skills/s1/versions/3/export");
    expect(calls[0].params?.tenant_id).toBe(TENANT);
  });

  it("getSupportingFile threads tenant_id", async () => {
    const calls = captureAdapter({ content: "", size: 0, mime: "text/plain" });
    await getSupportingFile("s1", 3, "scripts/run.py", TENANT);
    expect(calls[0].url).toBe("/v1/skills/s1/versions/3/supporting-files/scripts/run.py");
    expect(calls[0].params?.tenant_id).toBe(TENANT);
  });
});

describe("eval-runs SDK — tenantScope passthrough", () => {
  it("listEvalRuns threads tenant_id and omits it when undefined", async () => {
    let calls = captureAdapter({ items: [], total: 0 });
    await listEvalRuns({ tenantScope: TENANT, limit: 10 });
    expect(calls[0].url).toBe("/v1/eval-runs");
    expect(calls[0].params).toMatchObject({ limit: 10, tenant_id: TENANT });

    calls = captureAdapter({ items: [], total: 0 });
    await listEvalRuns();
    expect(calls[0].params?.tenant_id).toBeUndefined();
  });

  it("getEvalRun / getEvalRunCases thread tenant_id", async () => {
    let calls = captureAdapter({ id: "r1" });
    await getEvalRun("r1", TENANT);
    expect(calls[0].url).toBe("/v1/eval-runs/r1");
    expect(calls[0].params?.tenant_id).toBe(TENANT);

    calls = captureAdapter({ cases: [] });
    await getEvalRunCases("r1", TENANT);
    expect(calls[0].url).toBe("/v1/eval-runs/r1/cases");
    expect(calls[0].params?.tenant_id).toBe(TENANT);
  });
});

describe("curation SDK — tenantScope passthrough", () => {
  it("getCandidate threads tenant_id", async () => {
    const calls = captureAdapter({ id: "c1" });
    await getCandidate("c1", TENANT);
    expect(calls[0].url).toBe("/v1/curation/candidates/c1");
    expect(calls[0].params?.tenant_id).toBe(TENANT);
  });
});

describe("quality SDK — tenantScope passthrough", () => {
  it("listQualityScores threads tenant_id alongside filters", async () => {
    const calls = captureAdapter({ items: [] });
    await listQualityScores({ tenantScope: TENANT, agentName: "a", limit: 5 });
    expect(calls[0].url).toBe("/v1/quality/scores");
    expect(calls[0].params).toMatchObject({
      agent_name: "a",
      limit: 5,
      tenant_id: TENANT,
    });
  });

  it("listQualityDriftAlerts threads tenant_id and omits it when undefined", async () => {
    let calls = captureAdapter({ items: [] });
    await listQualityDriftAlerts({ tenantScope: TENANT });
    expect(calls[0].url).toBe("/v1/quality/drift-alerts");
    expect(calls[0].params?.tenant_id).toBe(TENANT);

    calls = captureAdapter({ items: [] });
    await listQualityDriftAlerts();
    expect(calls[0].params?.tenant_id).toBeUndefined();
  });
});

describe("usage SDK — tenantScope passthrough", () => {
  it("getUsageCost threads tenant_id alongside month/group_by", async () => {
    const body = { success: true, data: { groups: [] }, error: null };
    const calls = captureAdapter(body);
    await getUsageCost({ tenantScope: TENANT, month: "2026-07", groupBy: "agent" });
    expect(calls[0].url).toBe("/v1/usage/cost");
    expect(calls[0].params).toMatchObject({
      month: "2026-07",
      group_by: "agent",
      tenant_id: TENANT,
    });
  });

  it("getUsageTokens threads tenant_id and omits it when undefined", async () => {
    const body = { success: true, data: { total: {} }, error: null };
    let calls = captureAdapter(body);
    await getUsageTokens({ tenantScope: TENANT, userId: "u1" });
    expect(calls[0].url).toBe("/v1/usage/tokens");
    expect(calls[0].params).toMatchObject({ user_id: "u1", tenant_id: TENANT });

    calls = captureAdapter(body);
    await getUsageTokens();
    expect(calls[0].params?.tenant_id).toBeUndefined();
  });
});

describe("workspace SDK — tenantScope passthrough", () => {
  it("getUserWorkspace threads tenant_id alongside user_id", async () => {
    const body = { success: true, data: { workspace: null, artifacts: [] }, error: null };
    const calls = captureAdapter(body);
    await getUserWorkspace("u1", TENANT);
    expect(calls[0].url).toBe("/v1/workspace");
    expect(calls[0].params).toMatchObject({ user_id: "u1", tenant_id: TENANT });
  });

  it("getUserWorkspaceFiles threads tenant_id and omits it when undefined", async () => {
    const body = { success: true, data: { files: [] }, error: null };
    let calls = captureAdapter(body);
    await getUserWorkspaceFiles("u1", TENANT);
    expect(calls[0].url).toBe("/v1/workspace/files");
    expect(calls[0].params?.tenant_id).toBe(TENANT);

    calls = captureAdapter(body);
    await getUserWorkspaceFiles("u1");
    expect(calls[0].params?.tenant_id).toBeUndefined();
  });

  it("downloadUserWorkspaceFile appends tenant_id to the hand-built URL", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      blob: async () => new Blob(["x"]),
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn(() => "blob:x"),
      revokeObjectURL: vi.fn(),
    });

    await downloadUserWorkspaceFile("out/a.txt", "u1", TENANT);
    expect(fetchMock.mock.calls[0][0]).toBe(
      `/v1/workspace/file?path=out%2Fa.txt&user_id=u1&tenant_id=${TENANT}`,
    );
  });
});

describe("mcp-servers SDK — tenantScope passthrough", () => {
  it("listMcpServers threads tenant_id and omits it when undefined", async () => {
    const body = { success: true, data: [], error: null };
    let calls = captureAdapter(body);
    await listMcpServers(TENANT);
    expect(calls[0].url).toBe("/v1/mcp-servers");
    expect(calls[0].params?.tenant_id).toBe(TENANT);

    calls = captureAdapter(body);
    await listMcpServers();
    expect(calls[0].params?.tenant_id).toBeUndefined();
  });

  it("listAvailableMcpServers threads tenant_id", async () => {
    const calls = captureAdapter({ success: true, data: [], error: null });
    await listAvailableMcpServers(TENANT);
    expect(calls[0].url).toBe("/v1/mcp-servers/available");
    expect(calls[0].params?.tenant_id).toBe(TENANT);
  });

  it("listMcpServerTools threads tenant_id", async () => {
    const calls = captureAdapter({ success: true, data: [], error: null });
    await listMcpServerTools("srv", TENANT);
    expect(calls[0].url).toBe("/v1/mcp-servers/srv/tools");
    expect(calls[0].params?.tenant_id).toBe(TENANT);
  });
});

describe("members SDK — tenantScope passthrough", () => {
  const body = { success: true, data: { items: [], total: 0 }, error: null };

  it("listMembers threads a concrete tenant_id via tenantScope", async () => {
    const calls = captureAdapter(body);
    await listMembers({ tenantScope: TENANT, limit: 10 });
    expect(calls[0].url).toBe("/v1/members");
    expect(calls[0].params).toMatchObject({ limit: 10, tenant_id: TENANT });
  });

  it("legacy crossTenant:true still maps to tenant_id='*'", async () => {
    const calls = captureAdapter(body);
    await listMembers({ crossTenant: true });
    expect(calls[0].params?.tenant_id).toBe("*");
  });

  it("tenantScope wins over crossTenant and undefined omits tenant_id", async () => {
    let calls = captureAdapter(body);
    await listMembers({ tenantScope: TENANT, crossTenant: true });
    expect(calls[0].params?.tenant_id).toBe(TENANT);

    calls = captureAdapter(body);
    await listMembers({});
    expect(calls[0].params?.tenant_id).toBeUndefined();
  });
});
