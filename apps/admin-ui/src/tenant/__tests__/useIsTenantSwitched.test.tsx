/**
 * useIsTenantSwitched — 切入态判定(Track C W2)。
 *
 * 切入态 = scope 为具体租户 UUID 且 ≠ 归属租户。四态矩阵:
 *   - home(默认)→ false
 *   - "*" 聚合 → false
 *   - UUID == 归属租户 → false
 *   - UUID != 归属租户 → true(真正的切入态)
 *
 * Provider/mock 写法照同目录 TenantScopeWireGate.test.tsx:mock useAuth,
 * 真 TenantScopeProvider + sessionStorage 种 scope。
 */
import { afterEach, describe, expect, it, vi, type Mock } from "vitest";
import { render, screen } from "@testing-library/react";

import { SCOPE_ALL, TenantScopeProvider } from "../TenantScopeContext";
import { useIsTenantSwitched } from "../useIsTenantSwitched";
import { useAuth } from "../../auth/AuthContext";

vi.mock("../../auth/AuthContext", () => ({ useAuth: vi.fn() }));

const STORAGE_KEY = "expert_work.admin.tenantScope";
const HOME_TENANT = "11111111-1111-1111-1111-111111111111";
const OTHER_TENANT = "22222222-2222-2222-2222-222222222222";

afterEach(() => {
  window.sessionStorage.clear();
  vi.clearAllMocks();
});

function Probe() {
  const switched = useIsTenantSwitched();
  return <div data-testid="switched">{String(switched)}</div>;
}

function renderWithScope(scope: string | null): void {
  if (scope !== null) window.sessionStorage.setItem(STORAGE_KEY, scope);
  (useAuth as unknown as Mock).mockReturnValue({
    identity: {
      serverResolved: true,
      isSystemAdmin: true,
      homeIsPlatform: false,
      homeTenantId: HOME_TENANT,
    },
  });
  render(
    <TenantScopeProvider>
      <Probe />
    </TenantScopeProvider>,
  );
}

describe("useIsTenantSwitched", () => {
  it("home scope is not switched", () => {
    renderWithScope(null);
    expect(screen.getByTestId("switched").textContent).toBe("false");
  });

  it('"*" aggregate scope is not switched', () => {
    renderWithScope(SCOPE_ALL);
    expect(screen.getByTestId("switched").textContent).toBe("false");
  });

  it("a specific UUID equal to the home tenant is not switched", () => {
    renderWithScope(HOME_TENANT);
    expect(screen.getByTestId("switched").textContent).toBe("false");
  });

  it("a specific UUID different from the home tenant IS switched", () => {
    renderWithScope(OTHER_TENANT);
    expect(screen.getByTestId("switched").textContent).toBe("true");
  });
});
