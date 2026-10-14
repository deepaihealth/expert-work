/**
 * AuthContext tests — Stream H.1b PR 2a.
 *
 * The interesting transitions are:
 *
 *   1. ``/v1/me`` succeeds → identity becomes server-resolved
 *      (``serverResolved=true``), with ``is_system_admin`` carried over.
 *   2. ``/v1/me`` returns 401 → token is dropped and the provider goes
 *      anonymous.
 *
 * The optimistic JWT-decode path is covered by the existing
 * ``_identityFromTokenForTests`` cases in :ref:`TenantSwitcher.test`.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, act } from "@testing-library/react";
import type { AxiosResponse, InternalAxiosRequestConfig } from "axios";

import {
  AuthProvider,
  useAuth,
  _identityFromMeForTests,
} from "../AuthContext";
import type { OidcEventHandlers } from "../oidc";
import { apiClient, getStoredToken, setStoredToken } from "../../api/client";

// Capture the OIDC event handlers so a test can play a silent renew.
// (Without OIDC env the real ``registerEvents`` is a no-op anyway.)
const oidcHandlers = vi.hoisted(() => ({ current: null as OidcEventHandlers | null }));
vi.mock("../oidc", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../oidc")>()),
  registerEvents: (handlers: OidcEventHandlers) => {
    oidcHandlers.current = handlers;
    return () => {};
  },
}));

afterEach(() => {
  setStoredToken(null);
});

function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.`;
}

function Identity() {
  const { identity, identitySettled, status } = useAuth();
  return (
    <div>
      <div data-testid="status">{status}</div>
      <div data-testid="server-resolved">{identity?.serverResolved ? "yes" : "no"}</div>
      <div data-testid="identity-settled">{identitySettled ? "yes" : "no"}</div>
      <div data-testid="is-sys-admin">{identity?.isSystemAdmin ? "yes" : "no"}</div>
      <div data-testid="tenant">{identity?.homeTenantId ?? "null"}</div>
    </div>
  );
}

function withMeResponse(payload: unknown, status = 200) {
  apiClient.defaults.adapter = (config) =>
    Promise.resolve({
      data: payload,
      status,
      statusText: status === 200 ? "OK" : "ERR",
      headers: {},
      config,
      request: {},
    } as unknown as AxiosResponse);
}

describe("AuthContext server-truth resolution", () => {
  it("upgrades optimistic identity with /v1/me data", async () => {
    setStoredToken(
      makeJwt({
        sub: "11111111-1111-1111-1111-111111111111",
        sub_type: "user",
        tenant_id: "22222222-2222-2222-2222-222222222222",
        roles: ["admin"],
      }),
    );
    withMeResponse({
      success: true,
      data: {
        subject_id: "11111111-1111-1111-1111-111111111111",
        subject_type: "user",
        tenant_id: "33333333-3333-3333-3333-333333333333",
        email: null,
        auth_method: "jwt",
        roles: ["system_admin"],
        scopes: [],
        is_system_admin: true,
        home_is_platform: false,
        allowed_tenants: "*",
      },
      error: null,
    });

    render(
      <AuthProvider>
        <Identity />
      </AuthProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("server-resolved").textContent).toBe("yes");
    });
    // The server's view of the principal wins — including the home
    // tenant rewrite (e.g. when the JWT and the role binding disagree).
    expect(screen.getByTestId("is-sys-admin").textContent).toBe("yes");
    expect(screen.getByTestId("tenant").textContent).toBe(
      "33333333-3333-3333-3333-333333333333",
    );
  });

  it("logs out when /v1/me returns 401", async () => {
    setStoredToken(makeJwt({ sub: "x", tenant_id: "t" }));
    // The interceptor's error branch wants an axios-flavoured error;
    // the simplest way is to reject from the adapter.
    apiClient.defaults.adapter = (config) => {
      const error = new Error("Unauthorized") as Error & {
        isAxiosError: boolean;
        response: { status: number; data: unknown };
        config: typeof config;
      };
      error.isAxiosError = true;
      error.response = {
        status: 401,
        data: { detail: { code: "AUTH_INVALID", message: "stale token" } },
      };
      error.config = config;
      return Promise.reject(error);
    };

    render(
      <AuthProvider>
        <Identity />
      </AuthProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("status").textContent).toBe("anonymous");
    });
    // Re-asserts a property of being unauthenticated: act-style noop to
    // keep React happy if any pending effects are scheduled.
    await act(async () => {});
  });

  // ② 会话过期 — 任意业务端点 401(不只 /v1/me)都要清会话回登录:
  // AuthProvider 挂载时向 client 注册清会话 handler。
  it("clears the session when any API call returns 401", async () => {
    setStoredToken(
      makeJwt({ sub: "x", sub_type: "user", tenant_id: "t", roles: ["admin"] }),
    );
    withMeResponse({
      success: true,
      data: {
        subject_id: "x",
        subject_type: "user",
        tenant_id: "t",
        email: null,
        auth_method: "jwt",
        roles: ["admin"],
        scopes: [],
        is_system_admin: false,
        home_is_platform: false,
        allowed_tenants: ["t"],
      },
      error: null,
    });

    render(
      <AuthProvider>
        <Identity />
      </AuthProvider>,
    );
    await waitFor(() => {
      expect(screen.getByTestId("server-resolved").textContent).toBe("yes");
    });

    // Token 服务端过期 — 某个普通业务请求撞 401。
    apiClient.defaults.adapter = (config) => {
      const error = new Error("Unauthorized") as Error & {
        isAxiosError: boolean;
        response: { status: number; data: unknown };
        config: typeof config;
      };
      error.isAxiosError = true;
      error.response = {
        status: 401,
        data: { detail: { code: "AUTH_INVALID", message: "expired" } },
      };
      error.config = config;
      return Promise.reject(error);
    };
    await act(async () => {
      await apiClient.get("/v1/agents").catch(() => {});
    });

    await waitFor(() => {
      expect(screen.getByTestId("status").textContent).toBe("anonymous");
    });
    expect(getStoredToken()).toBeNull();
  });
});

/** A ``/v1/me`` envelope for ``sub`` — a platform-homed system_admin. */
function adminMe(sub: string) {
  return {
    success: true,
    data: {
      subject_id: sub,
      subject_type: "user",
      tenant_id: "33333333-3333-3333-3333-333333333333",
      email: null,
      auth_method: "jwt",
      roles: [],
      scopes: [],
      is_system_admin: true,
      home_is_platform: true,
      allowed_tenants: "*",
    },
    error: null,
  };
}

function failWithStatus(status: number) {
  return (config: InternalAxiosRequestConfig) => {
    const error = new Error(`HTTP ${status}`) as Error & {
      isAxiosError: boolean;
      response: { status: number; data: unknown };
      config: typeof config;
    };
    error.isAxiosError = true;
    error.response = { status, data: { detail: { code: "BOOM", message: "down" } } };
    error.config = config;
    return Promise.reject(error);
  };
}

const ADMIN_SUB = "11111111-1111-1111-1111-111111111111";

// B-183 — the tenant scope waits on this session's /v1/me before pages read
// with a switched-in scope, so it must know when /v1/me has answered; and a
// silent renew must not throw the server truth away in the meantime.
describe("AuthContext identitySettled + silent renew (B-183)", () => {
  it("is unsettled while /v1/me is in flight and settled once it resolves", async () => {
    setStoredToken(makeJwt({ sub: ADMIN_SUB, tenant_id: "t" }));
    let answer: ((value: AxiosResponse) => void) | null = null;
    apiClient.defaults.adapter = () =>
      new Promise<AxiosResponse>((res) => {
        answer = res;
      });

    render(
      <AuthProvider>
        <Identity />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("status").textContent).toBe("authenticated"));
    expect(screen.getByTestId("identity-settled").textContent).toBe("no");

    await act(async () => {
      answer?.({
        data: adminMe(ADMIN_SUB),
        status: 200,
        statusText: "OK",
        headers: {},
        config: {},
      } as unknown as AxiosResponse);
    });
    await waitFor(() => expect(screen.getByTestId("identity-settled").textContent).toBe("yes"));
    expect(screen.getByTestId("server-resolved").textContent).toBe("yes");
  });

  it("settles without resolving when /v1/me fails (non-401) — nothing waits forever", async () => {
    setStoredToken(makeJwt({ sub: ADMIN_SUB, tenant_id: "t" }));
    apiClient.defaults.adapter = failWithStatus(503);

    render(
      <AuthProvider>
        <Identity />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("identity-settled").textContent).toBe("yes"));
    expect(screen.getByTestId("server-resolved").textContent).toBe("no");
    expect(screen.getByTestId("status").textContent).toBe("authenticated");
  });

  it("a silent renew of the same subject keeps the server-resolved identity while /v1/me re-checks", async () => {
    setStoredToken(makeJwt({ sub: ADMIN_SUB, tenant_id: "t", iat: 1 }));
    withMeResponse(adminMe(ADMIN_SUB));
    render(
      <AuthProvider>
        <Identity />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("server-resolved").textContent).toBe("yes"));

    // Renewed token, same subject; the re-check stays in flight.
    apiClient.defaults.adapter = () => new Promise<AxiosResponse>(() => {});
    const renewed = makeJwt({ sub: ADMIN_SUB, tenant_id: "t", iat: 2 });
    act(() => oidcHandlers.current?.onUserLoaded(renewed));

    expect(getStoredToken()).toBe(renewed);
    // Dropping back to the optimistic view here flips apiTenantScope to home
    // and back — every switched-in page re-reads twice per renew.
    expect(screen.getByTestId("server-resolved").textContent).toBe("yes");
    expect(screen.getByTestId("is-sys-admin").textContent).toBe("yes");
    expect(screen.getByTestId("identity-settled").textContent).toBe("yes");
  });

  it("a renew that brings a different subject starts over from the new token", async () => {
    setStoredToken(makeJwt({ sub: ADMIN_SUB, tenant_id: "t" }));
    withMeResponse(adminMe(ADMIN_SUB));
    render(
      <AuthProvider>
        <Identity />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("server-resolved").textContent).toBe("yes"));

    apiClient.defaults.adapter = () => new Promise<AxiosResponse>(() => {});
    act(() =>
      oidcHandlers.current?.onUserLoaded(
        makeJwt({ sub: "99999999-9999-9999-9999-999999999999", tenant_id: "t2", roles: [] }),
      ),
    );

    expect(screen.getByTestId("server-resolved").textContent).toBe("no");
    expect(screen.getByTestId("is-sys-admin").textContent).toBe("no");
    expect(screen.getByTestId("identity-settled").textContent).toBe("no");
    expect(screen.getByTestId("tenant").textContent).toBe("t2");
  });
});

describe("identityFromMe projector", () => {
  it("preserves api_key kind even when auth_method=api_key", () => {
    const identity = _identityFromMeForTests(
      {
        subject_id: "sa-001",
        subject_type: "service_account",
        tenant_id: "t1",
        email: null,
        auth_method: "api_key",
        roles: [],
        scopes: ["read"],
        is_system_admin: false,
        home_is_platform: false,
        allowed_tenants: ["t1"],
      },
      "aforge_pat_abcdef123456",
    );
    expect(identity.kind).toBe("api_key");
    expect(identity.subjectType).toBe("service_account");
    expect(identity.serverResolved).toBe(true);
  });

  it("flags system_admin from the server view, not the JWT", () => {
    const identity = _identityFromMeForTests(
      {
        subject_id: "user-001",
        subject_type: "user",
        tenant_id: "t1",
        email: "founder@corp.com",
        auth_method: "jwt",
        // Roles do not contain ``system_admin`` — the augmentation is
        // server-side and Stream N carries it via ``is_system_admin``.
        roles: ["operator"],
        scopes: [],
        is_system_admin: true,
        home_is_platform: false,
        allowed_tenants: "*",
      },
      "eyJ.test.token",
    );
    expect(identity.isSystemAdmin).toBe(true);
    expect(identity.kind).toBe("jwt");
  });

  it("uses email as the display name for JWT users (not the bare UUID)", () => {
    const identity = _identityFromMeForTests(
      {
        subject_id: "a0d23cd9-4bee-49fb-8290-2a161c04f14d",
        subject_type: "user",
        tenant_id: "t1",
        email: "founder@corp.com",
        auth_method: "jwt",
        roles: ["operator"],
        scopes: [],
        is_system_admin: false,
        home_is_platform: false,
        allowed_tenants: ["t1"],
      },
      "eyJ.test.token",
    );
    expect(identity.displayName).toBe("founder@corp.com");
  });
});
