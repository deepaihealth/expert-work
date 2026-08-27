/**
 * AgentsList page tests — product-grade pass.
 *
 * Stubs ``listAgents`` so row navigation, status localisation, the
 * conditional tenant column, the owner column, and the search / status
 * filter wiring are exercised in isolation. Mirrors the RunsList harness.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import { MemoryRouter, useLocation } from "react-router-dom";
import "../../i18n";
import i18n from "../../i18n";

import { ApiError, setStoredToken } from "../../api/client";
import * as agentsSdk from "../../api/agents";
import * as membersSdk from "../../api/members";
import { AuthProvider } from "../../auth/AuthContext";
import { TenantScopeProvider } from "../../tenant/TenantScopeContext";
import { AgentsList } from "../AgentsList";
import type { AgentDetailResponse, AgentList } from "../../api/agents";

const listAgentsMock = vi.spyOn(agentsSdk, "listAgents");
const getAgentMock = vi.spyOn(agentsSdk, "getAgent");
const deleteAgentMock = vi.spyOn(agentsSdk, "deleteAgent");
const disableAgentMock = vi.spyOn(agentsSdk, "disableAgent");
const enableAgentMock = vi.spyOn(agentsSdk, "enableAgent");
const listMembersMock = vi.spyOn(membersSdk, "listMembers");

function jwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.`;
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="loc">{location.pathname}</div>;
}

beforeEach(() => {
  setStoredToken(
    jwt({ sub: "u", tenant_id: "11111111-1111-1111-1111-111111111111", roles: ["admin"] }),
  );
  listAgentsMock.mockReset();
  getAgentMock.mockReset();
  deleteAgentMock.mockReset();
  disableAgentMock.mockReset();
  enableAgentMock.mockReset();
  listMembersMock.mockReset();
  // ① 所有者列的成员名映射:默认空成员表 = 原样显示,既有断言不受影响。
  listMembersMock.mockResolvedValue({ items: [], total: 0 });
  // Default: the row-menu's lazy kill-switch fetch resolves "enabled" unless
  // a test overrides it — keeps tests that never care about this state safe
  // from calling .then() on an unmocked (undefined) return value.
  getAgentMock.mockResolvedValue({
    record: { ...sampleRow, spec: {} },
    disabled: false,
    disable: null,
  });
});

afterEach(() => {
  setStoredToken(null);
  vi.clearAllMocks();
});

function renderAgentsList() {
  return render(
    <MemoryRouter initialEntries={["/agents"]}>
      <AuthProvider>
        <TenantScopeProvider>
          <App>
            <AgentsList />
            <LocationProbe />
          </App>
        </TenantScopeProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

const sampleRow: AgentList["items"][0] = {
  id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
  tenant_id: "22222222-2222-2222-2222-222222222222",
  name: "customer-support-bot",
  version: "3.4.2",
  status: "active",
  spec_sha256: "a".repeat(64),
  created_by: "alice@acme.com",
  created_at: "2026-04-12T09:00:00Z",
  updated_at: "2026-05-25T07:00:00Z",
};

describe("AgentsList", () => {
  it("renders name, version and owner", async () => {
    listAgentsMock.mockResolvedValue({ items: [sampleRow], total: 1, cross_tenant: false });
    renderAgentsList();
    await waitFor(() => expect(listAgentsMock).toHaveBeenCalled());
    expect(await screen.findByText("customer-support-bot")).toBeInTheDocument();
    expect(screen.getByText("v3.4.2")).toBeInTheDocument();
    expect(screen.getByText("alice@acme.com")).toBeInTheDocument();
  });

  it("opens the agent overview when a row is clicked", async () => {
    listAgentsMock.mockResolvedValue({ items: [sampleRow], total: 1, cross_tenant: false });
    renderAgentsList();
    fireEvent.click(await screen.findByText("customer-support-bot"));
    expect(screen.getByTestId("loc")).toHaveTextContent(
      "/agents/customer-support-bot/3.4.2/overview",
    );
  });

  it("hides the tenant column in a single tenant, shows it cross-tenant", async () => {
    listAgentsMock.mockResolvedValue({ items: [sampleRow], total: 1, cross_tenant: false });
    const { unmount } = renderAgentsList();
    await screen.findByText("customer-support-bot");
    const tenantHeader = i18n.t("agents_page.column_tenant");
    expect(screen.queryByText(tenantHeader)).toBeNull();
    unmount();

    listAgentsMock.mockResolvedValue({ items: [sampleRow], total: 1, cross_tenant: true });
    renderAgentsList();
    expect(await screen.findByTestId("cross-tenant-banner")).toBeInTheDocument();
    expect(screen.getByText(tenantHeader)).toBeInTheDocument();
  });

  it("localises the status tag (zh-CN)", async () => {
    await i18n.changeLanguage("zh-CN");
    listAgentsMock.mockResolvedValue({ items: [sampleRow], total: 1, cross_tenant: false });
    renderAgentsList();
    expect(await screen.findByText("活跃")).toBeInTheDocument();
    await i18n.changeLanguage("en");
  });

  it("first listAgents call carries no name or status filter", async () => {
    listAgentsMock.mockResolvedValue({ items: [], total: 0, cross_tenant: false });
    renderAgentsList();
    await waitFor(() => expect(listAgentsMock).toHaveBeenCalledTimes(1));
    expect(listAgentsMock).toHaveBeenLastCalledWith(
      expect.objectContaining({ name: undefined, status: undefined }),
    );
    // Antd Select/portal interaction is unreliable in jsdom — assert the
    // controls exist; the SDK wiring is covered by the effect's deps.
    expect(screen.getByTestId("agents-search")).toBeInTheDocument();
    expect(screen.getByTestId("agents-status-filter")).toBeInTheDocument();
  });

  it("renders an error Alert when listAgents rejects", async () => {
    listAgentsMock.mockRejectedValue(new ApiError("DB down", "DB_DOWN", 500));
    renderAgentsList();
    expect(await screen.findByTestId("agents-error")).toHaveTextContent("DB_DOWN");
  });

  // Soft-deleted versions are history, not working agents — the everyday
  // ("default") view drops them client-side even though the unfiltered
  // fetch returns them.
  it("the default view hides soft-deleted rows", async () => {
    const deletedRow = { ...sampleRow, name: "retired-bot", status: "deleted" };
    listAgentsMock.mockResolvedValue({
      items: [sampleRow, deletedRow],
      total: 2,
      cross_tenant: false,
    });
    renderAgentsList();
    await screen.findByText("customer-support-bot");
    expect(screen.queryByText("retired-bot")).toBeNull();
  });
});

// ① 所有者列 UUID → 成员名(display_name,空则 email);未命中(服务账号名 /
// 查不到)原样显示;成员接口失败静默降级,绝不弹错。
describe("AgentsList owner column member-name mapping", () => {
  const ownerUuid = "33333333-3333-3333-3333-333333333333";
  const member: membersSdk.TenantMember = {
    id: "44444444-4444-4444-4444-444444444444",
    tenant_id: sampleRow.tenant_id,
    email: "ming@acme.com",
    display_name: "王小明",
    role: "admin",
    status: "active",
    keycloak_user_id: null,
    subject_id: ownerUuid,
    invited_by: "seed",
    invited_at: null,
    activated_at: null,
    updated_at: null,
  };

  it("maps a created_by subject UUID to the member display name, raw id in title", async () => {
    listAgentsMock.mockResolvedValue({
      items: [{ ...sampleRow, created_by: ownerUuid }],
      total: 1,
      cross_tenant: false,
    });
    listMembersMock.mockResolvedValue({ items: [member], total: 1 });
    renderAgentsList();
    const name = await screen.findByText("王小明");
    expect(name.closest("[title]")).toHaveAttribute("title", ownerUuid);
    expect(screen.queryByText(ownerUuid)).toBeNull();
  });

  it("falls back to the member email when display_name is empty", async () => {
    listAgentsMock.mockResolvedValue({
      items: [{ ...sampleRow, created_by: ownerUuid }],
      total: 1,
      cross_tenant: false,
    });
    listMembersMock.mockResolvedValue({
      items: [{ ...member, display_name: null }],
      total: 1,
    });
    renderAgentsList();
    expect(await screen.findByText("ming@acme.com")).toBeInTheDocument();
  });

  it("keeps a service-account name (no member match) as-is", async () => {
    listAgentsMock.mockResolvedValue({
      items: [{ ...sampleRow, created_by: "seed-canary" }],
      total: 1,
      cross_tenant: false,
    });
    listMembersMock.mockResolvedValue({ items: [member], total: 1 });
    renderAgentsList();
    expect(await screen.findByText("seed-canary")).toBeInTheDocument();
  });

  it("degrades silently to the raw id when listMembers fails — no error banner", async () => {
    listAgentsMock.mockResolvedValue({
      items: [{ ...sampleRow, created_by: ownerUuid }],
      total: 1,
      cross_tenant: false,
    });
    listMembersMock.mockRejectedValue(new ApiError("nope", "FORBIDDEN", 403));
    renderAgentsList();
    expect(await screen.findByText(ownerUuid)).toBeInTheDocument();
    expect(screen.queryByTestId("agents-error")).toBeNull();
  });
});

describe("AgentsList row actions", () => {
  function detailFor(disabled: boolean): AgentDetailResponse {
    return {
      record: { ...sampleRow, spec: {} },
      disabled,
      disable: disabled
        ? {
            tenant_id: sampleRow.tenant_id,
            agent_name: sampleRow.name,
            disabled: true,
            reason: null,
            disabled_by: "admin",
            disabled_at: "2026-07-01T00:00:00Z",
            updated_at: "2026-07-01T00:00:00Z",
          }
        : null,
    };
  }

  async function openRowMenu() {
    const user = userEvent.setup();
    listAgentsMock.mockResolvedValue({ items: [sampleRow], total: 1, cross_tenant: false });
    renderAgentsList();
    await screen.findByText("customer-support-bot");
    await user.click(screen.getByTestId(`agent-row-actions-${sampleRow.name}`));
    return user;
  }

  it("renders the delete and disable actions in the row menu", async () => {
    await openRowMenu();
    expect(await screen.findByText(i18n.t("agents_page.action_delete"))).toBeInTheDocument();
    expect(screen.getByText(i18n.t("agents_page.action_disable"))).toBeInTheDocument();
    expect(screen.queryByText(i18n.t("agents_page.action_enable"))).not.toBeInTheDocument();
    // Pre-existing actions are unaffected.
    expect(screen.getByText(i18n.t("agents_page.action_playground"))).toBeInTheDocument();
  });

  // A soft-deleted version can't be played/edited/disabled/re-deleted —
  // its menu keeps only the read-only run history entry. (Reached via the
  // "deleted" status filter; the default view hides such rows entirely.)
  it("a deleted row's menu offers only the run history action", async () => {
    const user = userEvent.setup();
    const deletedRow = { ...sampleRow, status: "deleted" };
    listAgentsMock.mockResolvedValue({
      items: [deletedRow],
      total: 1,
      cross_tenant: false,
    });
    renderAgentsList();
    // The default view hides deleted rows — switch the status filter to
    // "deleted" to surface it (mirrors how a user reaches such a row).
    await waitFor(() => expect(listAgentsMock).toHaveBeenCalled());
    const selector = screen
      .getByTestId("agents-status-filter")
      .querySelector(".ant-select-selector");
    fireEvent.mouseDown(selector as Element);
    await user.click(
      await screen.findByTitle(i18n.t("agents_page.status_deleted")),
    );
    await screen.findByText("customer-support-bot");
    await user.click(screen.getByTestId(`agent-row-actions-${deletedRow.name}`));

    expect(await screen.findByText(i18n.t("agents_page.action_runs"))).toBeInTheDocument();
    expect(screen.queryByText(i18n.t("agents_page.action_delete"))).toBeNull();
    expect(screen.queryByText(i18n.t("agents_page.action_disable"))).toBeNull();
    expect(screen.queryByText(i18n.t("agents_page.action_playground"))).toBeNull();
    expect(screen.queryByText(i18n.t("agents_page.action_edit"))).toBeNull();
  });

  it("arms the delete confirm only once the exact agent name is typed", async () => {
    const user = await openRowMenu();
    await user.click(screen.getByText(i18n.t("agents_page.action_delete")));

    const ok = await screen.findByTestId("delete-agent-confirm-ok");
    expect(ok).toBeDisabled(); // armed only on an exact name match

    const input = screen.getByTestId("delete-agent-confirm-input");
    await user.type(input, "wrong-name");
    expect(ok).toBeDisabled();
    await user.clear(input);
    await user.type(input, sampleRow.name);
    expect(ok).toBeEnabled();
  });

  it("deletes the agent once armed, then refreshes the list", async () => {
    deleteAgentMock.mockResolvedValue(undefined);
    const user = await openRowMenu();
    await user.click(screen.getByText(i18n.t("agents_page.action_delete")));

    const input = await screen.findByTestId("delete-agent-confirm-input");
    await user.type(input, sampleRow.name);
    await user.click(screen.getByTestId("delete-agent-confirm-ok"));

    await waitFor(() =>
      expect(deleteAgentMock).toHaveBeenCalledWith(sampleRow.name, sampleRow.version),
    );
    // initial load + post-delete refresh
    await waitFor(() => expect(listAgentsMock).toHaveBeenCalledTimes(2));
  });

  it("shows an error and keeps the modal open (retryable) when delete fails", async () => {
    deleteAgentMock.mockRejectedValue(new ApiError("nope", "FORBIDDEN", 403));
    const user = await openRowMenu();
    await user.click(screen.getByText(i18n.t("agents_page.action_delete")));

    const input = await screen.findByTestId("delete-agent-confirm-input");
    await user.type(input, sampleRow.name);
    await user.click(screen.getByTestId("delete-agent-confirm-ok"));

    await waitFor(() => expect(deleteAgentMock).toHaveBeenCalled());
    // Failure does not refresh or close the modal — it stays open to retry.
    expect(listAgentsMock).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("delete-agent-confirm-input")).toBeInTheDocument();
  });

  it("disables the agent after confirming, and refreshes the list", async () => {
    disableAgentMock.mockResolvedValue({
      name: sampleRow.name,
      disabled: true,
      cancelled_runs: 2,
    });
    const user = await openRowMenu();
    await user.click(screen.getByText(i18n.t("agents_page.action_disable")));

    const confirmOk = await screen.findByRole("button", {
      name: i18n.t("agents_page.action_disable"),
    });
    await user.click(confirmOk);

    await waitFor(() => expect(disableAgentMock).toHaveBeenCalledWith(sampleRow.name));
    await waitFor(() => expect(listAgentsMock).toHaveBeenCalledTimes(2));
  });

  it("swaps to Enable once the lazily-fetched kill-switch state is disabled, and calls enableAgent", async () => {
    getAgentMock.mockResolvedValue(detailFor(true));
    enableAgentMock.mockResolvedValue({ name: sampleRow.name, disabled: false });
    const user = await openRowMenu();

    const enableItem = await screen.findByText(i18n.t("agents_page.action_enable"));
    expect(screen.queryByText(i18n.t("agents_page.action_disable"))).not.toBeInTheDocument();

    await user.click(enableItem);
    await waitFor(() => expect(enableAgentMock).toHaveBeenCalledWith(sampleRow.name));
    await waitFor(() => expect(listAgentsMock).toHaveBeenCalledTimes(2));
  });
});
