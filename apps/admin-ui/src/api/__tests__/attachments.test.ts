/** B-295 —— ``GET /v1/sessions/{thread_id}/attachment``:ref 与 tenant_id 透传、
 *  渲染依据是服务端的 Content-Type、410 识别为「已过期或已删除」。 */
import { afterEach, describe, expect, it } from "vitest";
import axios, { AxiosError, type AxiosAdapter, type InternalAxiosRequestConfig } from "axios";

import {
  fetchSessionAttachment,
  isAttachmentGone,
  previewKindOf,
} from "../attachments";
import { ApiError, apiClient } from "../client";

const TENANT = "22222222-2222-2222-2222-222222222222";
const original = apiClient.defaults.adapter;

interface Capture {
  url: string;
  params: Record<string, unknown> | undefined;
  responseType: string | undefined;
}

function respondWith(status: number, headers: Record<string, string>, data: unknown): Capture[] {
  const calls: Capture[] = [];
  const adapter: AxiosAdapter = (config: InternalAxiosRequestConfig) => {
    calls.push({
      url: config.url ?? "",
      params: config.params as Record<string, unknown> | undefined,
      responseType: config.responseType,
    });
    const response = { data, status, statusText: "", headers, config, request: {} };
    if (status >= 400) {
      return Promise.reject(
        new AxiosError("failed", "ERR_BAD_REQUEST", config, {}, response),
      );
    }
    return Promise.resolve(response);
  };
  apiClient.defaults.adapter = adapter;
  return calls;
}

afterEach(() => {
  apiClient.defaults.adapter = original;
});

describe("fetchSessionAttachment", () => {
  it("asks for the chip's ref in that thread, with the tenant passthrough", async () => {
    const calls = respondWith(
      200,
      {
        "content-type": "image/png",
        "content-disposition":
          "inline; filename=\"____.png\"; filename*=UTF-8''%E5%BE%AE%E4%BF%A1.png",
      },
      new Blob(["x"]),
    );
    const ref = "expert_work://image/t/th/i.png";

    const got = await fetchSessionAttachment("th/1", ref, TENANT);

    expect(calls[0].url).toBe("/v1/sessions/th%2F1/attachment");
    expect(calls[0].params).toEqual({ ref, tenant_id: TENANT });
    expect(calls[0].responseType).toBe("blob");
    expect(got.contentType).toBe("image/png");
    // The save-as name is the server's RFC 5987 ``filename*``, decoded.
    expect(got.filename).toBe("微信.png");
  });

  it("omits tenant_id for the home tenant and normalises the content type", async () => {
    const calls = respondWith(200, { "content-type": "Text/Plain; charset=utf-8" }, new Blob([]));

    const got = await fetchSessionAttachment("th-1", "uploads/a.txt");

    expect(calls[0].params).toEqual({ ref: "uploads/a.txt" });
    expect(got.contentType).toBe("text/plain");
    // No Content-Disposition → the stored leaf of the ref.
    expect(got.filename).toBe("a.txt");
  });

  it("a 410 is recognised as gone; other failures are not", async () => {
    respondWith(410, {}, { detail: { code: "ATTACHMENT_GONE", message: "expired" } });
    const err = await fetchSessionAttachment("th-1", "uploads/a.txt").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(isAttachmentGone(err)).toBe(true);

    respondWith(404, {}, { detail: "session not found" });
    const notFound = await fetchSessionAttachment("th-1", "uploads/a.txt").catch((e: unknown) => e);
    expect(isAttachmentGone(notFound)).toBe(false);
    expect(isAttachmentGone(new Error("x"))).toBe(false);
    expect(axios.isAxiosError(notFound)).toBe(false);
  });
});

describe("previewKindOf", () => {
  it("previews only whitelisted server types", () => {
    expect(previewKindOf("image/jpeg")).toBe("image");
    expect(previewKindOf("application/pdf")).toBe("pdf");
    expect(previewKindOf("text/plain")).toBe("text");
    // The server sends md / csv as text/plain; nothing else is previewed.
    for (const unsafe of [
      "text/csv",
      "text/markdown",
      "image/svg+xml",
      "text/html",
      "application/octet-stream",
      "constructor",
    ]) {
      expect(previewKindOf(unsafe)).toBeNull();
    }
  });
});
