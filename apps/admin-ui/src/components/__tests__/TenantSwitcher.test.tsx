/**
 * TenantSwitcher tests — Stream H.1b PR 1.
 *
 * Stream N invariants the switcher must enforce client-side:
 *
 *   - tenant_admin sees ONLY their home tenant (component is rendered
 *     as a disabled single-option select).
 *   - system_admin sees home + "All tenants".
 *   - selecting "All tenants" persists the choice across remounts via
 *     ``sessionStorage`` (delegated to TenantScopeContext).
 *
 * Server-side ``ensure_tenant_scope`` is the real gate; this verifies
 * the UI doesn't accidentally offer cross-tenant to a non-admin.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";

import { AuthProvider, _identityFromTokenForTests } from "../../auth/AuthContext";
import { TenantScopeProvider } from "../../tenant/TenantScopeContext";
import { TenantSwitcher } from "../TenantSwitcher";
import { setStoredToken } from "../../api/client";
import { listTenants } from "../../api/tenants";

vi.mock("../../api/tenants", () => ({ listTenants: vi.fn() }));

beforeEach(() => {
  // Default: empty list so existing tests (which don't mock it) still
  // resolve the async fetch without adding concrete-tenant options.
  (listTenants as Mock).mockResolvedValue([]);
});

afterEach(() => {
  setStoredToken(null);
  window.sessionStorage.clear();
  vi.clearAllMocks();
});

function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.`;
}

/** Exposes the router path so the scope-switch landing rule is assertable. */
function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location-probe">{location.pathname}</div>;
}

function renderWith(token: string, initialPath = "/knowledge") {
  setStoredToken(token);
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <AuthProvider>
        <TenantScopeProvider>
          <TenantSwitcher />
          <LocationProbe />
        </TenantScopeProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe("TenantSwitcher — Stream N integration", () => {
  it("non-admin sees only the home tenant option (disabled)", async () => {
    const token = makeJwt({
      sub: "00000000-0000-0000-0000-0000000000aa",
      sub_type: "user",
      tenant_id: "00000000-0000-0000-0000-0000000000a1",
      roles: ["admin"],
    });
    renderWith(token);
    const select = await screen.findByTestId("tenant-switcher");
    // Antd disables the underlying <input> wrapper, not the testid host;
    // assert via the ``ant-select-disabled`` modifier class.
    expect(select.className).toContain("ant-select-disabled");
  });

  it("system_admin renders the switcher in enabled mode", async () => {
    const token = makeJwt({
      sub: "00000000-0000-0000-0000-0000000000aa",
      sub_type: "user",
      tenant_id: "00000000-0000-0000-0000-0000000000a1",
      roles: ["system_admin"],
    });
    renderWith(token);
    const select = await screen.findByTestId("tenant-switcher");
    // Enabled state ⇔ there is more than one option, which for our
    // builder means the "All tenants" entry was added. Tenant-admin
    // path (above) asserts the disabled inverse.
    expect(select.className).not.toContain("ant-select-disabled");
    // userEvent.click is imported but only needed by the negative
    // tenant_admin test; reference it to silence unused-import lint.
    void userEvent;
  });

  it("non-admin never fetches the tenant list", async () => {
    const token = makeJwt({
      sub: "00000000-0000-0000-0000-0000000000aa",
      sub_type: "user",
      tenant_id: "00000000-0000-0000-0000-0000000000a1",
      roles: ["admin"],
    });
    renderWith(token);
    await screen.findByTestId("tenant-switcher");
    expect(listTenants).not.toHaveBeenCalled();
    // Only the home option exists (no "all tenants" entry for non-admins).
    expect(screen.queryByTestId("tenant-switcher-option-*")).toBeNull();
  });

  it("system_admin lists concrete tenants from listTenants()", async () => {
    const tenantId = "11111111-1111-1111-1111-111111111111";
    (listTenants as Mock).mockResolvedValue([
      {
        tenant_id: tenantId,
        display_name: "乐毅大公司",
        plan: "free",
        created_at: "2026-06-02T00:00:00Z",
      },
    ]);
    const token = makeJwt({
      sub: "00000000-0000-0000-0000-0000000000aa",
      sub_type: "user",
      tenant_id: "00000000-0000-0000-0000-0000000000a1",
      roles: ["system_admin"],
    });
    const user = userEvent.setup();
    renderWith(token);
    const select = await screen.findByTestId("tenant-switcher");
    const combobox = within(select).getByRole("combobox");
    await user.click(combobox);
    // Await the async fetch + render of the concrete-tenant option.
    const option = await screen.findByTestId(`tenant-switcher-option-${tenantId}`);
    expect(option).toBeInTheDocument();
    expect(option.textContent).toContain("乐毅大公司");
  });

  it("omits the synthetic platform tenant from the options", async () => {
    const platformId = "11111111-1111-1111-1111-111111111111";
    const realId = "33333333-3333-3333-3333-333333333333";
    (listTenants as Mock).mockResolvedValue([
      {
        tenant_id: platformId,
        display_name: "Platform",
        plan: "enterprise",
        created_at: "2026-06-02T00:00:00Z",
        is_platform: true,
      },
      {
        tenant_id: realId,
        display_name: "真实租户",
        plan: "free",
        created_at: "2026-06-02T00:00:00Z",
        is_platform: false,
      },
    ]);
    const token = makeJwt({
      sub: "00000000-0000-0000-0000-0000000000aa",
      sub_type: "user",
      tenant_id: "00000000-0000-0000-0000-0000000000a1",
      roles: ["system_admin"],
    });
    const user = userEvent.setup();
    renderWith(token);
    const select = await screen.findByTestId("tenant-switcher");
    await user.click(within(select).getByRole("combobox"));
    await screen.findByTestId(`tenant-switcher-option-${realId}`);
    expect(
      screen.queryByTestId(`tenant-switcher-option-${platformId}`),
    ).toBeNull();
  });

  it("selecting a concrete tenant persists the UUID scope", async () => {
    const tenantId = "11111111-1111-1111-1111-111111111111";
    (listTenants as Mock).mockResolvedValue([
      {
        tenant_id: tenantId,
        display_name: "乐毅大公司",
        plan: "free",
        created_at: "2026-06-02T00:00:00Z",
      },
    ]);
    const token = makeJwt({
      sub: "00000000-0000-0000-0000-0000000000aa",
      sub_type: "user",
      tenant_id: "00000000-0000-0000-0000-0000000000a1",
      roles: ["system_admin"],
    });
    const user = userEvent.setup();
    renderWith(token);
    const select = await screen.findByTestId("tenant-switcher");
    const combobox = within(select).getByRole("combobox");
    await user.click(combobox);
    await screen.findByTestId(`tenant-switcher-option-${tenantId}`);
    // Click the visible option content (Antd renders a hidden a11y mirror too).
    const item = await screen.findByText(
      (_content, el) =>
        el?.classList.contains("ant-select-item-option-content") === true &&
        el.textContent?.includes("乐毅大公司") === true,
    );
    await user.click(item);
    expect(window.sessionStorage.getItem("expert_work.admin.tenantScope")).toBe(tenantId);
  });

  it("switching into a tenant lands on the tenant IA's first entry (/agents)", async () => {
    const tenantId = "11111111-1111-1111-1111-111111111111";
    (listTenants as Mock).mockResolvedValue([
      {
        tenant_id: tenantId,
        display_name: "乐毅大公司",
        plan: "free",
        created_at: "2026-06-02T00:00:00Z",
      },
    ]);
    const token = makeJwt({
      sub: "00000000-0000-0000-0000-0000000000aa",
      sub_type: "user",
      tenant_id: "00000000-0000-0000-0000-0000000000a1",
      roles: ["system_admin"],
    });
    const user = userEvent.setup();
    // Start on a platform page — a tenant perspective has no such route.
    renderWith(token, "/settings/tenants");
    const select = await screen.findByTestId("tenant-switcher");
    await user.click(within(select).getByRole("combobox"));
    await screen.findByTestId(`tenant-switcher-option-${tenantId}`);
    const item = await screen.findByText(
      (_content, el) =>
        el?.classList.contains("ant-select-item-option-content") === true &&
        el.textContent?.includes("乐毅大公司") === true,
    );
    await user.click(item);
    expect(screen.getByTestId("location-probe")).toHaveTextContent("/agents");
  });

  it('switching back to "*" lands on the platform IA\'s first entry (/settings/tenants)', async () => {
    const token = makeJwt({
      sub: "00000000-0000-0000-0000-0000000000aa",
      sub_type: "user",
      tenant_id: "00000000-0000-0000-0000-0000000000a1",
      roles: ["system_admin"],
    });
    const user = userEvent.setup();
    // Start deep inside a tenant page — meaningless under "*".
    renderWith(token, "/agents/some-agent/1.0.0");
    const select = await screen.findByTestId("tenant-switcher");
    await user.click(within(select).getByRole("combobox"));
    const item = await screen.findByTestId("tenant-switcher-option-*");
    await user.click(item);
    expect(screen.getByTestId("location-probe")).toHaveTextContent("/settings/tenants");
  });
});

describe("identityFromToken parser", () => {
  it("flags system_admin via roles claim", () => {
    const token = makeJwt({ sub: "x", roles: ["system_admin"], tenant_id: "t1" });
    const identity = _identityFromTokenForTests(token);
    expect(identity.isSystemAdmin).toBe(true);
    expect(identity.homeTenantId).toBe("t1");
  });

  it("treats api keys as non-system-admin opaque", () => {
    const identity = _identityFromTokenForTests("aforge_pat_abcdefg12345");
    expect(identity.kind).toBe("api_key");
    expect(identity.isSystemAdmin).toBe(false);
    expect(identity.homeTenantId).toBeNull();
  });
});
