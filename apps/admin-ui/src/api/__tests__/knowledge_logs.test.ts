/**
 * Retrieval records SDK — wire-level tests (B-175 P2-PR4).
 *
 * The tab tests mock the SDK, so whether a camelCase filter really becomes
 * the query the backend reads (``zero_hit``, ``tenant_id``) needs the adapter.
 */
import { describe, expect, it } from "vitest";
import type { InternalAxiosRequestConfig } from "axios";

import { apiClient } from "../client";
import {
  getRetrievalLog,
  getRetrievalLogPassages,
  listRetrievalLogs,
} from "../knowledge_logs";

interface Call {
  url?: string;
  params?: Record<string, unknown>;
}

function captureAdapter(data: unknown): Call[] {
  const calls: Call[] = [];
  apiClient.defaults.adapter = (config: InternalAxiosRequestConfig) => {
    calls.push({ url: config.url, params: config.params as Record<string, unknown> | undefined });
    return Promise.resolve({
      data,
      status: 200,
      statusText: "OK",
      headers: {},
      config,
      request: {},
    });
  };
  return calls;
}

const PAGE = { logs: [], total: 0, offset: 0, limit: 20 };

describe("listRetrievalLogs", () => {
  it("sends only the filters that are set, in the backend's names", async () => {
    let calls = captureAdapter(PAGE);
    await listRetrievalLogs("kb one", {}, { limit: 20, offset: 40 });
    expect(calls[0].url).toBe("/v1/knowledge/bases/kb%20one/retrieval-logs");
    expect(calls[0].params).toEqual({ limit: 20, offset: 40 });

    calls = captureAdapter(PAGE);
    await listRetrievalLogs(
      "kb",
      {
        source: "agent",
        status: "error",
        zeroHit: true,
        since: "2026-10-01T00:00:00.000Z",
        until: "2026-10-02T00:00:00.000Z",
      },
      { limit: 20, offset: 0 },
      "tenant-b",
    );
    expect(calls[0].params).toEqual({
      limit: 20,
      offset: 0,
      source: "agent",
      status: "error",
      zero_hit: true,
      since: "2026-10-01T00:00:00.000Z",
      until: "2026-10-02T00:00:00.000Z",
      tenant_id: "tenant-b",
    });

    // ``false`` is the backend default: not sent.
    calls = captureAdapter(PAGE);
    await listRetrievalLogs("kb", { zeroHit: false }, { limit: 20, offset: 0 });
    expect(calls[0].params?.zero_hit).toBeUndefined();
  });
});

describe("one record", () => {
  it("reads the detail and the live passages under the same scope", async () => {
    let calls = captureAdapter({ id: "log-1" });
    await getRetrievalLog("kb", "log-1", "tenant-b");
    expect(calls[0].url).toBe("/v1/knowledge/bases/kb/retrieval-logs/log-1");
    expect(calls[0].params).toEqual({ tenant_id: "tenant-b" });

    const passages = [{ passage: null, hit: null }];
    calls = captureAdapter({ passages });
    await expect(getRetrievalLogPassages("kb", "log-1")).resolves.toEqual(passages);
    expect(calls[0].url).toBe("/v1/knowledge/bases/kb/retrieval-logs/log-1/passages");
    expect(calls[0].params).toEqual({});
  });
});
