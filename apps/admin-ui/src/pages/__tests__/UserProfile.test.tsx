/**
 * UserProfile tests — the agent-agnostic top-level user page (M2).
 *
 * Covers the header resolving subject_id from the registry, and the memory
 * tab: default-descending sort by importance plus the admin edit/forget
 * mutations threading the surrogate userId. Auth + tenant-scope contexts
 * are mocked to an admin; each pane SDK is stubbed with vi.spyOn.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "antd";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import "../../i18n";

import * as usersSdk from "../../api/users";
import * as agentsSdk from "../../api/agents";
import * as artifactsSdk from "../../api/artifacts";
import * as convoSdk from "../../api/conversations";
import * as memorySdk from "../../api/memory";
import * as workspaceSdk from "../../api/workspace";
import { ApiError } from "../../api/client";
import { UserProfile } from "../UserProfile";
import type { MemoryItem } from "../../api/memory";
import type { PurgeSummary } from "../../api/users";

// A non-matching default — the self-purge test below points this at the
// rendered target's subject_id ("ext-alice") to exercise the isSelf branch.
let mockIdentitySubject = "someone-else";
vi.mock("../../auth/AuthContext", () => ({
  useAuth: () => ({
    identity: {
      isSystemAdmin: false,
      roles: ["admin"],
      serverResolved: true,
      subject: mockIdentitySubject,
    },
  }),
}));
let mockScope: string | undefined;
// Spread the real module so the page keeps the real ``concreteTenantScope``
// ("*" → undefined) instead of a test-local copy that could drift.
vi.mock("../../tenant/TenantScopeContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../tenant/TenantScopeContext")>()),
  useTenantScope: () => ({ scope: mockScope, apiTenantScope: mockScope }),
}));

const USER_ID = "aaaaaaaa-0000-0000-0000-000000000001";

const HIGH: MemoryItem = {
  id: "m-high",
  tenant_id: "t",
  user_id: USER_ID,
  kind: "fact",
  content: "High importance memory",
  created_at: "2026-06-01T10:00:00Z",
  importance: 0.9,
  confidence: 0.5,
};
const LOW: MemoryItem = {
  id: "m-low",
  tenant_id: "t",
  user_id: USER_ID,
  kind: "episodic",
  content: "Low importance memory",
  created_at: "2026-06-30T10:00:00Z",
  importance: 0.2,
  confidence: 0.9,
};

function stubCommon() {
  vi.spyOn(usersSdk, "getTenantUser").mockResolvedValue({
    user_id: USER_ID,
    subject_id: "ext-alice",
    display_name: "Alice",
    subject_type: "user",
    is_member: false,
    member_email: null,
    member_role: null,
    created_at: "2026-06-01T00:00:00Z",
    last_active_at: "2026-07-01T00:00:00Z",
  });
  vi.spyOn(agentsSdk, "listAgents").mockResolvedValue({
    items: [],
    total: 0,
    cross_tenant: false,
  });
  vi.spyOn(convoSdk, "listConversations").mockResolvedValue({
    items: [],
    total: 0,
    cross_tenant: false,
  });
  vi.spyOn(memorySdk, "listMemories").mockResolvedValue({
    items: [LOW, HIGH],
    total: 2,
    cross_tenant: false,
  });
}

const OK_SUMMARY: PurgeSummary = {
  tenant_id: "t",
  user_id: USER_ID,
  subject_id: "ext-alice",
  threads_purged: 1,
  runs_deleted: 0,
  threads_capped: false,
  deleted: {},
  anonymized: {},
  workspace_marked_deleted: true,
  deactivated: true,
  failures: {},
  ok: true,
};

/** ``navState`` mirrors ``Users.tsx``'s row-click state (eg. ``isMember``). */
function renderPage(navState?: { isMember?: boolean }) {
  return render(
    <App>
      <MemoryRouter
        initialEntries={[{ pathname: `/users/${USER_ID}`, state: navState }]}
      >
        <Routes>
          <Route path="/users/:userId" element={<UserProfile />} />
          {/* Purge navigates back here on success. */}
          <Route path="/users" element={<div data-testid="users-roster-sentinel" />} />
        </Routes>
      </MemoryRouter>
    </App>,
  );
}

beforeEach(() => {
  mockIdentitySubject = "someone-else";
});
afterEach(() => {
  mockScope = undefined;
  vi.restoreAllMocks();
});

describe("UserProfile", () => {
  it("resolves and shows subject_id in the header on a direct URL open", async () => {
    stubCommon();
    renderPage();
    // display_name paints the title; subject_id is the copyable identifier.
    expect(await screen.findByText("Alice")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("user-profile-subject-id")).toHaveTextContent("ext-alice"),
    );
    expect(usersSdk.getTenantUser).toHaveBeenCalledWith(USER_ID, undefined);
  });

  it("threads the tenant scope through getTenantUser (跨租户钻取 B类补传)", async () => {
    mockScope = "22222222-2222-2222-2222-222222222222";
    stubCommon();
    renderPage();
    await waitFor(() =>
      expect(usersSdk.getTenantUser).toHaveBeenCalledWith(USER_ID, mockScope),
    );
  });

  it('maps the "*" aggregate scope to no tenant_id (backend 422s a literal "*")', async () => {
    mockScope = "*";
    stubCommon();
    renderPage();
    await waitFor(() =>
      expect(usersSdk.getTenantUser).toHaveBeenCalledWith(USER_ID, undefined),
    );
  });

  it("threads the switched scope through the conversations + workspace panes (W3)", async () => {
    mockScope = "22222222-2222-2222-2222-222222222222";
    stubCommon();
    vi.spyOn(workspaceSdk, "getUserWorkspace").mockResolvedValue({
      workspace: null,
      artifacts: [],
    });
    vi.spyOn(workspaceSdk, "getUserWorkspaceFiles").mockResolvedValue([]);
    const artifactsSpy = vi
      .spyOn(artifactsSdk, "listArtifacts")
      .mockResolvedValue({ items: [], cross_tenant: false });
    const user = userEvent.setup();
    renderPage();

    // Conversations is the default tab — the agent-filter list rides the
    // bare scope; the conversations list takes the concrete UUID.
    await waitFor(() =>
      expect(agentsSdk.listAgents).toHaveBeenCalledWith(
        expect.objectContaining({ limit: 100, tenantScope: mockScope }),
      ),
    );
    expect(convoSdk.listConversations).toHaveBeenCalledWith(
      expect.objectContaining({ userId: USER_ID, tenantScope: mockScope }),
    );

    await user.click(screen.getByRole("tab", { name: "Workspace" }));
    await waitFor(() =>
      expect(artifactsSpy).toHaveBeenCalledWith(
        expect.objectContaining({ userId: USER_ID, tenantScope: mockScope }),
      ),
    );
  });

  it("titles an unnamed employee by their email, not their OIDC sub", async () => {
    // An employee's subject_id is a Keycloak sub UUID and display_name is
    // usually unset — the title used to be an unreadable UUID even though
    // the member row carries their email.
    stubCommon();
    vi.spyOn(usersSdk, "getTenantUser").mockResolvedValue({
      user_id: USER_ID,
      subject_id: "6344ed9a-6f6e-455a-8eb3-a03a26da1639",
      display_name: null,
      subject_type: "user",
      is_member: true,
      member_email: "alice@corp.com",
      member_role: "admin",
      created_at: "2026-06-01T00:00:00Z",
      last_active_at: "2026-07-01T00:00:00Z",
    });
    renderPage();
    expect(await screen.findByText("alice@corp.com")).toBeInTheDocument();
  });

  it("titles an external caller by the id their app passed in", async () => {
    // No member row, no email — subject_id is already the recognizable name.
    stubCommon();
    vi.spyOn(usersSdk, "getTenantUser").mockResolvedValue({
      user_id: USER_ID,
      subject_id: "customer-8891",
      display_name: null,
      subject_type: "user",
      is_member: false,
      member_email: null,
      member_role: null,
      created_at: "2026-06-01T00:00:00Z",
      last_active_at: "2026-07-01T00:00:00Z",
    });
    renderPage();
    // subject_id also renders as the copyable identifier — assert the title.
    expect(await screen.findByRole("heading", { name: "customer-8891" })).toBeInTheDocument();
  });

  it("sorts memory by importance (descending) by default", async () => {
    stubCommon();
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Alice");
    await user.click(screen.getByRole("tab", { name: "Memory" }));

    const table = await screen.findByTestId("user-memory-table");
    // Default descending sort → the high-importance row comes first.
    const rows = within(table).getAllByRole("row");
    // rows[0] is the header; rows[1] is the first data row.
    expect(rows[1]).toHaveTextContent("High importance memory");
  });

  it("edits a memory through the modal, threading the userId", async () => {
    stubCommon();
    vi.spyOn(memorySdk, "updateMemory").mockResolvedValue(HIGH);
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Alice");
    await user.click(screen.getByRole("tab", { name: "Memory" }));

    await user.click(await screen.findByTestId(`memory-edit-${HIGH.id}`));
    const textarea = await screen.findByTestId("memory-edit-content");
    await user.clear(textarea);
    await user.type(textarea, "Corrected content");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(memorySdk.updateMemory).toHaveBeenCalledWith(
        HIGH.id,
        expect.objectContaining({ content: "Corrected content", kind: "fact" }),
        USER_ID,
      ),
    );
  });

  it("forgets a memory, threading the userId", async () => {
    stubCommon();
    vi.spyOn(memorySdk, "deleteMemory").mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Alice");
    await user.click(screen.getByRole("tab", { name: "Memory" }));

    await user.click(await screen.findByTestId(`memory-forget-${LOW.id}`));
    // The Popconfirm confirm button (rendered last in a portal).
    const confirms = screen.getAllByRole("button", { name: "Forget" });
    await user.click(confirms[confirms.length - 1]);

    await waitFor(() =>
      expect(memorySdk.deleteMemory).toHaveBeenCalledWith(LOW.id, USER_ID),
    );
  });

  it("purges an external user only after typing the subject_id to confirm", async () => {
    stubCommon();
    const purgeSpy = vi.spyOn(usersSdk, "purgeUser").mockResolvedValue(OK_SUMMARY);
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Alice");

    await user.click(screen.getByTestId("user-purge-btn"));
    const ok = await screen.findByTestId("purge-confirm-ok");
    expect(ok).toBeDisabled(); // armed only on an exact subject_id match
    // The caller ("someone-else") isn't the target ("ext-alice") — no self warning.
    expect(screen.queryByTestId("purge-self-warning")).not.toBeInTheDocument();

    const input = screen.getByTestId("purge-confirm-input");
    await user.type(input, "wrong");
    expect(ok).toBeDisabled();
    await user.clear(input);
    await user.type(input, "ext-alice");
    expect(ok).toBeEnabled();

    await user.click(ok);
    await waitFor(() => expect(purgeSpy).toHaveBeenCalledWith(USER_ID));
    // Success navigates back to the roster.
    expect(await screen.findByTestId("users-roster-sentinel")).toBeInTheDocument();
  });

  it("keeps the modal open on a partial purge so retry stays actionable", async () => {
    stubCommon();
    vi.spyOn(usersSdk, "purgeUser").mockResolvedValue({
      ...OK_SUMMARY,
      ok: false,
      failures: { workspace: "no supervisor client wired" },
    });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Alice");

    await user.click(screen.getByTestId("user-purge-btn"));
    await user.type(await screen.findByTestId("purge-confirm-input"), "ext-alice");
    await user.click(screen.getByTestId("purge-confirm-ok"));

    await waitFor(() => expect(usersSdk.purgeUser).toHaveBeenCalledWith(USER_ID));
    // Partial failure does not navigate away — the modal stays open to retry.
    expect(screen.queryByTestId("users-roster-sentinel")).not.toBeInTheDocument();
    expect(screen.getByTestId("purge-confirm-input")).toBeInTheDocument();
  });

  it("shows a defensive warning if the purge endpoint still returns 409 (legacy guard)", async () => {
    // Purging is decoupled from account deletion — the backend no longer
    // 409s for employees — but the client-side handler stays as a defensive
    // fallback in case some other conflict ever surfaces here.
    stubCommon();
    vi.spyOn(usersSdk, "purgeUser").mockRejectedValue(
      new ApiError("member", "CONFLICT", 409),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Alice");

    await user.click(screen.getByTestId("user-purge-btn"));
    await user.type(await screen.findByTestId("purge-confirm-input"), "ext-alice");
    await user.click(screen.getByTestId("purge-confirm-ok"));

    // A warning modal directs the admin to the members page; no navigation.
    expect(await screen.findByText(/members page/i)).toBeInTheDocument();
    expect(screen.queryByTestId("users-roster-sentinel")).not.toBeInTheDocument();
  });

  it("enables the purge button for an employee (member) and purges their data", async () => {
    // The disabled-button + members-page tooltip is gone: an employee's
    // *data* is purgeable from here exactly like an external user's.
    stubCommon();
    const purgeSpy = vi.spyOn(usersSdk, "purgeUser").mockResolvedValue(OK_SUMMARY);
    const user = userEvent.setup();
    renderPage({ isMember: true });
    await screen.findByText("Alice");

    const btn = screen.getByTestId("user-purge-btn");
    expect(btn).toBeEnabled();
    await user.click(btn);
    await user.type(await screen.findByTestId("purge-confirm-input"), "ext-alice");
    await user.click(screen.getByTestId("purge-confirm-ok"));

    await waitFor(() => expect(purgeSpy).toHaveBeenCalledWith(USER_ID));
    expect(await screen.findByTestId("users-roster-sentinel")).toBeInTheDocument();
  });

  it("shows a reinforced warning when the target is the caller's own data", async () => {
    stubCommon();
    mockIdentitySubject = "ext-alice"; // matches the resolved target subject_id
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Alice");

    await user.click(screen.getByTestId("user-purge-btn"));
    expect(await screen.findByTestId("purge-self-warning")).toBeInTheDocument();
  });
});
