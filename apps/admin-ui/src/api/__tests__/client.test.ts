/**
 * Unit tests for ``api/client`` — Stream H.1b PR 1.
 *
 * Covers the two pieces the UI relies on for Stream N integration:
 *
 *   1. ``withTenantScope`` — omits ``tenant_id`` for the home view,
 *      injects ``"*"`` for cross-tenant, injects a specific UUID for
 *      a tenant switch.
 *   2. ``unwrap`` — surfaces ``error.code`` / ``error.message`` from
 *      the envelope as :class:`ApiError`.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AxiosRequestConfig } from "axios";

import {
  ApiError,
  createClient,
  setStoredToken,
  setUnauthorizedHandler,
  unwrap,
  withTenantScope,
} from "../client";

describe("withTenantScope", () => {
  it("returns params unchanged when scope is undefined", () => {
    const got = withTenantScope({ limit: 50 }, undefined);
    expect(got).toEqual({ limit: 50 });
    expect("tenant_id" in got).toBe(false);
  });

  it("injects tenant_id=* for cross-tenant", () => {
    const got = withTenantScope({ status: "active" }, "*");
    expect(got).toEqual({ status: "active", tenant_id: "*" });
  });

  it("injects the specific UUID for a tenant switch", () => {
    const tenantId = "00000000-0000-0000-0000-0000000000a1";
    const got = withTenantScope({}, tenantId);
    expect(got).toEqual({ tenant_id: tenantId });
  });
});

describe("unwrap", () => {
  it("returns data when success=true", () => {
    expect(unwrap({ success: true, data: { x: 1 }, error: null })).toEqual({ x: 1 });
  });

  it("throws ApiError carrying code+message when success=false", () => {
    expect(() =>
      unwrap({
        success: false,
        data: null,
        error: { code: "CROSS_TENANT_FORBIDDEN", message: "nope" },
      }),
    ).toThrowError(ApiError);
  });

  it("throws when data is null even with success=true (defensive)", () => {
    expect(() => unwrap({ success: true, data: null, error: null })).toThrowError(
      "envelope.data was null",
    );
  });
});

// ② 401 → 会话过期:拦截器通知注册的 handler(AuthContext 挂载时注册清会话
// 流程)。axios 自定义 adapter 直接拒绝,拦截器照常跑。
function rejectWithStatus(status: number) {
  return (config: AxiosRequestConfig) => {
    const error = new Error(`HTTP ${status}`) as Error & {
      isAxiosError: boolean;
      response: { status: number; data: unknown };
      config: AxiosRequestConfig;
    };
    error.isAxiosError = true;
    error.response = {
      status,
      data: { detail: { code: `HTTP_${status}_CODE`, message: "boom" } },
    };
    error.config = config;
    return Promise.reject(error);
  };
}

// ③ B-9 — 422 的两种形状。控制台路由(含 /v1/agents 下的 console_only 路由)
// 必须回 FastAPI 自己的 ``{ detail: [...] }``:拦截器读的就是 ``data.detail``,
// 字段级错误经 ``ApiError.details`` 透出;第三方信封里没有 ``detail`` 可读,
// 只能退化成 ``HTTP_422`` + axios 通用文案 —— 这正是后端不能给控制台路由
// 套信封的原因。
function rejectWithData(status: number, data: unknown) {
  return (config: AxiosRequestConfig) => {
    const error = new Error(`HTTP ${status}`) as Error & {
      isAxiosError: boolean;
      response: { status: number; data: unknown };
      config: AxiosRequestConfig;
    };
    error.isAxiosError = true;
    error.response = { status, data };
    error.config = config;
    return Promise.reject(error);
  };
}

describe("422 detail shapes (B-9)", () => {
  const fastapiDetail = [
    {
      type: "int_parsing",
      loc: ["query", "limit"],
      msg: "Input should be a valid integer, unable to parse string as an integer",
      input: "abc",
    },
  ];

  it("surfaces FastAPI's bare detail list from a console route under /v1/agents", async () => {
    await expect(
      createClient().get("/v1/agents/support-bot/1.0.0/revisions", {
        adapter: rejectWithData(422, { detail: fastapiDetail }),
      }),
    ).rejects.toMatchObject({ status: 422, code: "HTTP_422", details: fastapiDetail });
  });

  it("finds nothing to read in the third-party envelope (why console routes stay bare)", async () => {
    const err = await createClient()
      .get("/v1/agents/support-bot/1.0.0/revisions", {
        adapter: rejectWithData(422, {
          success: false,
          data: null,
          error: { code: "INVALID_REQUEST", message: "Input should be a valid integer" },
        }),
      })
      .catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    const apiErr = err as ApiError;
    expect(apiErr.status).toBe(422);
    expect(apiErr.code).toBe("HTTP_422");
    expect(apiErr.details).toBeUndefined();
    // The envelope's own code/message never reach the caller.
    expect(apiErr.message).not.toContain("valid integer");
  });
});

describe("401 unauthorized handler", () => {
  afterEach(() => {
    setUnauthorizedHandler(null);
    setStoredToken(null);
  });

  it("fires the registered handler and still surfaces the ApiError", async () => {
    setStoredToken("tok");
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    await expect(
      createClient().get("/v1/agents", { adapter: rejectWithStatus(401) }),
    ).rejects.toMatchObject({ code: "HTTP_401_CODE", status: 401 });
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("fires once for concurrent 401s when the handler clears the session (debounce)", async () => {
    setStoredToken("tok");
    // 与 AuthContext 注册的 handler 同款:第一件事就是清 token。
    const handler = vi.fn(() => setStoredToken(null));
    setUnauthorizedHandler(handler);
    const client = createClient();
    const results = await Promise.allSettled([
      client.get("/v1/agents", { adapter: rejectWithStatus(401) }),
      client.get("/v1/sessions", { adapter: rejectWithStatus(401) }),
    ]);
    expect(results.every((r) => r.status === "rejected")).toBe(true);
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("does not fire without a stored token (pre-auth endpoints cannot loop)", async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    await expect(
      createClient().get("/v1/setup/status", { adapter: rejectWithStatus(401) }),
    ).rejects.toBeInstanceOf(ApiError);
    expect(handler).not.toHaveBeenCalled();
  });

  it("does not fire on non-401 errors", async () => {
    setStoredToken("tok");
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    await expect(
      createClient().get("/v1/agents", { adapter: rejectWithStatus(500) }),
    ).rejects.toBeInstanceOf(ApiError);
    expect(handler).not.toHaveBeenCalled();
  });
});
