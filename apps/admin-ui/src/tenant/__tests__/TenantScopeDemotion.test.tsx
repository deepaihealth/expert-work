/**
 * TenantScopeContext — stale-scope demotion (Cross-tenant W3).
 *
 * A non-system-admin landing with a cached non-home scope (the "*"
 * aggregate OR a residual specific-tenant UUID from a prior admin
 * session / mid-session demotion) is demoted back to ``home`` once the
 * server truth confirms the non-admin. Without the UUID branch the
 * residual scope would flag ``useIsTenantSwitched`` forever and lock
 * every write control greyed.
 */
import { afterEach, describe, expect, it, vi, type Mock } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

import {
  SCOPE_HOME,
  TenantScopeProvider,
  useTenantScope,
} from "../TenantScopeContext";
import { useAuth } from "../../auth/AuthContext";

vi.mock("../../auth/AuthContext", () => ({ useAuth: vi.fn() }));

const STORAGE_KEY = "expert_work.admin.tenantScope";
const RESIDUAL_UUID = "22222222-2222-2222-2222-222222222222";

afterEach(() => {
  window.sessionStorage.clear();
  vi.clearAllMocks();
});

function ScopeProbe() {
  const { scope } = useTenantScope();
  return <div data-testid="scope">{scope}</div>;
}

function renderWithIdentity(identity: unknown) {
  (useAuth as unknown as Mock).mockReturnValue({ identity });
  return render(
    <TenantScopeProvider>
      <ScopeProbe />
    </TenantScopeProvider>,
  );
}

describe("TenantScopeContext — 降级 effect (W3)", () => {
  it("残留具体 UUID + 确认非管理员 → 回 home(两态其一)", async () => {
    window.sessionStorage.setItem(STORAGE_KEY, RESIDUAL_UUID);
    renderWithIdentity({ serverResolved: true, isSystemAdmin: false });

    await waitFor(() => {
      expect(screen.getByTestId("scope").textContent).toBe(SCOPE_HOME);
    });
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBe(SCOPE_HOME);
  });

  it("残留具体 UUID + 确认系统管理员 → 保持切入态(两态其二)", async () => {
    window.sessionStorage.setItem(STORAGE_KEY, RESIDUAL_UUID);
    renderWithIdentity({ serverResolved: true, isSystemAdmin: true });

    // Give the effect a chance to (not) fire.
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.getByTestId("scope").textContent).toBe(RESIDUAL_UUID);
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBe(RESIDUAL_UUID);
  });

  it("server 未确认前不降级(乐观 identity 不作数)", async () => {
    window.sessionStorage.setItem(STORAGE_KEY, RESIDUAL_UUID);
    renderWithIdentity({ serverResolved: false, isSystemAdmin: false });

    await new Promise((r) => setTimeout(r, 0));
    expect(screen.getByTestId("scope").textContent).toBe(RESIDUAL_UUID);
  });
});
