/**
 * Scope-alignment tests — admin-ui-nav-ia §4 + cross-tenant landing rule.
 *
 * Two alignments live in Shell:
 *
 *   1. deep-link promotion: a system_admin deep-linking a platform page
 *      enters platform scope and STAYS PUT (bookmark just works).
 *   2. transition landing: when the scope changes underneath the page
 *      (login auto-promotion to "*", stale-scope demotion) and the page is
 *      not part of the new scope's nav, land on the new scope's first menu
 *      entry — same rule as a switcher change. Pages that ARE part of it
 *      stay; mounting directly onto a "*" aggregate page never bounces.
 *
 * Non-admins on a platform route still get the page's own notice (no
 * bounce) — steady state is untouched, only transitions redirect.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";
import { render, screen } from "@testing-library/react";

import { Shell } from "../Shell";

vi.mock("../Sidebar", () => ({ Sidebar: () => <div /> }));
vi.mock("../Topbar", () => ({ Topbar: () => <div /> }));

let mockScope: string;
const setScope = vi.fn();
vi.mock("../../tenant/TenantScopeContext", () => ({
  SCOPE_ALL: "*",
  SCOPE_HOME: "home",
  useTenantScope: () => ({ scope: mockScope, setScope }),
}));

let mockIsSystemAdmin: boolean;
vi.mock("../../auth/AuthContext", () => ({
  useAuth: () => ({ identity: { isSystemAdmin: mockIsSystemAdmin } }),
}));

function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="pathname">{loc.pathname}</div>;
}

function tree() {
  return (
    <Shell>
      <Routes>
        <Route path="*" element={<LocationProbe />} />
      </Routes>
    </Shell>
  );
}

function renderAt(initialPath: string) {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>{tree()}</MemoryRouter>,
  );
}

/** Flip the mocked scope and rerender — simulates the context applying a
 *  promotion/demotion underneath the mounted Shell. MemoryRouter ignores
 *  ``initialEntries`` after first mount, so the router state carries over. */
function transitionScope(
  view: ReturnType<typeof renderAt>,
  next: string,
  initialPath: string,
) {
  mockScope = next;
  view.rerender(
    <MemoryRouter initialEntries={[initialPath]}>{tree()}</MemoryRouter>,
  );
}

afterEach(() => {
  setScope.mockClear();
});

describe("Shell — scope alignment", () => {
  it("system_admin deep-linking a platform page enters platform scope, stays put", () => {
    mockScope = "home";
    mockIsSystemAdmin = true;
    renderAt("/settings/tenants");
    expect(setScope).toHaveBeenCalledWith("*");
    expect(screen.getByTestId("pathname").textContent).toBe("/settings/tenants");
  });

  it("non-admin on a platform route is NOT bounced (page shows its own notice)", () => {
    mockScope = "home";
    mockIsSystemAdmin = false;
    renderAt("/settings/tenants");
    expect(setScope).not.toHaveBeenCalled();
    expect(screen.getByTestId("pathname").textContent).toBe("/settings/tenants");
  });

  it("does not force-switch on a tenant route (scope-adaptive pages keep scope)", () => {
    // e.g. cross-tenant Members at "*" stays "*"; /settings/members is a tenant route.
    mockScope = "*";
    mockIsSystemAdmin = true;
    renderAt("/settings/members");
    expect(setScope).not.toHaveBeenCalled();
    expect(screen.getByTestId("pathname").textContent).toBe("/settings/members");
  });

  it("leaves an already-aligned platform route untouched", () => {
    mockScope = "*";
    mockIsSystemAdmin = true;
    renderAt("/settings/rate-card");
    expect(setScope).not.toHaveBeenCalled();
    expect(screen.getByTestId("pathname").textContent).toBe("/settings/rate-card");
  });
});

describe("Shell — scope-transition landing", () => {
  it("auto-promotion to '*' away from a workspace page lands the platform first menu", () => {
    // Login flow: platform-homed system_admin lands on /agents, then /v1/me
    // resolves and the context promotes home → "*".
    mockScope = "home";
    mockIsSystemAdmin = true;
    const view = renderAt("/agents");
    expect(screen.getByTestId("pathname").textContent).toBe("/agents");
    transitionScope(view, "*", "/agents");
    expect(screen.getByTestId("pathname").textContent).toBe("/settings/tenants");
  });

  it("promotion keeps a deep-linked platform page (no clobber to first menu)", () => {
    mockScope = "home";
    mockIsSystemAdmin = true;
    const view = renderAt("/settings/mcp-catalog");
    transitionScope(view, "*", "/settings/mcp-catalog");
    expect(screen.getByTestId("pathname").textContent).toBe(
      "/settings/mcp-catalog",
    );
  });

  it("promotion on a dual-group path (/settings/members) stays put", () => {
    mockScope = "home";
    mockIsSystemAdmin = true;
    const view = renderAt("/settings/members");
    transitionScope(view, "*", "/settings/members");
    expect(screen.getByTestId("pathname").textContent).toBe("/settings/members");
  });

  it("mounting a workspace page directly under '*' does not bounce (aggregate views stay reachable)", () => {
    mockScope = "*";
    mockIsSystemAdmin = true;
    renderAt("/knowledge");
    expect(screen.getByTestId("pathname").textContent).toBe("/knowledge");
  });

  it("stale-scope demotion away from a platform page lands the workspace first menu", () => {
    mockScope = "*";
    mockIsSystemAdmin = false;
    const view = renderAt("/settings/tenants");
    transitionScope(view, "home", "/settings/tenants");
    expect(screen.getByTestId("pathname").textContent).toBe("/agents");
  });
});
