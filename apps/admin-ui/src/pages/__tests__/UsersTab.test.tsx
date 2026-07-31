/**
 * UsersTab tests — the per-agent users rollup (conversation-centric IA M2).
 *
 * Stubs ``listAgentUsers``; asserts the rollup columns (display name
 * fallback, run/error signals, compact tokens) and the drill-down
 * navigation target with the display name riding on router state.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import "../../i18n";

import * as usersSdk from "../../api/users";
import { UsersTab } from "../agent_detail/UsersTab";
import type { AgentDetailResponse } from "../../api/agents";
import type { AgentUserItem } from "../../api/users";

// Stream N — the tab reads the ambient tenant scope; these tests don't
// mount a TenantScopeProvider, so mock it (mutable for passthrough asserts).
const { tenantScopeRef } = vi.hoisted(() => ({
  tenantScopeRef: { current: undefined as string | undefined },
}));
vi.mock("../../tenant/TenantScopeContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../tenant/TenantScopeContext")>()),
  useTenantScope: () => ({
    scope: tenantScopeRef.current ?? "home",
    setScope: () => {},
    apiTenantScope: tenantScopeRef.current,
  }),
}));

const DETAIL = {
  record: { name: "support-bot", version: "1.0.0" },
} as unknown as AgentDetailResponse;

const ALICE: AgentUserItem = {
  user_id: "aaaaaaaa-0000-0000-0000-000000000001",
  display_name: "Alice",
  conversation_count: 2,
  run_count: 3,
  error_count: 1,
  pending_count: 0,
  last_run_at: "2026-06-30T12:00:00Z",
  tokens: {
    input_tokens: 1200,
    output_tokens: 300,
    cache_creation_tokens: 0,
    cache_read_tokens: 0,
    total_tokens: 1500,
    llm_calls: 3,
    models: ["m1"],
  },
};

const BOB: AgentUserItem = {
  user_id: "bbbbbbbb-0000-0000-0000-000000000002",
  display_name: null,
  conversation_count: 1,
  run_count: 1,
  error_count: 0,
  pending_count: 0,
  last_run_at: null,
  tokens: null,
};

function LocationProbe() {
  const location = useLocation();
  return (
    <div data-testid="probe">
      {location.pathname}|{JSON.stringify(location.state)}
    </div>
  );
}

function renderTab() {
  return render(
    <MemoryRouter initialEntries={["/agents/support-bot/1.0.0/users"]}>
      <Routes>
        <Route path="/agents/:name/:version/users" element={<UsersTab detail={DETAIL} />} />
        <Route path="/agents/:name/:version/users/:userId" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  tenantScopeRef.current = undefined;
  vi.restoreAllMocks();
});

describe("UsersTab", () => {
  it("passes the ambient tenant scope through to the users rollup", async () => {
    tenantScopeRef.current = "99999999-9999-9999-9999-999999999999";
    const listSpy = vi
      .spyOn(usersSdk, "listAgentUsers")
      .mockResolvedValue({ items: [ALICE], total: 1, cross_tenant: false });
    renderTab();
    await waitFor(() =>
      expect(listSpy).toHaveBeenCalledWith("support-bot", "1.0.0", {
        tenantScope: "99999999-9999-9999-9999-999999999999",
      }),
    );
  });

  it("renders the rollup rows with name fallback, error signal + tokens", async () => {
    vi.spyOn(usersSdk, "listAgentUsers").mockResolvedValue({
      items: [ALICE, BOB],
      total: 2,
      cross_tenant: false,
    });
    renderTab();
    expect(await screen.findByText("Alice")).toBeInTheDocument();
    expect(screen.getByText("Unnamed user")).toBeInTheDocument();
    expect(screen.getByTestId(`user-error-${ALICE.user_id}`)).toBeInTheDocument();
    expect(screen.getByTestId(`user-tokens-${ALICE.user_id}`)).toHaveTextContent("1.5k");
  });

  it("drills into the user detail with the display name on router state", async () => {
    vi.spyOn(usersSdk, "listAgentUsers").mockResolvedValue({
      items: [ALICE],
      total: 1,
      cross_tenant: false,
    });
    const user = userEvent.setup();
    renderTab();
    await user.click(await screen.findByText("Alice"));
    await waitFor(() =>
      expect(screen.getByTestId("probe")).toHaveTextContent(
        `/agents/support-bot/1.0.0/users/${ALICE.user_id}`,
      ),
    );
    expect(screen.getByTestId("probe")).toHaveTextContent('"displayName":"Alice"');
  });

  it("surfaces SDK errors in an alert", async () => {
    vi.spyOn(usersSdk, "listAgentUsers").mockRejectedValue(new Error("boom"));
    renderTab();
    expect(await screen.findByTestId("users-tab-error")).toBeInTheDocument();
  });
});
