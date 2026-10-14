/**
 * TenantScopeGate — no page reads with a scope that is about to change (B-183).
 *
 * ``apiTenantScope`` reports home until ``/v1/me`` confirms a system_admin (see
 * ``TenantScopeWireGate.test``). On a reload with a switched-in scope persisted
 * in sessionStorage, every page used to mount in that window, read its home
 * tenant first and re-read once the scope flipped. A page without a
 * stale-response guard then showed whichever answer landed last — live on
 * 2026-10-14: the knowledge detail page stuck on 「知识库加载失败 HTTP_404」
 * because the home read (404, base lives in the switched-in tenant) came back
 * after the switched-in read (200).
 *
 * The gate holds the routed pages back while the scope is still settling, so
 * they mount once, on the scope they should read with.
 */
import { afterEach, describe, expect, it, vi, type Mock } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import { App } from "antd";
import { useEffect } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import "../../i18n";

import * as knowledgeSdk from "../../api/knowledge";
import * as meSdk from "../../api/me";
import { ApiError, setStoredToken } from "../../api/client";
import { AuthProvider, useAuth } from "../../auth/AuthContext";
import { ProtectedRoute } from "../../auth/ProtectedRoute";
import { KnowledgeDetail } from "../../pages/KnowledgeDetail";
import {
  SCOPE_ALL,
  TenantScopeGate,
  TenantScopeProvider,
  useTenantScope,
} from "../TenantScopeContext";

const STORAGE_KEY = "expert_work.admin.tenantScope";
const HOME = "<home>";
const PLATFORM_TENANT = "00000000-0000-0000-0000-000000000001";
const SWITCHED_TENANT = "dd068302-0000-4000-8000-000000000001";
const ADMIN_SUB = "11111111-1111-1111-1111-111111111111";

function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  return `${header}.${btoa(JSON.stringify(payload))}.`;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const BASE: knowledgeSdk.KnowledgeBase = {
  id: "33333333-3333-3333-3333-333333333333",
  name: "b175-scope",
  chunk_max_tokens: 512,
  chunk_overlap_tokens: 64,
  created_at: "2026-10-14T00:00:00Z",
  description: null,
  retrieval_config: { top_k: 5, score_threshold: null, method: "hybrid", rerank_enabled: true },
  embedding_provider: "qwen",
  embedding_model: "text-embedding-v4",
  needs_reindex: false,
  reindexing: false,
  stats: { document_count: 0, chunk_count: 0 },
};

afterEach(() => {
  window.sessionStorage.clear();
  setStoredToken(null);
  vi.restoreAllMocks();
});

describe("B-183 — reload of a switched-in knowledge base (real AuthProvider)", () => {
  it("never reads the base with the home scope and does not end on the error card", async () => {
    // A platform-homed system_admin switched into SWITCHED_TENANT, then F5.
    // The JWT carries no system_admin role (the grant is a server-side role
    // binding), so only /v1/me can tell.
    window.sessionStorage.setItem(STORAGE_KEY, SWITCHED_TENANT);
    setStoredToken(
      makeJwt({ sub: ADMIN_SUB, sub_type: "user", tenant_id: PLATFORM_TENANT, roles: [] }),
    );
    const me = deferred<meSdk.MeResponse>();
    vi.spyOn(meSdk, "getMe").mockReturnValue(me.promise);

    // Home read: the base isn't in the platform tenant → 404, and it lands
    // late (after the switched-in read), exactly as on the live network log.
    const homeRead = deferred<knowledgeSdk.KnowledgeBase>();
    // Fixed code never asks for it — keep its rejection from going unhandled.
    homeRead.promise.catch(() => {});
    const getBase = vi
      .spyOn(knowledgeSdk, "getBase")
      .mockImplementation((_name, scope) =>
        scope === SWITCHED_TENANT ? Promise.resolve(BASE) : homeRead.promise,
      );
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue({
      documents: [],
      total: 0,
      offset: 0,
      limit: 50,
    });

    // Same nesting as main.tsx + App.tsx (minus Shell chrome).
    render(
      <MemoryRouter initialEntries={["/knowledge/b175-scope/documents"]}>
        <AuthProvider>
          <TenantScopeProvider>
            <App>
              <ProtectedRoute>
                <TenantScopeGate>
                  <Routes>
                    <Route path="/knowledge/:name/:tab" element={<KnowledgeDetail />} />
                  </Routes>
                </TenantScopeGate>
              </ProtectedRoute>
            </App>
          </TenantScopeProvider>
        </AuthProvider>
      </MemoryRouter>,
    );

    // /v1/me still in flight: nothing may be read yet.
    await act(async () => {});
    expect(meSdk.getMe).toHaveBeenCalledTimes(1);
    expect(getBase).not.toHaveBeenCalled();

    await act(async () => {
      me.resolve({
        subject_id: ADMIN_SUB,
        subject_type: "user",
        tenant_id: PLATFORM_TENANT,
        email: "root@example.com",
        auth_method: "jwt",
        roles: [],
        scopes: [],
        is_system_admin: true,
        home_is_platform: true,
        allowed_tenants: "*",
      });
    });
    await waitFor(() => expect(getBase).toHaveBeenCalledWith("b175-scope", SWITCHED_TENANT));
    expect(await screen.findByTestId("knowledge-detail-root")).toBeInTheDocument();

    // Whatever home read might have gone out answers 404 now — late.
    await act(async () => {
      homeRead.reject(new ApiError("knowledge base not found", "HTTP_404", 404));
    });

    for (const [, scope] of getBase.mock.calls) {
      expect(scope).toBe(SWITCHED_TENANT);
    }
    expect(screen.queryByTestId("knowledge-detail-error")).toBeNull();
    expect(screen.getByTestId("knowledge-detail-root")).toBeInTheDocument();
  });
});

// --- Gate matrix (mocked useAuth) ------------------------------------------

vi.mock("../../auth/AuthContext", async (importOriginal) => {
  const original = await importOriginal<typeof import("../../auth/AuthContext")>();
  return { ...original, useAuth: vi.fn(original.useAuth) };
});

/** Stands in for a page: "reads" on mount and whenever the wire scope
 *  changes, and records its own unmount (a page that is unmounted can no
 *  longer show a late answer). */
function ReadProbe({ log }: { log: string[] }) {
  const { apiTenantScope } = useTenantScope();
  useEffect(() => {
    log.push(`read:${apiTenantScope ?? HOME}`);
  }, [apiTenantScope, log]);
  useEffect(() => () => void log.push("unmount"), [log]);
  return <div data-testid="probe" />;
}

interface FakeAuth {
  identitySettled: boolean;
  identity: {
    serverResolved: boolean;
    isSystemAdmin: boolean;
    homeIsPlatform: boolean;
    homeTenantId: string | null;
  };
}

function gateTree(log: string[]) {
  return (
    <TenantScopeProvider>
      <TenantScopeGate>
        <ReadProbe log={log} />
      </TenantScopeGate>
    </TenantScopeProvider>
  );
}

function renderGate(auth: FakeAuth, log: string[]) {
  (useAuth as unknown as Mock).mockReturnValue({ status: "authenticated", ...auth });
  const view = render(gateTree(log));
  return {
    ...view,
    settle(next: FakeAuth) {
      (useAuth as unknown as Mock).mockReturnValue({ status: "authenticated", ...next });
      view.rerender(gateTree(log));
    },
  };
}

const OPTIMISTIC = {
  serverResolved: false,
  isSystemAdmin: false,
  homeIsPlatform: false,
  homeTenantId: PLATFORM_TENANT,
};
const PLATFORM_ADMIN = {
  serverResolved: true,
  isSystemAdmin: true,
  homeIsPlatform: true,
  homeTenantId: PLATFORM_TENANT,
};
const TENANT_USER = {
  serverResolved: true,
  isSystemAdmin: false,
  homeIsPlatform: false,
  homeTenantId: "44444444-4444-4444-4444-444444444444",
};

describe("TenantScopeGate", () => {
  it("holds pages back until /v1/me while a switched-in scope is persisted", async () => {
    window.sessionStorage.setItem(STORAGE_KEY, SWITCHED_TENANT);
    const log: string[] = [];
    const view = renderGate({ identitySettled: false, identity: OPTIMISTIC }, log);
    await act(async () => {});
    expect(screen.queryByTestId("probe")).toBeNull();
    expect(log).toEqual([]);

    view.settle({ identitySettled: true, identity: PLATFORM_ADMIN });
    await waitFor(() => expect(log).toEqual([`read:${SWITCHED_TENANT}`]));
  });

  it("does not add a wait for the common case (home scope)", async () => {
    const log: string[] = [];
    const view = renderGate(
      { identitySettled: false, identity: { ...TENANT_USER, serverResolved: false } },
      log,
    );
    // Optimistic paint: the page reads before /v1/me is back…
    expect(screen.getByTestId("probe")).toBeInTheDocument();
    expect(log).toEqual([`read:${HOME}`]);

    // …and /v1/me landing changes nothing for it.
    view.settle({ identitySettled: true, identity: TENANT_USER });
    await act(async () => {});
    expect(log).toEqual([`read:${HOME}`]);
  });

  it("lets pages through on home when /v1/me fails (no endless placeholder)", async () => {
    window.sessionStorage.setItem(STORAGE_KEY, SWITCHED_TENANT);
    const log: string[] = [];
    const view = renderGate({ identitySettled: false, identity: OPTIMISTIC }, log);
    expect(screen.queryByTestId("probe")).toBeNull();

    // Settled without a server identity = /v1/me failed (non-401): the
    // optimistic identity stays and the wire stays home, as before the gate.
    view.settle({ identitySettled: true, identity: OPTIMISTIC });
    await waitFor(() => expect(screen.getByTestId("probe")).toBeInTheDocument());
    expect(log).toEqual([`read:${HOME}`]);
  });

  it("a stale scope of a confirmed non-admin is demoted before any page reads", async () => {
    window.sessionStorage.setItem(STORAGE_KEY, SWITCHED_TENANT);
    const log: string[] = [];
    const view = renderGate({ identitySettled: false, identity: OPTIMISTIC }, log);

    view.settle({ identitySettled: true, identity: TENANT_USER });
    await waitFor(() => expect(log).toEqual([`read:${HOME}`]));
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBe("home");
  });

  it("fresh tab, platform-homed admin: the page that read home is replaced, not re-fed", async () => {
    // Nothing persisted yet (first load in this tab) — indistinguishable
    // from a tenant user until /v1/me, so no wait up front. When the
    // promotion to "*" lands, the page instance that read home must be gone
    // (its late answer can't land), and a fresh one reads "*".
    const log: string[] = [];
    const view = renderGate({ identitySettled: false, identity: OPTIMISTIC }, log);
    expect(log).toEqual([`read:${HOME}`]);

    view.settle({ identitySettled: true, identity: PLATFORM_ADMIN });
    await waitFor(() => expect(log).toEqual([`read:${HOME}`, "unmount", `read:${SCOPE_ALL}`]));
  });
});
