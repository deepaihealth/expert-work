/**
 * Tenants page tests — system_admin gate + list render + Manage action.
 *
 * Manage switches the tenant scope into the row's tenant (persisted to
 * sessionStorage by TenantScopeProvider) and navigates to the per-tenant
 * config page. The non-admin path renders the gate alert and never hits the
 * list API.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { App } from "antd";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../i18n";

import { SettingsTenants } from "../SettingsTenants";
import { AuthProvider } from "../../auth/AuthContext";
import { TenantScopeProvider } from "../../tenant/TenantScopeContext";
import { setStoredToken } from "../../api/client";
import {
  activateTenant,
  deactivateTenant,
  listTenants,
  resendFirstAdmin,
} from "../../api/tenants";

const mockNavigate = vi.fn();

vi.mock("../../api/tenants", () => ({
  listTenants: vi.fn(),
  deactivateTenant: vi.fn(),
  activateTenant: vi.fn(),
  resendFirstAdmin: vi.fn(),
}));

vi.mock("../../components/CreateTenantDrawer", () => ({
  CreateTenantDrawer: ({ open }: { open: boolean }) =>
    open ? <div data-testid="create-tenant-drawer" /> : null,
}));

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>(
    "react-router-dom",
  );
  return { ...actual, useNavigate: () => mockNavigate };
});

function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.`;
}

function renderPage(): void {
  render(
    <MemoryRouter>
      <AuthProvider>
        <TenantScopeProvider>
          <App>
            <SettingsTenants />
          </App>
        </TenantScopeProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

afterEach(() => {
  setStoredToken(null);
  window.sessionStorage.clear();
  vi.clearAllMocks();
});

describe("SettingsTenants", () => {
  it("lists tenants for a system_admin", async () => {
    setStoredToken(
      makeJwt({ sub: "u1", tenant_id: "t1", roles: ["admin", "system_admin"] }),
    );
    vi.mocked(listTenants).mockResolvedValue([
      {
        tenant_id: "11111111-1111-1111-1111-111111111111",
        display_name: "乐毅大公司",
        plan: "free",
        created_at: "2026-06-02T00:00:00Z",
        status: "active",
      },
    ]);
    renderPage();

    expect(await screen.findByText("乐毅大公司")).toBeInTheDocument();
  });

  it("hides the synthetic platform tenant from the table", async () => {
    setStoredToken(
      makeJwt({ sub: "u1", tenant_id: "t1", roles: ["admin", "system_admin"] }),
    );
    vi.mocked(listTenants).mockResolvedValue([
      {
        tenant_id: "11111111-1111-1111-1111-111111111111",
        display_name: "Platform",
        plan: "enterprise",
        created_at: "2026-06-02T00:00:00Z",
        status: "active",
        is_platform: true,
      },
      {
        tenant_id: "33333333-3333-3333-3333-333333333333",
        display_name: "真实租户",
        plan: "free",
        created_at: "2026-06-02T00:00:00Z",
        status: "active",
        is_platform: false,
      },
    ]);
    renderPage();

    expect(await screen.findByText("真实租户")).toBeInTheDocument();
    expect(screen.queryByText("Platform")).not.toBeInTheDocument();
  });

  it("Manage switches scope and navigates to tenant config", async () => {
    const user = userEvent.setup();
    setStoredToken(
      makeJwt({ sub: "u1", tenant_id: "t1", roles: ["admin", "system_admin"] }),
    );
    vi.mocked(listTenants).mockResolvedValue([
      {
        tenant_id: "11111111-1111-1111-1111-111111111111",
        display_name: "乐毅大公司",
        plan: "free",
        created_at: "2026-06-02T00:00:00Z",
        status: "active",
      },
    ]);
    renderPage();

    await screen.findByText("乐毅大公司");
    await user.click(
      screen.getByTestId("st-manage-11111111-1111-1111-1111-111111111111"),
    );

    expect(mockNavigate).toHaveBeenCalledWith("/settings/tenant-config");
    expect(window.sessionStorage.getItem("expert_work.admin.tenantScope")).toBe(
      "11111111-1111-1111-1111-111111111111",
    );
  });

  it("renders the suspended badge for a suspended tenant", async () => {
    setStoredToken(
      makeJwt({ sub: "u1", tenant_id: "t1", roles: ["admin", "system_admin"] }),
    );
    vi.mocked(listTenants).mockResolvedValue([
      {
        tenant_id: "22222222-2222-2222-2222-222222222222",
        display_name: "停用公司",
        plan: "free",
        created_at: "2026-06-02T00:00:00Z",
        status: "suspended",
      },
    ]);
    renderPage();

    const badge = await screen.findByTestId(
      "st-status-22222222-2222-2222-2222-222222222222",
    );
    expect(badge).toHaveTextContent("Suspended");
    expect(
      screen.getByTestId("st-activate-22222222-2222-2222-2222-222222222222"),
    ).toBeInTheDocument();
  });

  it("activate calls activateTenant then re-fetches", async () => {
    const user = userEvent.setup();
    setStoredToken(
      makeJwt({ sub: "u1", tenant_id: "t1", roles: ["admin", "system_admin"] }),
    );
    vi.mocked(listTenants).mockResolvedValue([
      {
        tenant_id: "22222222-2222-2222-2222-222222222222",
        display_name: "停用公司",
        plan: "free",
        created_at: "2026-06-02T00:00:00Z",
        status: "suspended",
      },
    ]);
    vi.mocked(activateTenant).mockResolvedValue();
    renderPage();

    await screen.findByText("停用公司");
    await user.click(
      screen.getByTestId("st-activate-22222222-2222-2222-2222-222222222222"),
    );

    expect(activateTenant).toHaveBeenCalledWith(
      "22222222-2222-2222-2222-222222222222",
    );
    await vi.waitFor(() => expect(listTenants).toHaveBeenCalledTimes(2));
  });

  it("deactivate confirms then calls deactivateTenant and re-fetches", async () => {
    const user = userEvent.setup();
    setStoredToken(
      makeJwt({ sub: "u1", tenant_id: "t1", roles: ["admin", "system_admin"] }),
    );
    vi.mocked(listTenants).mockResolvedValue([
      {
        tenant_id: "11111111-1111-1111-1111-111111111111",
        display_name: "乐毅大公司",
        plan: "free",
        created_at: "2026-06-02T00:00:00Z",
        status: "active",
      },
    ]);
    vi.mocked(deactivateTenant).mockResolvedValue();
    renderPage();

    await screen.findByText("乐毅大公司");
    await user.click(
      screen.getByTestId("st-deactivate-11111111-1111-1111-1111-111111111111"),
    );
    // Popconfirm: click through the OK button (antd default okText is "OK").
    await user.click(await screen.findByRole("button", { name: "OK" }));

    expect(deactivateTenant).toHaveBeenCalledWith(
      "11111111-1111-1111-1111-111111111111",
    );
    await vi.waitFor(() => expect(listTenants).toHaveBeenCalledTimes(2));
  });

  it("resend first-admin credentials shows the one-time password panel", async () => {
    const user = userEvent.setup();
    setStoredToken(
      makeJwt({ sub: "u1", tenant_id: "t1", roles: ["admin", "system_admin"] }),
    );
    vi.mocked(listTenants).mockResolvedValue([
      {
        tenant_id: "33333333-3333-3333-3333-333333333333",
        display_name: "锁死公司",
        plan: "free",
        created_at: "2026-09-07T00:00:00Z",
        status: "active",
      },
    ]);
    vi.mocked(resendFirstAdmin).mockResolvedValue({
      member_id: "m1",
      email: "boss@locked.example",
      status: "invited",
      keycloak_user_id: "kc-1",
      initial_password: "lark-opal-fern-2048",
      credential_pending: false,
    });
    renderPage();

    await screen.findByText("锁死公司");
    await user.click(
      screen.getByTestId("st-resend-first-admin-33333333-3333-3333-3333-333333333333"),
    );

    expect(resendFirstAdmin).toHaveBeenCalledWith("33333333-3333-3333-3333-333333333333");
    expect(await screen.findByTestId("one-time-credential-panel")).toBeInTheDocument();
    expect(screen.getByText("lark-opal-fern-2048")).toBeInTheDocument();
  });

  it("opens the create-tenant drawer from the Create button", async () => {
    const user = userEvent.setup();
    setStoredToken(
      makeJwt({ sub: "u1", tenant_id: "t1", roles: ["admin", "system_admin"] }),
    );
    vi.mocked(listTenants).mockResolvedValue([]);
    renderPage();

    await screen.findByTestId("tenants-create");
    expect(screen.queryByTestId("create-tenant-drawer")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("tenants-create"));

    expect(
      await screen.findByTestId("create-tenant-drawer"),
    ).toBeInTheDocument();
  });

  it("gates non-admins and never lists", async () => {
    setStoredToken(makeJwt({ sub: "u1", tenant_id: "t1", roles: ["admin"] }));
    renderPage();

    expect(await screen.findByTestId("st-not-admin")).toBeInTheDocument();
    expect(listTenants).not.toHaveBeenCalled();
  });
});
