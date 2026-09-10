/**
 * Conversations SDK — wire-level param tests (P-2 PR3).
 *
 * The page test mocks ``listConversations`` itself, so it can only prove the
 * page reaches the SDK with ``hasDownRated``; whether that camelCase option
 * actually turns into the ``has_down_rated`` query the backend reads is a
 * separate claim, and it needs the adapter to see it.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import type { InternalAxiosRequestConfig } from "axios";

import { apiClient } from "../client";
import { listConversations } from "../conversations";

function captureAdapter(): { params?: Record<string, unknown> }[] {
  const calls: { params?: Record<string, unknown> }[] = [];
  apiClient.defaults.adapter = (config: InternalAxiosRequestConfig) => {
    calls.push({ params: config.params as Record<string, unknown> | undefined });
    return Promise.resolve({
      data: { success: true, data: { items: [], total: 0, cross_tenant: false }, error: null },
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

describe("listConversations has_down_rated", () => {
  it("sends has_down_rated=true only when asked, and omits it otherwise", async () => {
    let calls = captureAdapter();
    await listConversations({ hasDownRated: true });
    expect(calls[0].params?.has_down_rated).toBe(true);

    // ``false`` is the backend default — naming it must not change the request
    // (an explicit ``false`` in the query would still be a filter the backend
    // has to parse, and every other boolean filter here omits it).
    calls = captureAdapter();
    await listConversations({ hasDownRated: false });
    expect(calls[0].params?.has_down_rated).toBeUndefined();

    calls = captureAdapter();
    await listConversations();
    expect(calls[0].params?.has_down_rated).toBeUndefined();
  });

  it("composes with has_error on the wire", async () => {
    const calls = captureAdapter();
    await listConversations({ hasDownRated: true, hasError: true });
    expect(calls[0].params?.has_down_rated).toBe(true);
    expect(calls[0].params?.has_error).toBe(true);
  });
});
