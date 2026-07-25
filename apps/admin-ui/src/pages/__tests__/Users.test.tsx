/**
 * Users list tests — the tenant-wide user roster (M2).
 *
 * Stubs ``listUsers``; asserts the roster rows key on subject_id (not the
 * surrogate), the type tags (external / member · role), the summary stats,
 * the truncation tag, and the drill-down navigation carrying subject_id on
 * router state. Auth + tenant-scope contexts are mocked to an admin.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import "../../i18n";

import * as usersSdk from "../../api/users";
import { Users } from "../Users";
import type { TenantUserRosterItem } from "../../api/users";

vi.mock("../../auth/AuthContext", () => ({
  useAuth: () => ({
    identity: { isSystemAdmin: false, roles: ["admin"], serverResolved: true },
  }),
}));
vi.mock("../../tenant/TenantScopeContext", () => ({
  SCOPE_ALL: "*",
  useTenantScope: () => ({ scope: undefined, apiTenantScope: undefined }),
}));

const EXTERNAL: TenantUserRosterItem = {
  user_id: "aaaaaaaa-0000-0000-0000-000000000001",
  subject_id: "ext-alice",
  subject_type: "user",
  display_name: "Alice",
  is_member: false,
  member_email: null,
  member_role: null,
  conversation_count: 3,
  run_count: 5,
  error_count: 2,
  pending_count: 0,
  last_active_at: "2026-07-01T12:00:00Z",
  last_run_at: "2026-07-01T12:00:00Z",
};

const MEMBER: TenantUserRosterItem = {
  user_id: "bbbbbbbb-0000-0000-0000-000000000002",
  subject_id: "kc-bob",
  subject_type: "user",
  display_name: "Bob",
  is_member: true,
  member_email: "bob@example.com",
  member_role: "admin",
  conversation_count: 4,
  run_count: 1,
  error_count: 1,
  pending_count: 0,
  last_active_at: null,
  last_run_at: null,
};

function LocationProbe() {
  const location = useLocation();
  return (
    <div data-testid="probe">
      {location.pathname}|{JSON.stringify(location.state)}
    </div>
  );
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/users"]}>
      <Routes>
        <Route path="/users" element={<Users />} />
        <Route path="/users/:userId" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => vi.restoreAllMocks());

describe("Users", () => {
  it("renders roster rows keyed on subject_id, type tags, and summary stats", async () => {
    vi.spyOn(usersSdk, "listUsers").mockResolvedValue({
      items: [EXTERNAL, MEMBER],
      total: 2,
      cross_tenant: false,
    });
    renderPage();

    // subject_id is the primary identifier (not the surrogate UUID).
    expect(await screen.findByText("ext-alice")).toBeInTheDocument();
    expect(screen.getByText("kc-bob")).toBeInTheDocument();
    expect(screen.getByText("Alice")).toBeInTheDocument();
    expect(screen.getByText("Bob")).toBeInTheDocument();

    // Type tags: external vs member with role appended (scoped to the row
    // tag testid — "External"/"Member" also label the Segmented filter).
    expect(screen.getByTestId(`user-type-${EXTERNAL.user_id}`)).toHaveTextContent("External");
    expect(screen.getByTestId(`user-type-${MEMBER.user_id}`)).toHaveTextContent("Member · admin");

    // Stats: total users (2), summed conversations (3+4=7), errors (2+1=3).
    const stats = screen.getByTestId("users-stats");
    expect(within(stats).getByText("7")).toBeInTheDocument();
    expect(within(stats).getByText("3")).toBeInTheDocument();
  });

  it("shows the truncation tag when total exceeds the returned page", async () => {
    vi.spyOn(usersSdk, "listUsers").mockResolvedValue({
      items: [EXTERNAL],
      total: 42,
      cross_tenant: false,
    });
    renderPage();
    expect(await screen.findByTestId("users-truncated")).toBeInTheDocument();
  });

  it("filters to external / member client-side", async () => {
    vi.spyOn(usersSdk, "listUsers").mockResolvedValue({
      items: [EXTERNAL, MEMBER],
      total: 2,
      cross_tenant: false,
    });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("ext-alice");

    await user.click(screen.getByText("Member"));
    await waitFor(() => expect(screen.queryByText("ext-alice")).not.toBeInTheDocument());
    expect(screen.getByText("kc-bob")).toBeInTheDocument();
  });

  it("names an unnamed employee by their email and still shows the sub", async () => {
    // The common case: an employee has no display_name and their subject_id
    // is an opaque Keycloak sub, so the row used to read "<UUID> / Unnamed".
    const UNNAMED_EMPLOYEE: TenantUserRosterItem = {
      ...MEMBER,
      user_id: "9d1c0a3e-0000-4000-8000-000000000003",
      subject_id: "6344ed9a-6f6e-455a-8eb3-a03a26da1639",
      display_name: null,
      member_email: "carol@corp.com",
    };
    vi.spyOn(usersSdk, "listUsers").mockResolvedValue({
      items: [UNNAMED_EMPLOYEE],
      total: 1,
      cross_tenant: false,
    });
    renderPage();
    expect(await screen.findByText("carol@corp.com")).toBeInTheDocument();
    // The sub stays visible — it is the id operators copy into API calls.
    expect(screen.getByText("6344ed9a-6f6e-455a-8eb3-a03a26da1639")).toBeInTheDocument();
  });

  it("finds an employee by email, not just by subject_id", async () => {
    vi.spyOn(usersSdk, "listUsers").mockResolvedValue({
      items: [EXTERNAL, MEMBER],
      total: 2,
      cross_tenant: false,
    });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("ext-alice");

    await user.type(screen.getByTestId("users-search"), "bob@example.com");
    await waitFor(() => expect(screen.queryByText("ext-alice")).not.toBeInTheDocument());
    expect(screen.getByText("kc-bob")).toBeInTheDocument();
  });

  it("drills into the profile carrying subject_id + name on router state", async () => {
    vi.spyOn(usersSdk, "listUsers").mockResolvedValue({
      items: [EXTERNAL],
      total: 1,
      cross_tenant: false,
    });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByText("Alice"));
    await waitFor(() =>
      expect(screen.getByTestId("probe")).toHaveTextContent(`/users/${EXTERNAL.user_id}`),
    );
    expect(screen.getByTestId("probe")).toHaveTextContent('"subjectId":"ext-alice"');
  });
});
