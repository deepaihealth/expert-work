/**
 * ConversationsList page tests — the global conversation browser
 * (``docs/design/conversation-centric-ia.md`` §3 primitive ③).
 *
 * Stubs ``listConversations`` so the cross-tenant scope, filters, and
 * error states are exercised in isolation. The shared axios stub
 * adapter from ``src/test/setup.ts`` keeps any forgotten network call
 * from hitting the wire.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import "../../i18n";

import { ApiError } from "../../api/client";
import * as agentsSdk from "../../api/agents";
import * as conversationsSdk from "../../api/conversations";
import { AuthProvider } from "../../auth/AuthContext";
import { TenantScopeProvider } from "../../tenant/TenantScopeContext";
import { setStoredToken } from "../../api/client";
import { ConversationsList } from "../ConversationsList";
import type { ConversationListItem } from "../../api/conversations";

const listConversationsMock = vi.spyOn(conversationsSdk, "listConversations");

function jwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.`;
}

beforeEach(() => {
  vi.spyOn(agentsSdk, "listAgents").mockResolvedValue({
    items: [
      { id: "a1", tenant_id: "t", name: "support-bot", version: "1.0.0", status: "active" } as never,
    ],
    total: 1,
    cross_tenant: false,
  } as never);
  setStoredToken(
    jwt({ sub: "u", tenant_id: "11111111-1111-1111-1111-111111111111", roles: ["admin"] }),
  );
  listConversationsMock.mockReset();
});

afterEach(() => {
  setStoredToken(null);
  vi.clearAllMocks();
});

function renderPage(entry = "/conversations") {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <AuthProvider>
        <TenantScopeProvider>
          <ConversationsList />
        </TenantScopeProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

const sampleRow: ConversationListItem = {
  thread_id: "33333333-3333-3333-3333-333333333333",
  tenant_id: "22222222-2222-2222-2222-222222222222",
  user_id: "88888888-8888-8888-8888-888888888888",
  agent_name: "customer-support-bot",
  agent_version: "3.4.2",
  title: "refund question",
  status: "active",
  created_at: "2026-05-26T08:00:00Z",
  updated_at: "2026-05-26T08:00:32Z",
  run_count: 3,
  error_count: 0,
  pending_count: 0,
  last_run_at: "2026-05-26T08:00:30Z",
  tokens: null,
};

describe("ConversationsList", () => {
  it("renders rows with title, agent name + version", async () => {
    listConversationsMock.mockResolvedValue({
      items: [sampleRow],
      total: 1,
      cross_tenant: false,
    });
    renderPage();
    await waitFor(() => expect(listConversationsMock).toHaveBeenCalled());
    expect(await screen.findByText("refund question")).toBeInTheDocument();
    expect(screen.getByText("customer-support-bot")).toBeInTheDocument();
    expect(screen.getByText("v3.4.2")).toBeInTheDocument();
  });

  it("localizes the status cell and the filter label instead of echoing the raw enum", async () => {
    listConversationsMock.mockResolvedValue({
      items: [{ ...sampleRow, status: "paused" }],
      total: 1,
      cross_tenant: false,
    });
    // status=paused in the URL makes the filter Select display that
    // option's label too — both surfaces must show the translation.
    renderPage("/conversations?status=paused");
    // Wait for the async row first — the Select's label renders sync, so a
    // bare findAllByText would resolve at 1 hit before the table loads.
    expect(await screen.findByText("refund question")).toBeInTheDocument();
    expect(screen.getAllByText("Paused")).toHaveLength(2);
    expect(screen.queryByText("paused")).toBeNull();
  });

  it("falls back to the untitled label when title is null", async () => {
    listConversationsMock.mockResolvedValue({
      items: [{ ...sampleRow, title: null }],
      total: 1,
      cross_tenant: false,
    });
    renderPage();
    expect(await screen.findByText("Untitled conversation")).toBeInTheDocument();
  });

  it("shows the cross-tenant banner when the response flag is true", async () => {
    listConversationsMock.mockResolvedValue({
      items: [sampleRow],
      total: 1,
      cross_tenant: true,
    });
    renderPage();
    expect(await screen.findByTestId("cross-tenant-banner")).toBeInTheDocument();
  });

  it("first listConversations call uses no status filter", async () => {
    listConversationsMock.mockResolvedValue({ items: [], total: 0, cross_tenant: false });
    renderPage();
    await waitFor(() => expect(listConversationsMock).toHaveBeenCalledTimes(1));
    expect(listConversationsMock).toHaveBeenLastCalledWith(
      expect.objectContaining({ status: undefined }),
    );
    // The Antd Select renders into a portal; jsdom can't reliably click
    // through its virtual list, so we assert the testid exists at the
    // surface level (status → SDK wiring is covered by the initial-call
    // assertion + the effect's ``statusFilter`` dependency).
    expect(screen.getByTestId("conversations-status-filter")).toBeInTheDocument();
  });

  it("renders error Alert when listConversations rejects", async () => {
    listConversationsMock.mockRejectedValue(new ApiError("DB down", "DB_DOWN", 500));
    renderPage();
    expect(await screen.findByTestId("conversations-error")).toHaveTextContent("DB_DOWN");
  });

  it("renders run rollup with error indicator and compact tokens", async () => {
    listConversationsMock.mockResolvedValue({
      items: [
        {
          ...sampleRow,
          error_count: 2,
          tokens: {
            input_tokens: 1200,
            output_tokens: 300,
            cache_creation_tokens: 0,
            cache_read_tokens: 0,
            total_tokens: 1500,
            llm_calls: 3,
            models: ["m1"],
          },
        },
      ],
      total: 1,
      cross_tenant: false,
    });
    renderPage();
    expect(
      await screen.findByTestId(`conversations-page-error-${sampleRow.thread_id}`),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId(`conversation-tokens-${sampleRow.thread_id}`),
    ).toHaveTextContent("1.5k");
  });

  it("debounces the search box into the q param", async () => {
    const user = userEvent.setup();
    listConversationsMock.mockResolvedValue({ items: [], total: 0, cross_tenant: false });
    renderPage();
    await waitFor(() => expect(listConversationsMock).toHaveBeenCalled());
    await user.type(screen.getByRole("textbox"), "refund");
    await waitFor(() =>
      expect(listConversationsMock).toHaveBeenCalledWith(
        expect.objectContaining({ q: "refund" }),
      ),
    );
  });

  it("applies ?user_id from the URL and shows a clearable filter chip", async () => {
    const uid = "99999999-9999-9999-9999-999999999999";
    listConversationsMock.mockResolvedValue({ items: [], total: 0, cross_tenant: false });
    renderPage(`/conversations?user_id=${uid}`);
    await waitFor(() =>
      expect(listConversationsMock).toHaveBeenCalledWith(
        expect.objectContaining({ userId: uid }),
      ),
    );
    expect(screen.getByTestId("conversations-user-filter-chip")).toBeInTheDocument();
  });

  it("clicking a conversation's user filters the list to that user", async () => {
    const uid = "77777777-7777-7777-7777-777777777777";
    listConversationsMock.mockResolvedValue({
      items: [{ ...sampleRow, user_id: uid }],
      total: 1,
      cross_tenant: false,
    });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId(`conversation-user-${uid}`));
    await waitFor(() =>
      expect(listConversationsMock).toHaveBeenCalledWith(
        expect.objectContaining({ userId: uid }),
      ),
    );
  });

  it("errors-only checkbox flows into the hasError param", async () => {
    const user = userEvent.setup();
    listConversationsMock.mockResolvedValue({ items: [], total: 0, cross_tenant: false });
    renderPage();
    await waitFor(() => expect(listConversationsMock).toHaveBeenCalled());
    await user.click(screen.getByTestId("conversations-errors-only"));
    await waitFor(() =>
      expect(listConversationsMock).toHaveBeenCalledWith(
        expect.objectContaining({ hasError: true }),
      ),
    );
  });

  it("awaiting-approval checkbox flows into the hasPending param", async () => {
    const user = userEvent.setup();
    listConversationsMock.mockResolvedValue({ items: [], total: 0, cross_tenant: false });
    renderPage();
    await waitFor(() => expect(listConversationsMock).toHaveBeenCalled());
    await user.click(screen.getByTestId("conversations-pending-only"));
    await waitFor(() =>
      expect(listConversationsMock).toHaveBeenCalledWith(
        expect.objectContaining({ hasPending: true }),
      ),
    );
  });

  it("renders the agent filter fed from the agents list", async () => {
    listConversationsMock.mockResolvedValue({ items: [], total: 0, cross_tenant: false });
    renderPage();
    await waitFor(() => expect(listConversationsMock).toHaveBeenCalled());
    // Antd Select renders options in a portal jsdom can't click through —
    // assert the control exists and the options source was fetched.
    expect(screen.getByTestId("conversations-agent-filter")).toBeInTheDocument();
    expect(agentsSdk.listAgents).toHaveBeenCalled();
  });

  it("requests page 1 server-side and pages via offset", async () => {
    const user = userEvent.setup();
    listConversationsMock.mockResolvedValue({
      items: [sampleRow],
      total: 120,
      cross_tenant: false,
    });
    renderPage();
    await waitFor(() =>
      expect(listConversationsMock).toHaveBeenLastCalledWith(
        expect.objectContaining({ limit: 50, offset: 0 }),
      ),
    );
    // total=120 → 3 pages; clicking page 2 re-fetches with offset 50.
    await user.click(await screen.findByTitle("2"));
    await waitFor(() =>
      expect(listConversationsMock).toHaveBeenLastCalledWith(
        expect.objectContaining({ limit: 50, offset: 50 }),
      ),
    );
  });

  it("changing a filter resets pagination to page 1", async () => {
    const user = userEvent.setup();
    listConversationsMock.mockResolvedValue({
      items: [sampleRow],
      total: 120,
      cross_tenant: false,
    });
    renderPage();
    await user.click(await screen.findByTitle("2"));
    await waitFor(() =>
      expect(listConversationsMock).toHaveBeenLastCalledWith(
        expect.objectContaining({ offset: 50 }),
      ),
    );
    await user.click(screen.getByTestId("conversations-errors-only"));
    await waitFor(() =>
      expect(listConversationsMock).toHaveBeenLastCalledWith(
        expect.objectContaining({ hasError: true, offset: 0 }),
      ),
    );
  });

  it("renders the time-window filter and omits since by default", async () => {
    listConversationsMock.mockResolvedValue({ items: [], total: 0, cross_tenant: false });
    renderPage();
    await waitFor(() => expect(listConversationsMock).toHaveBeenCalled());
    // Antd Select portal — assert the control + the default request shape;
    // window → since wiring is a pure Date computation in refresh().
    expect(screen.getByTestId("conversations-window-filter")).toBeInTheDocument();
    expect(listConversationsMock).toHaveBeenLastCalledWith(
      expect.objectContaining({ since: undefined }),
    );
  });

  it("hydrates every filter from a deep-linked URL", async () => {
    listConversationsMock.mockResolvedValue({ items: [], total: 0, cross_tenant: false });
    renderPage("/conversations?errors=1&pending=1&agent=support-bot&status=active&window=24&page=3");
    await waitFor(() =>
      expect(listConversationsMock).toHaveBeenLastCalledWith(
        expect.objectContaining({
          hasError: true,
          hasPending: true,
          agentName: "support-bot",
          status: "active",
          offset: 100,
          since: expect.any(String),
        }),
      ),
    );
  });

  it("auto-refresh polls silently every 30s while enabled", async () => {
    listConversationsMock.mockResolvedValue({ items: [], total: 0, cross_tenant: false });
    renderPage();
    // First load on real timers; only the interval runs on fake ones.
    await waitFor(() => expect(listConversationsMock).toHaveBeenCalled());
    vi.useFakeTimers();
    try {
      fireEvent.click(screen.getByTestId("conversations-auto-refresh"));
      const before = listConversationsMock.mock.calls.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(31_000);
      });
      expect(listConversationsMock.mock.calls.length).toBeGreaterThan(before);
    } finally {
      vi.useRealTimers();
    }
  });
});
