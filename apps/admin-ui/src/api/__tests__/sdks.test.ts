/**
 * SDK smoke tests — Stream H.1b PR 3.
 *
 * Each new SDK module is a thin wrapper that builds a URL + query and
 * unwraps the envelope. We assert that the wire shape (URL + params)
 * matches the backend contract; full request/response flow gets
 * exercised by the Playwright E2E in PR 4.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import type { InternalAxiosRequestConfig } from "axios";

import { apiClient } from "../client";
import { listAgents, getAgent } from "../agents";
import { getRun, resumeRun } from "../runs";
import { listSkills } from "../skills";
import { listTriggers } from "../triggers";
import { listMemories } from "../memory";
import { listCandidates, listEvalDatasets } from "../curation";
import {
  listApiKeys,
  createApiKey,
  revokeApiKey,
  rotateApiKey,
  listServiceAccounts,
} from "../api_keys";
import { listTenants } from "../tenants";
import {
  getPlatformEmbeddingConfig,
  getPlatformEmbeddingStatus,
} from "../platform_embedding_config";
import { getKillSwitch, getLineage, listEvalResults } from "../skill-evolution";

interface Capture {
  url: string;
  params: Record<string, unknown> | undefined;
  data: unknown;
  method: string;
}

function captureAdapter(body: unknown) {
  const calls: Capture[] = [];
  apiClient.defaults.adapter = (config: InternalAxiosRequestConfig) => {
    calls.push({
      url: config.url ?? "",
      params: config.params as Record<string, unknown> | undefined,
      data: config.data,
      method: (config.method ?? "get").toLowerCase(),
    });
    return Promise.resolve({
      data: body,
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

describe("listAgents — tenant scope threading", () => {
  it("omits tenant_id when tenantScope is undefined", async () => {
    const calls = captureAdapter({
      success: true,
      data: { items: [], total: 0, cross_tenant: false },
      error: null,
    });
    await listAgents();
    expect(calls[0].url).toBe("/v1/agents");
    expect(calls[0].params?.tenant_id).toBeUndefined();
  });

  it("threads tenant_id=* for system_admin", async () => {
    const calls = captureAdapter({
      success: true,
      data: { items: [], total: 0, cross_tenant: true },
      error: null,
    });
    await listAgents({ tenantScope: "*" });
    expect(calls[0].params?.tenant_id).toBe("*");
  });
});

describe("getAgent — URL encoding of name + version", () => {
  it("encodes path segments", async () => {
    const calls = captureAdapter({
      success: true,
      data: { record: { id: "1", tenant_id: "t", name: "a/b", version: "1.0", status: "active", spec_sha256: "x".repeat(64), spec: {}, created_by: "x", created_at: "", updated_at: "" } },
      error: null,
    });
    await getAgent("a/b", "1.0+rc.1");
    expect(calls[0].url).toBe("/v1/agents/a%2Fb/1.0%2Brc.1");
  });
});

describe("getRun — raw (un-enveloped) endpoint", () => {
  it("does not unwrap an envelope; returns the payload directly", async () => {
    captureAdapter({
      run_id: "r-1",
      thread_id: "t-1",
      status: "completed",
      pending_approval: null,
    });
    const result = await getRun("t-1", "r-1");
    expect(result.run_id).toBe("r-1");
    expect(result.status).toBe("completed");
  });
});

describe("resumeRun POSTs the approval body", () => {
  it("includes approved + reason in body", async () => {
    const calls = captureAdapter({
      run_id: "r-1",
      thread_id: "t-1",
      status: "running",
      pending_approval: null,
    });
    await resumeRun("t-1", "r-1", { decision: "approve", reason: "ship it" });
    expect(calls[0].url).toBe("/v1/sessions/t-1/runs/r-1/resume");
    expect(calls[0].method).toBe("post");
    const parsed = JSON.parse(calls[0].data as string);
    expect(parsed.decision).toBe("approve");
    expect(parsed.reason).toBe("ship it");
  });
});

describe("list endpoints share the envelope path", () => {
  it.each([
    ["memory", () => listMemories(), "/v1/memory"],
    ["api-keys", () => listApiKeys(), "/v1/api_keys"],
    ["service-accounts", () => listServiceAccounts(), "/v1/service_accounts"],
  ])("%s hits %s", async (_name, call, expectedUrl) => {
    const calls = captureAdapter({
      success: true,
      data: { items: [], total: 0, cross_tenant: false },
      error: null,
    });
    await call();
    expect(calls[0].url).toBe(expectedUrl);
  });
});

describe("raw (un-enveloped) list endpoints", () => {
  // H.4 latent-bug fixes (PR 1 + PR 5 + PR 6): curation, skills, and
  // triggers backends return raw ``JSONResponse(content={...})``
  // payloads, *not* enveloped ``{success, data}``. The SDK matches
  // that contract by going through ``apiClient`` directly.
  it.each([
    ["candidates", () => listCandidates(), "/v1/curation/candidates"],
    ["eval-datasets", () => listEvalDatasets(), "/v1/eval-datasets"],
    ["skills", () => listSkills(), "/v1/skills"],
    ["triggers", () => listTriggers(), "/v1/triggers"],
  ])("%s hits %s", async (_name, call, expectedUrl) => {
    const calls = captureAdapter({
      items: [],
      next_cursor: null,
      cross_tenant: false,
    });
    await call();
    expect(calls[0].url).toBe(expectedUrl);
  });
});

describe("getPlatformEmbeddingConfig — enveloped platform endpoint", () => {
  it("hits /v1/platform/embedding-config and unwraps the envelope", async () => {
    const calls = captureAdapter({
      success: true,
      data: {
        embedding: { provider: "openai", model: "text-embedding-3-small" },
        rerank: null,
        available_embedding: [
          { provider: "openai", model: "text-embedding-3-small" },
        ],
        available_rerank: [],
      },
      error: null,
    });
    const result = await getPlatformEmbeddingConfig();
    expect(calls[0].url).toBe("/v1/platform/embedding-config");
    expect(calls[0].method).toBe("get");
    expect(result.embedding?.provider).toBe("openai");
    expect(result.rerank).toBeNull();
  });

  it("getPlatformEmbeddingStatus calls the status endpoint", async () => {
    const calls = captureAdapter({
      success: true,
      data: { configured: true },
      error: null,
    });
    const out = await getPlatformEmbeddingStatus();
    expect(calls[0].url).toBe("/v1/platform/embedding-config/status");
    expect(calls[0].method).toBe("get");
    expect(out).toEqual({ configured: true });
  });
});

describe("listTenants — enveloped platform list endpoint", () => {
  it("listTenants calls GET /v1/tenants and unwraps data", async () => {
    const calls = captureAdapter({
      success: true,
      data: [
        {
          tenant_id: "t1",
          display_name: "Acme",
          plan: "free",
          created_at: "2026-01-01T00:00:00Z",
        },
      ],
      error: null,
    });
    const out = await listTenants();
    expect(calls[0].url).toMatch(/^\/v1\/tenants/);
    expect(calls[0].method).toBe("get");
    expect(out).toHaveLength(1);
    expect(out[0].display_name).toBe("Acme");
  });
});

describe("api_keys mutations", () => {
  it("createApiKey POSTs to the SA-scoped path", async () => {
    const calls = captureAdapter({
      success: true,
      data: {
        api_key: {
          id: "k1",
          service_account_id: "sa1",
          tenant_id: "t1",
          prefix: "aforge_pat_abc",
          scopes: ["read"],
          expires_at: null,
          last_used_at: null,
          revoked_at: null,
          rotated_at: null,
          grace_period_s: null,
          created_by: "u1",
          created_at: "2026-01-01T00:00:00Z",
        },
        plaintext: "aforge_pat_abc_xyz",
      },
      error: null,
    });
    await createApiKey("sa1", { scopes: ["read"] });
    expect(calls[0].url).toBe("/v1/service_accounts/sa1/api_keys");
    expect(calls[0].method).toBe("post");
  });

  it("revokeApiKey DELETEs the id-scoped path", async () => {
    const calls = captureAdapter("");
    await revokeApiKey("k1");
    expect(calls[0].url).toBe("/v1/api_keys/k1");
    expect(calls[0].method).toBe("delete");
  });

  it("rotateApiKey POSTs to /rotate", async () => {
    const calls = captureAdapter({
      success: true,
      data: {
        old: {
          id: "k1",
          service_account_id: "sa1",
          tenant_id: "t1",
          prefix: "aforge_pat_abc",
          scopes: ["read"],
          expires_at: null,
          last_used_at: null,
          revoked_at: null,
          rotated_at: "2026-01-01T00:00:00Z",
          grace_period_s: 300,
          created_by: "u1",
          created_at: "2025-12-01T00:00:00Z",
        },
        new: {
          api_key: {
            id: "k2",
            service_account_id: "sa1",
            tenant_id: "t1",
            prefix: "aforge_pat_def",
            scopes: ["read"],
            expires_at: null,
            last_used_at: null,
            revoked_at: null,
            rotated_at: null,
            grace_period_s: null,
            created_by: "u1",
            created_at: "2026-01-01T00:00:00Z",
          },
          plaintext: "aforge_pat_def_xyz",
        },
      },
      error: null,
    });
    await rotateApiKey("k1", { grace_period_s: 600 });
    expect(calls[0].url).toBe("/v1/api_keys/k1/rotate");
    expect(calls[0].method).toBe("post");
  });
});

describe("skill-evolution reads — tenant scope threading (跨租户钻取)", () => {
  // Raw payloads (no envelope) — the module reads ``response.data`` verbatim.
  it("threads tenant_id through listEvalResults", async () => {
    const calls = captureAdapter({ items: [] });
    await listEvalResults("sk-1", "t-1");
    expect(calls[0].url).toBe("/v1/skill-evolution/skills/sk-1/eval-results");
    expect(calls[0].params?.tenant_id).toBe("t-1");
  });

  it("threads tenant_id through getLineage", async () => {
    const calls = captureAdapter({ skill: null, forked_from_source: null, versions: [] });
    await getLineage("sk-1", "t-1");
    expect(calls[0].url).toBe("/v1/skill-evolution/skills/sk-1/lineage");
    expect(calls[0].params?.tenant_id).toBe("t-1");
  });

  it("threads tenant_id through getKillSwitch and omits it when unscoped", async () => {
    const calls = captureAdapter({ global: null, tenant: null, effective_halted: false });
    await getKillSwitch("t-1");
    expect(calls[0].url).toBe("/v1/skill-evolution/kill-switch");
    expect(calls[0].params?.tenant_id).toBe("t-1");
    await getKillSwitch();
    expect(calls[1].params?.tenant_id).toBeUndefined();
  });
});
