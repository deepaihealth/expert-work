/**
 * B-278 — the detail page of a deleted agent version.
 *
 * Renders the real AgentDetail (route params + getAgent) and checks: the
 * 「已删除」banner and read-only page, restore / purge only for tenant admins,
 * greyed out in a switched tenant, and the restore flow (confirm → SDK →
 * reload, triggers-still-off notice).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { App } from "antd";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../i18n";

import { AgentDetail } from "../AgentDetail";
import type { AgentDetailResponse } from "../../api/agents";
import { getAgent, restoreAgent } from "../../api/agents";

vi.mock("../../api/agents", async () => {
  const actual = await vi.importActual<typeof import("../../api/agents")>("../../api/agents");
  return {
    ...actual,
    getAgent: vi.fn(),
    restoreAgent: vi.fn(),
    getAgentPurgePreview: vi.fn(() => new Promise(() => {})),
    purgeAgent: vi.fn(),
  };
});

const { isTenantSwitchedMock, rolesRef } = vi.hoisted(() => ({
  isTenantSwitchedMock: vi.fn(() => false),
  rolesRef: { current: ["admin"] as string[] },
}));
const scopeRef = vi.hoisted(() => ({ current: undefined as string | undefined }));
vi.mock("../../tenant/TenantScopeContext", async (importOriginal) => {
  const { mockTenantScopeModule } = await import("../../test-utils/tenantScopeMock");
  return mockTenantScopeModule(
    await importOriginal<typeof import("../../tenant/TenantScopeContext")>(),
    scopeRef,
  );
});
vi.mock("../../tenant/useIsTenantSwitched", () => ({
  useIsTenantSwitched: isTenantSwitchedMock,
}));
vi.mock("../../auth/AuthContext", () => ({
  useAuth: () => ({ identity: { isSystemAdmin: false, roles: rolesRef.current } }),
}));

function detail(status = "deleted"): AgentDetailResponse {
  return {
    record: {
      id: "11111111-1111-1111-1111-111111111111",
      tenant_id: "22222222-2222-2222-2222-222222222222",
      name: "code-reviewer",
      version: "1.0.0",
      status,
      spec_sha256: "a".repeat(64),
      created_by: "user-1",
      created_at: "2026-06-12T00:00:00Z",
      updated_at: "2026-06-12T00:00:00Z",
      spec: {},
    },
    disabled: false,
    disable: null,
  } as AgentDetailResponse;
}

function renderPage(tab = "overview"): void {
  render(
    <MemoryRouter initialEntries={[`/agents/code-reviewer/1.0.0/${tab}`]}>
      <App>
        <Routes>
          <Route path="/agents/:name/:version/:tab" element={<AgentDetail />} />
          <Route path="/triggers" element={<div data-testid="triggers-page" />} />
        </Routes>
      </App>
    </MemoryRouter>,
  );
}

afterEach(() => vi.clearAllMocks());

beforeEach(() => {
  isTenantSwitchedMock.mockReturnValue(false);
  rolesRef.current = ["admin"];
});

describe("AgentDetail — deleted agent (B-278)", () => {
  it("shows the deleted banner with restore + purge and hides the kill switch", async () => {
    vi.mocked(getAgent).mockResolvedValue(detail());
    renderPage();

    const banner = await screen.findByTestId("agent-deleted-banner");
    expect(banner).toHaveTextContent("This agent has been deleted");
    expect(within(banner).getByTestId("agent-restore-btn")).toBeEnabled();
    expect(within(banner).getByTestId("agent-purge-btn")).toBeEnabled();
    expect(screen.queryByTestId("agent-disable-btn")).toBeNull();
    expect(screen.queryByTestId("agent-enable-btn")).toBeNull();
  });

  it("shows no banner and keeps the kill switch for a live agent", async () => {
    vi.mocked(getAgent).mockResolvedValue(detail("active"));
    renderPage();

    expect(await screen.findByTestId("agent-disable-btn")).toBeInTheDocument();
    expect(screen.queryByTestId("agent-deleted-banner")).toBeNull();
  });

  it("hides restore / purge from a non-admin but still says it is deleted", async () => {
    rolesRef.current = ["viewer"];
    vi.mocked(getAgent).mockResolvedValue(detail());
    renderPage();

    const banner = await screen.findByTestId("agent-deleted-banner");
    expect(banner).toHaveTextContent("Only a tenant admin");
    expect(screen.queryByTestId("agent-restore-btn")).toBeNull();
    expect(screen.queryByTestId("agent-purge-btn")).toBeNull();
  });

  it("greys out restore / purge in a switched tenant (they act on the home tenant)", async () => {
    isTenantSwitchedMock.mockReturnValue(true);
    vi.mocked(getAgent).mockResolvedValue(detail());
    renderPage();

    expect(await screen.findByTestId("agent-restore-btn")).toBeDisabled();
    expect(screen.getByTestId("agent-purge-btn")).toBeDisabled();
  });

  it("replaces the playground with a notice instead of a runnable console", async () => {
    vi.mocked(getAgent).mockResolvedValue(detail());
    renderPage("playground");

    expect(await screen.findByTestId("agent-playground-deleted")).toHaveTextContent(
      "A deleted agent can't be run",
    );
  });

  it("restore: confirm → POST restore → reload, and points at still-disabled triggers", async () => {
    const user = userEvent.setup();
    vi.mocked(getAgent).mockResolvedValue(detail());
    vi.mocked(restoreAgent).mockResolvedValue({
      name: "code-reviewer",
      version: "1.0.0",
      status: "active",
      triggers_disabled: 2,
    });
    renderPage();

    await user.click(await screen.findByTestId("agent-restore-btn"));
    const confirm = await screen.findByRole("dialog");
    expect(confirm).toHaveTextContent("Restore code-reviewer v1.0.0?");
    expect(restoreAgent).not.toHaveBeenCalled();
    await user.click(within(confirm).getByRole("button", { name: "Restore" }));

    await waitFor(() => expect(restoreAgent).toHaveBeenCalledWith("code-reviewer", "1.0.0"));
    // Reloaded the detail after the restore (initial load + refresh).
    await waitFor(() => expect(getAgent).toHaveBeenCalledTimes(2));
    expect(
      await screen.findByText(/2 scheduled task\(s\) \(triggers\) are still disabled/),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open Triggers" }));
    expect(await screen.findByTestId("triggers-page")).toBeInTheDocument();
  });

  // Contract update: a purge that didn't finish leaves the name ``purging`` —
  // read-only like deleted, can't be restored, purge again to finish.
  it("purging: read-only, says the purge didn't finish, offers purge but not restore", async () => {
    vi.mocked(getAgent).mockResolvedValue(detail("purging"));
    renderPage();

    const banner = await screen.findByTestId("agent-deleted-banner");
    expect(banner).toHaveTextContent("The last purge of this agent didn't finish");
    expect(within(banner).queryByTestId("agent-restore-btn")).toBeNull();
    expect(within(banner).getByTestId("agent-purge-btn")).toBeEnabled();
    expect(screen.queryByTestId("agent-disable-btn")).toBeNull();
  });

  it("purging: the playground is not runnable either", async () => {
    vi.mocked(getAgent).mockResolvedValue(detail("purging"));
    renderPage("playground");
    expect(await screen.findByTestId("agent-playground-deleted")).toBeInTheDocument();
  });

  it("restore with no disabled triggers shows no trigger notice", async () => {
    const user = userEvent.setup();
    vi.mocked(getAgent).mockResolvedValue(detail());
    vi.mocked(restoreAgent).mockResolvedValue({
      name: "code-reviewer",
      version: "1.0.0",
      status: "active",
      triggers_disabled: 0,
    });
    renderPage();

    await user.click(await screen.findByTestId("agent-restore-btn"));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Restore" }));
    await waitFor(() => expect(getAgent).toHaveBeenCalledTimes(2));
    expect(screen.queryByText(/still disabled/)).toBeNull();
  });
});
