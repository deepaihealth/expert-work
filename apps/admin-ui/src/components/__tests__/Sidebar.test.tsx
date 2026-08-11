/**
 * Sidebar IA tests — scope-driven platform/tenant separation.
 *
 * See ``docs/design/admin-ui-nav-ia.md``. The sidebar is a function of
 * ``(scope, isSystemAdmin)``:
 *
 *   - a concrete tenant scope → Workspace + Tenant settings; no Platform.
 *   - the ``"*"`` scope (system_admin) → Platform only.
 *   - a non-admin can never see the Platform group on any scope.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { render, screen } from "@testing-library/react";
import "../../i18n";

import { Sidebar } from "../Sidebar";

let mockScope: string;
vi.mock("../../tenant/TenantScopeContext", () => ({
  SCOPE_ALL: "*",
  SCOPE_HOME: "home",
  useTenantScope: () => ({ scope: mockScope }),
}));

let mockIsSystemAdmin = false;
vi.mock("../../auth/AuthContext", () => ({
  useAuth: () => ({ identity: { isSystemAdmin: mockIsSystemAdmin } }),
}));

// ApprovalPendingBadge fetches the pending count — stub it out so the
// sidebar renders synchronously without network.
vi.mock("../ApprovalPendingBadge", () => ({
  ApprovalPendingBadge: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

function renderSidebar() {
  return render(
    <MemoryRouter>
      <Sidebar />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("Sidebar — scope-driven groups", () => {
  it("tenant scope shows Workspace + Tenant settings, not Platform", () => {
    mockScope = "home";
    mockIsSystemAdmin = false;
    renderSidebar();

    expect(screen.getByText("Workspace")).toBeInTheDocument();
    expect(screen.getByText("Tenant settings")).toBeInTheDocument();
    expect(screen.queryByText("Platform")).toBeNull();
    // A workspace item + a tenant-settings item are present.
    expect(screen.getByText("Agents")).toBeInTheDocument();
    expect(screen.getByText("Members")).toBeInTheDocument();
    // No platform-only item leaks in.
    expect(screen.queryByText("Tenants")).toBeNull();
    expect(screen.queryByText("Model Pricing")).toBeNull();
  });

  it("platform scope (system_admin) shows Platform only", () => {
    mockScope = "*";
    mockIsSystemAdmin = true;
    renderSidebar();

    expect(screen.getByText("Platform")).toBeInTheDocument();
    expect(screen.queryByText("Workspace")).toBeNull();
    expect(screen.queryByText("Tenant settings")).toBeNull();
    // Platform governance items present.
    expect(screen.getByText("Tenants")).toBeInTheDocument();
    expect(screen.getByText("Model Pricing")).toBeInTheDocument();
    expect(screen.getByText("Members (all tenants)")).toBeInTheDocument();
    expect(screen.getByText("Keycloak")).toBeInTheDocument();
    // No workspace item leaks in.
    expect(screen.queryByText("Agents")).toBeNull();
  });

  it("non-admin never sees the Platform group, even on the * scope", () => {
    // Defense in depth: the switcher won't offer "*" to a non-admin, but
    // if a stale scope slips through the group still hides.
    mockScope = "*";
    mockIsSystemAdmin = false;
    renderSidebar();

    expect(screen.queryByText("Platform")).toBeNull();
    expect(screen.queryByText("Tenants")).toBeNull();
  });

  it("the Handbook entry is always visible — tenant scope, non-admin", () => {
    mockScope = "home";
    mockIsSystemAdmin = false;
    renderSidebar();

    expect(screen.getByText("Handbook")).toBeInTheDocument();
  });

  it("the Handbook entry is always visible — platform scope, system_admin", () => {
    mockScope = "*";
    mockIsSystemAdmin = true;
    renderSidebar();

    expect(screen.getByText("Handbook")).toBeInTheDocument();
  });
});
