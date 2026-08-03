/**
 * TenantSwitcher — platform-homed admin (Stream ACCT).
 *
 * A /setup-provisioned system_admin homes to the synthetic platform tenant.
 * That tenant has no workspace and the platform level is the ``"*"`` scope, so
 * the switcher must NOT offer the home tenant as a peer row. A dual-role admin
 * (real-tenant member granted platform scope) keeps their home option.
 *
 * Drives the component through mocked hooks so we can pin ``homeIsPlatform``
 * (only authoritative post-/v1/me; the AuthProvider path can't reach it
 * without a live server).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import { TenantSwitcher } from "../TenantSwitcher";

vi.mock("../../api/tenants", () => ({ listTenants: vi.fn().mockResolvedValue([]) }));

let mockIdentity: { isSystemAdmin: boolean; homeIsPlatform: boolean; homeTenantId: string };
vi.mock("../../auth/AuthContext", () => ({
  useAuth: () => ({ identity: mockIdentity }),
}));

let mockScope: string;
const setScope = vi.fn();
vi.mock("../../tenant/TenantScopeContext", () => ({
  SCOPE_HOME: "home",
  SCOPE_ALL: "*",
  useTenantScope: () => ({ scope: mockScope, setScope }),
}));

afterEach(() => {
  setScope.mockClear();
});

async function openDropdown() {
  const user = userEvent.setup();
  const select = await screen.findByTestId("tenant-switcher");
  await user.click(within(select).getByRole("combobox"));
}

describe("TenantSwitcher — platform-homed admin", () => {
  it("hides the home option when the home tenant is the platform tenant", async () => {
    mockIdentity = {
      isSystemAdmin: true,
      homeIsPlatform: true,
      homeTenantId: "11111111-1111-1111-1111-111111111111",
    };
    mockScope = "*";
    render(
      <MemoryRouter>
        <TenantSwitcher />
      </MemoryRouter>,
    );
    await openDropdown();
    expect(screen.queryByTestId("tenant-switcher-option-home")).toBeNull();
    // Antd may mirror the selected label, so assert presence, not count.
    expect(screen.getAllByTestId("tenant-switcher-option-*").length).toBeGreaterThan(0);
  });

  it("keeps the home option for a dual-role admin homed to a real tenant", async () => {
    mockIdentity = {
      isSystemAdmin: true,
      homeIsPlatform: false,
      homeTenantId: "22222222-2222-2222-2222-222222222222",
    };
    mockScope = "home";
    render(
      <MemoryRouter>
        <TenantSwitcher />
      </MemoryRouter>,
    );
    await openDropdown();
    expect(screen.getAllByTestId("tenant-switcher-option-home").length).toBeGreaterThan(0);
    expect(screen.getAllByTestId("tenant-switcher-option-*").length).toBeGreaterThan(0);
  });
});
