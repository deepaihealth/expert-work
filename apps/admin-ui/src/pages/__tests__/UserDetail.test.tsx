/**
 * UserDetail tests — the (agent, user) instance page
 * (conversation-centric IA M2).
 *
 * Stubs all four pane SDKs; asserts each tab assembles its per-user
 * data and that a pane failure stays contained to its own tab.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import "../../i18n";

import * as convoSdk from "../../api/conversations";
import * as memorySdk from "../../api/memory";
import * as artifactsSdk from "../../api/artifacts";
import * as usageSdk from "../../api/usage";
import * as usersSdk from "../../api/users";
import { UserDetail } from "../UserDetail";

let mockScope: string | undefined;
// Spread the real module so the page keeps the real ``concreteTenantScope``
// ("*" → undefined) instead of a test-local copy that could drift.
vi.mock("../../tenant/TenantScopeContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../tenant/TenantScopeContext")>()),
  useTenantScope: () => ({ scope: mockScope, apiTenantScope: mockScope }),
}));

const USER_ID = "aaaaaaaa-0000-0000-0000-000000000001";

function renderPage() {
  return render(
    <MemoryRouter initialEntries={[`/agents/support-bot/1.0.0/users/${USER_ID}`]}>
      <Routes>
        <Route path="/agents/:name/:version/users/:userId" element={<UserDetail />} />
      </Routes>
    </MemoryRouter>,
  );
}

function stubAll() {
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
  vi.spyOn(convoSdk, "listConversations").mockResolvedValue({
    items: [
      {
        thread_id: "33333333-3333-3333-3333-333333333333",
        tenant_id: "t",
        user_id: USER_ID,
        agent_name: "support-bot",
        agent_version: "1.0.0",
        title: "refund question",
        status: "active",
        created_at: null,
        updated_at: null,
        run_count: 2,
        error_count: 0,
        pending_count: 0,
        last_run_at: "2026-06-30T12:00:00Z",
        tokens: null,
      },
    ],
    total: 1,
    cross_tenant: false,
  });
  vi.spyOn(memorySdk, "listMemories").mockResolvedValue({
    items: [
      {
        id: "m1",
        tenant_id: "t",
        user_id: USER_ID,
        kind: "fact",
        content: "Prefers email contact",
        created_at: "2026-06-30T10:00:00Z",
        updated_at: "2026-06-30T10:00:00Z",
      } as never,
    ],
    total: 1,
    cross_tenant: false,
  });
  vi.spyOn(artifactsSdk, "listArtifacts").mockResolvedValue({
    items: [{ name: "report.md", kind: "document", latest_version: 2 }],
    cross_tenant: false,
  });
  vi.spyOn(usageSdk, "getUsageTokens").mockResolvedValue({
    month: "2026-07",
    as_of: "2026-07-02T00:00:00Z",
    realtime: true,
    total: {
      input_tokens: 1200,
      output_tokens: 300,
      cache_creation_tokens: 0,
      cache_read_tokens: 0,
    },
    by_agent: [],
    by_model: [
      {
        key: "m1",
        input_tokens: 1200,
        output_tokens: 300,
        cache_creation_tokens: 0,
        cache_read_tokens: 0,
      },
    ],
    by_kind: [],
  });
}

afterEach(() => {
  mockScope = undefined;
  vi.restoreAllMocks();
});

describe("UserDetail", () => {
  it("renders the conversations tab with the agent+user filter applied", async () => {
    stubAll();
    renderPage();
    expect(await screen.findByText("refund question")).toBeInTheDocument();
    expect(convoSdk.listConversations).toHaveBeenCalledWith(
      expect.objectContaining({
        agentName: "support-bot",
        agentVersion: "1.0.0",
        userId: USER_ID,
      }),
    );
  });

  it("assembles memory / artifacts / usage panes per user", async () => {
    stubAll();
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("refund question");

    await user.click(screen.getByRole("tab", { name: "Memory" }));
    expect(await screen.findByText("Prefers email contact")).toBeInTheDocument();
    expect(memorySdk.listMemories).toHaveBeenCalledWith(
      expect.objectContaining({ userId: USER_ID }),
    );

    await user.click(screen.getByRole("tab", { name: "Artifacts" }));
    expect(await screen.findByText("report.md")).toBeInTheDocument();
    expect(artifactsSdk.listArtifacts).toHaveBeenCalledWith(
      expect.objectContaining({ userId: USER_ID }),
    );

    await user.click(screen.getByRole("tab", { name: "Usage" }));
    await waitFor(() =>
      expect(screen.getByTestId("user-usage-total")).toHaveTextContent("1.5k"),
    );
    expect(usageSdk.getUsageTokens).toHaveBeenCalledWith(
      expect.objectContaining({ userId: USER_ID }),
    );
  });

  it("contains a pane failure to its own tab", async () => {
    stubAll();
    vi.spyOn(memorySdk, "listMemories").mockRejectedValue(new Error("403"));
    const user = userEvent.setup();
    renderPage();
    // Conversations still render…
    expect(await screen.findByText("refund question")).toBeInTheDocument();
    // …and the memory tab shows its own error without killing the page.
    await user.click(screen.getByRole("tab", { name: "Memory" }));
    const pane = await screen.findByTestId("user-memory-pane");
    expect(pane).toHaveTextContent("403");
    expect(screen.getByTestId("user-detail-root")).toBeInTheDocument();
  });

  it("resolves the display name from the registry on a direct URL open", async () => {
    stubAll();
    // No router state (direct URL / refresh) — the fast-follow
    // GET /v1/users/{id} supplies the human name for the title.
    renderPage();
    expect(await screen.findByText("Alice")).toBeInTheDocument();
    expect(usersSdk.getTenantUser).toHaveBeenCalledWith(USER_ID, undefined);
  });

  it("threads the tenant scope through getTenantUser (跨租户钻取 B类补传)", async () => {
    mockScope = "22222222-2222-2222-2222-222222222222";
    stubAll();
    renderPage();
    await waitFor(() =>
      expect(usersSdk.getTenantUser).toHaveBeenCalledWith(USER_ID, mockScope),
    );
  });

  it('maps the "*" aggregate scope to no tenant_id (backend 422s a literal "*")', async () => {
    mockScope = "*";
    stubAll();
    renderPage();
    await waitFor(() =>
      expect(usersSdk.getTenantUser).toHaveBeenCalledWith(USER_ID, undefined),
    );
  });
});
