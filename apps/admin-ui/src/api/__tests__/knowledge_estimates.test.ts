/**
 * B-222 PR-2 — the estimate endpoints' requests: list filters as repeated
 * query keys (FastAPI's list query), confirm by list / all with totals, and
 * 「仍入库」.
 */
import { afterEach, describe, expect, it } from "vitest";
import type { AxiosAdapter, InternalAxiosRequestConfig } from "axios";

import { apiClient } from "../client";
import {
  confirmAllDocuments,
  confirmDocuments,
  estimateDuplicate,
  listDocuments,
} from "../knowledge";

interface Capture {
  method: string;
  uri: string;
  data: unknown;
}

const original: AxiosAdapter | undefined = apiClient.defaults.adapter as AxiosAdapter | undefined;

function capture(body: unknown): Capture[] {
  const calls: Capture[] = [];
  apiClient.defaults.adapter = (config: InternalAxiosRequestConfig) => {
    calls.push({
      method: config.method ?? "",
      uri: apiClient.getUri({
        url: config.url,
        params: config.params,
        paramsSerializer: config.paramsSerializer,
      }),
      data: typeof config.data === "string" ? JSON.parse(config.data) : config.data,
    });
    return Promise.resolve({ data: body, status: 200, statusText: "OK", headers: {}, config });
  };
  return calls;
}

afterEach(() => {
  apiClient.defaults.adapter = original;
});

describe("estimate endpoints", () => {
  it("lists by status and document ids as repeated keys", async () => {
    const calls = capture({ documents: [], total: 0, offset: 0, limit: 50 });
    await listDocuments("kb", { limit: 50, offset: 0 }, undefined, {
      status: ["estimating", "awaiting_confirmation"],
      documentIds: ["d1", "d2"],
    });
    const query = new URL(calls[0].uri, "http://x").searchParams;
    expect(query.getAll("status")).toEqual(["estimating", "awaiting_confirmation"]);
    expect(query.getAll("document_id")).toEqual(["d1", "d2"]);
    expect(query.get("limit")).toBe("50");
  });

  it("sends no filter it was not given", async () => {
    const calls = capture({ documents: [], total: 0, offset: 0, limit: 50 });
    await listDocuments("kb", { limit: 50, offset: 0 });
    const query = new URL(calls[0].uri, "http://x").searchParams;
    expect(query.has("status")).toBe(false);
    expect(query.has("document_id")).toBe(false);
  });

  it("confirms by estimate id, or all with what the user saw", async () => {
    const calls = capture({ results: [], confirmed: [], withdrawn: [], estimate_summary: {} });
    await confirmDocuments("kb", [{ documentId: "d1", estimateId: "e1" }]);
    await confirmAllDocuments("kb", { count: 2, totalMicros: 120_000, withdrawDuplicateIds: ["d3"] });
    expect(calls.map((c) => [c.method, new URL(c.uri, "http://x").pathname, c.data])).toEqual([
      [
        "post",
        "/v1/knowledge/bases/kb/documents/confirm",
        { documents: [{ document_id: "d1", estimate_id: "e1" }] },
      ],
      [
        "post",
        "/v1/knowledge/bases/kb/documents/confirm",
        {
          all: true,
          expected_count: 2,
          expected_total_micros: 120_000,
          withdraw_duplicate_ids: ["d3"],
        },
      ],
    ]);
  });

  it("ingests a duplicate anyway", async () => {
    const calls = capture({ id: "d3" });
    await estimateDuplicate("kb", "d3");
    expect(new URL(calls[0].uri, "http://x").pathname).toBe(
      "/v1/knowledge/bases/kb/documents/d3/estimate",
    );
  });
});
