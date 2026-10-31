/**
 * B-175 P3-PR3 — the maintenance SDK: each call's method, URL and body, the
 * reads' ``tenant_id`` passthrough, the 409 helpers, and the shared passage
 * fixture (the backend test checks every live passage carries its keys; this
 * side checks the keys the console reads are in it).
 */
import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it } from "vitest";
import type { AxiosAdapter, InternalAxiosRequestConfig } from "axios";

import { ApiError, apiClient } from "../client";
import { listDocuments } from "../knowledge";
import {
  addPassage,
  batchDeleteDocuments,
  batchDocuments,
  discardUnappliedEdits,
  getStaleDocuments,
  getUnappliedEdits,
  listPassageRevisions,
  listPassages,
  passageConflict,
  passageErrorCode,
  reingestStale,
  restorePassage,
  updatePassage,
  type Passage,
  type PassageChild,
  type PassagesPage,
} from "../knowledge_maintenance";

const TENANT = "22222222-2222-2222-2222-222222222222";
const DOC = "/v1/knowledge/bases/kb%201/documents/d1";

interface Capture {
  method: string;
  url: string;
  params: Record<string, unknown> | undefined;
  body: unknown;
}

const originalAdapter = apiClient.defaults.adapter;

function captureAdapter(response: unknown): Capture[] {
  const calls: Capture[] = [];
  const adapter: AxiosAdapter = (config: InternalAxiosRequestConfig) => {
    calls.push({
      method: (config.method ?? "").toUpperCase(),
      url: config.url ?? "",
      params: config.params as Record<string, unknown> | undefined,
      body: typeof config.data === "string" ? JSON.parse(config.data) : config.data,
    });
    return Promise.resolve({
      data: response,
      status: 200,
      statusText: "OK",
      headers: {},
      config,
      request: {},
    });
  };
  apiClient.defaults.adapter = adapter;
  return calls;
}

afterEach(() => {
  apiClient.defaults.adapter = originalAdapter;
});

describe("knowledge maintenance SDK — requests", () => {
  it("lists passages with paging and the tenant scope, and without it when unscoped", async () => {
    let calls = captureAdapter({ passages: [], figure_ids: [], total: 0, offset: 20, limit: 20 });
    await listPassages("kb 1", "d1", { offset: 20, limit: 20 }, TENANT);
    expect(calls[0]).toMatchObject({ method: "GET", url: `${DOC}/passages` });
    expect(calls[0].params).toEqual({ offset: 20, limit: 20, tenant_id: TENANT });

    calls = captureAdapter({ passages: [], figure_ids: [], total: 0, offset: 0, limit: 20 });
    await listPassages("kb 1", "d1", { offset: 0, limit: 20 });
    expect(calls[0].params).toEqual({ offset: 0, limit: 20 });
  });

  it("edits text with the revision it was read at, and switches with enabled alone", async () => {
    const calls = captureAdapter({});
    await updatePassage("kb 1", "d1", "p1", { content: "New.", revision: 2 });
    await updatePassage("kb 1", "d1", "p1", { enabled: false });
    expect(calls.map((c) => [c.method, c.url, c.body])).toEqual([
      ["PATCH", `${DOC}/passages/p1`, { content: "New.", revision: 2 }],
      ["PATCH", `${DOC}/passages/p1`, { enabled: false }],
    ]);
  });

  it("adds a passage with its heading path", async () => {
    const calls = captureAdapter({});
    await addPassage("kb 1", "d1", { content: "Added.", heading_path: ["A", "B"] });
    expect(calls[0]).toMatchObject({
      method: "POST",
      url: `${DOC}/passages`,
      body: { content: "Added.", heading_path: ["A", "B"] },
    });
  });

  it("restores a revision against the revision the passage was read at", async () => {
    const calls = captureAdapter({});
    await restorePassage("kb 1", "d1", "p1", 1, 3);
    expect(calls[0]).toMatchObject({
      method: "POST",
      url: `${DOC}/passages/p1/restore`,
      body: { revision: 1, expected_revision: 3 },
    });
  });

  it("reads revisions, unapplied edits and the stale count with the tenant scope", async () => {
    const calls = captureAdapter({ revisions: [], unapplied: [], inherited: [], stale: 0 });
    await listPassageRevisions("kb 1", "d1", "p1", TENANT);
    await getUnappliedEdits("kb 1", "d1", TENANT);
    await getStaleDocuments("kb 1", TENANT);
    expect(calls.map((c) => [c.url, c.params?.tenant_id])).toEqual([
      [`${DOC}/passages/p1/revisions`, TENANT],
      [`${DOC}/unapplied-edits`, TENANT],
      ["/v1/knowledge/bases/kb%201/stale-documents", TENANT],
    ]);
  });

  it("discards unapplied edits and answers how many entries went (not revision rows)", async () => {
    const calls = captureAdapter({ discarded: 2, revisions_deleted: 9 });
    expect(await discardUnappliedEdits("kb 1", "d1")).toBe(2);
    expect(calls[0]).toMatchObject({ method: "DELETE", url: `${DOC}/unapplied-edits` });
  });

  it("asks the document list for each row's review only when told to", async () => {
    const calls = captureAdapter({ documents: [], total: 0, offset: 0, limit: 50 });
    await listDocuments("kb 1", { limit: 50, offset: 0 }, TENANT, { withReview: true });
    await listDocuments("kb 1", { limit: 50, offset: 0 }, TENANT);
    await listDocuments("kb 1", { limit: 50, offset: 0 }, undefined, { withReview: false });
    expect(calls.map((c) => c.params)).toEqual([
      { limit: 50, offset: 0, with_review: true, tenant_id: TENANT },
      { limit: 50, offset: 0, tenant_id: TENANT },
      { limit: 50, offset: 0 },
    ]);
  });

  it("posts batch actions, batch deletes and the stale re-ingest", async () => {
    const calls = captureAdapter({ done: [], skipped: [], queued: [], remaining: 0 });
    await batchDocuments("kb 1", "disable", ["d1", "d2"]);
    await batchDeleteDocuments("kb 1", ["d3"]);
    await reingestStale("kb 1");
    expect(calls.map((c) => [c.method, c.url, c.body])).toEqual([
      [
        "POST",
        "/v1/knowledge/bases/kb%201/documents/batch",
        { action: "disable", document_ids: ["d1", "d2"] },
      ],
      ["POST", "/v1/knowledge/bases/kb%201/documents/batch-delete", { document_ids: ["d3"] }],
      ["POST", "/v1/knowledge/bases/kb%201/reingest-stale", undefined],
    ]);
  });
});

describe("knowledge maintenance SDK — refusals", () => {
  const passage = { id: "p1", revision: 3 } as Passage;

  it("passageConflict returns the server's passage for a 409 revision_conflict only", () => {
    const conflict = new ApiError("x", "HTTP_409", 409, { error: "revision_conflict", passage });
    expect(passageConflict(conflict)).toEqual(passage);
    const busy = new ApiError("x", "HTTP_409", 409, { error: "document_busy" });
    expect(passageConflict(busy)).toBeNull();
    const other = new ApiError("x", "HTTP_422", 422, { error: "revision_conflict", passage });
    expect(passageConflict(other)).toBeNull();
    expect(passageConflict(new Error("boom"))).toBeNull();
  });

  it("passageErrorCode reads the refusal code from a structured detail", () => {
    const tooLong = new ApiError("x", "HTTP_422", 422, {
      error: "passage_too_long",
      limit_tokens: 2048,
    });
    expect(passageErrorCode(tooLong)).toBe("passage_too_long");
    expect(passageErrorCode(new ApiError("x", "HTTP_404", 404))).toBeNull();
  });
});

describe("shared passage fixture", () => {
  // Every key the console reads off a passage / a chunk under it.
  const PASSAGE_KEYS = [
    "id",
    "kind",
    "chunk_index",
    "content",
    "heading_path",
    "enabled",
    "disabled_reason",
    "edited",
    "revision",
    "children",
  ] as const satisfies readonly (keyof Passage)[];
  const CHILD_KEYS = [
    "id",
    "chunk_index",
    "content",
    "heading_path",
    "enabled",
    "disabled_reason",
  ] as const satisfies readonly (keyof PassageChild)[];
  const PAGE_KEYS = [
    "passages",
    "figure_ids",
    "total",
    "offset",
    "limit",
  ] as const satisfies readonly (keyof PassagesPage)[];

  it("carries every key the console reads", () => {
    const fixture = JSON.parse(
      readFileSync("src/api/__fixtures__/knowledge_passages.json", "utf8"),
    ) as Record<string, unknown> & { passages: Record<string, unknown>[] };
    expect(Object.keys(fixture)).toEqual(expect.arrayContaining([...PAGE_KEYS]));
    for (const passage of fixture.passages) {
      expect(Object.keys(passage)).toEqual(expect.arrayContaining([...PASSAGE_KEYS]));
      for (const child of passage.children as Record<string, unknown>[]) {
        expect(Object.keys(child)).toEqual(expect.arrayContaining([...CHILD_KEYS]));
      }
    }
  });
});
