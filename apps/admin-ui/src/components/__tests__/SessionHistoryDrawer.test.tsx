/**
 * SessionHistoryDrawer tests — browse / search / resume / rename / archive /
 * purge over the caller's threads for one agent.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "antd";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../i18n";

import { SessionHistoryDrawer } from "../SessionHistoryDrawer";
import * as sessionsSdk from "../../api/sessions";
import type { ThreadMeta } from "../../api/sessions";

// Stream N — the drawer reads the ambient tenant scope; these tests don't
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

const listMock = vi.spyOn(sessionsSdk, "listSessions");
const renameMock = vi.spyOn(sessionsSdk, "renameSession");
const archiveMock = vi.spyOn(sessionsSdk, "archiveSession");
const purgeMock = vi.spyOn(sessionsSdk, "purgeSession");

function meta(overrides: Partial<ThreadMeta>): ThreadMeta {
  return {
    thread_id: "aaaaaaaa-0000-0000-0000-000000000001",
    tenant_id: "22222222-2222-2222-2222-222222222222",
    agent_name: "demo-agent",
    agent_version: "1.0.0",
    user_id: null,
    status: "active",
    title: null,
    created_by: "u",
    created_at: "2026-05-25T00:00:00Z",
    updated_at: "2026-05-25T00:00:00Z",
    ...overrides,
  };
}

const A = meta({
  thread_id: "aaaaaaaa-0000-0000-0000-00000000000a",
  title: "Quarterly report",
});
const B = meta({
  thread_id: "bbbbbbbb-0000-0000-0000-00000000000b",
  title: "今天天气",
});

function renderDrawer(
  props: Partial<Parameters<typeof SessionHistoryDrawer>[0]> = {},
) {
  const onResume = vi.fn();
  const onClose = vi.fn();
  const onChanged = vi.fn();
  render(
    <App>
      <SessionHistoryDrawer
        open
        onClose={onClose}
        agentName="demo-agent"
        currentThreadId={null}
        onResume={onResume}
        onChanged={onChanged}
        {...props}
      />
    </App>,
  );
  return { onResume, onClose, onChanged };
}

beforeEach(() => {
  tenantScopeRef.current = undefined;
  listMock.mockReset();
  listMock.mockResolvedValue([A, B]);
  renameMock.mockReset().mockResolvedValue(A);
  archiveMock.mockReset().mockResolvedValue(undefined);
  purgeMock.mockReset().mockResolvedValue(undefined);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("SessionHistoryDrawer", () => {
  it("lists the agent's sessions with their titles", async () => {
    renderDrawer();
    expect(await screen.findByText("Quarterly report")).toBeInTheDocument();
    expect(screen.getByText("今天天气")).toBeInTheDocument();
    // Scoped to the agent, server-side.
    expect(listMock).toHaveBeenCalledWith(
      expect.objectContaining({ agentName: "demo-agent" }),
    );
  });

  it("passes the ambient tenant scope through to the sessions list", async () => {
    tenantScopeRef.current = "99999999-9999-9999-9999-999999999999";
    renderDrawer();
    await waitFor(() =>
      expect(listMock).toHaveBeenCalledWith(
        expect.objectContaining({
          tenantScope: "99999999-9999-9999-9999-999999999999",
        }),
      ),
    );
  });

  it("falls back to the thread_id prefix when a session has no title", async () => {
    listMock.mockResolvedValue([
      meta({ thread_id: "cccccccc-1111-2222-3333-444444444444" }),
    ]);
    renderDrawer();
    expect(await screen.findByText(/cccccccc…/)).toBeInTheDocument();
  });

  it("debounces the search box into the server q param", async () => {
    const user = userEvent.setup();
    renderDrawer();
    await screen.findByText("Quarterly report");
    await user.type(screen.getByTestId("session-history-search"), "report");
    await waitFor(() =>
      expect(listMock).toHaveBeenCalledWith(
        expect.objectContaining({ q: "report" }),
      ),
    );
  });

  it("renders the status filter (interaction covered by e2e — antd Select's virtual list does not render options under jsdom)", async () => {
    renderDrawer();
    expect(
      await screen.findByTestId("session-history-status-filter"),
    ).toBeInTheDocument();
  });

  it("resumes the picked thread and closes", async () => {
    const user = userEvent.setup();
    const { onResume, onClose } = renderDrawer();
    await user.click(
      await screen.findByTestId(`session-history-item-${A.thread_id}`),
    );
    expect(onResume).toHaveBeenCalledWith(A);
    expect(onClose).toHaveBeenCalled();
  });

  it("renames a session", async () => {
    const user = userEvent.setup();
    const { onChanged } = renderDrawer();
    await user.click(
      await screen.findByTestId(`session-history-rename-${A.thread_id}`),
    );
    const input = await screen.findByTestId("session-history-rename-input");
    await user.clear(input);
    await user.type(input, "New name");
    await user.click(screen.getByRole("button", { name: /save|保存/i }));
    await waitFor(() =>
      expect(renameMock).toHaveBeenCalledWith(A.thread_id, "New name"),
    );
    expect(onChanged).toHaveBeenCalled();
  });

  it("archives a session after confirmation", async () => {
    const user = userEvent.setup();
    const { onChanged } = renderDrawer();
    await user.click(
      await screen.findByTestId(`session-history-archive-${A.thread_id}`),
    );
    // Popconfirm — click the confirm (OK) button.
    const popup = await screen.findByRole("tooltip");
    await user.click(
      within(popup).getByRole("button", { name: /archive|归档/i }),
    );
    await waitFor(() => expect(archiveMock).toHaveBeenCalledWith(A.thread_id));
    expect(onChanged).toHaveBeenCalled();
  });

  it("purges a session after the second confirmation", async () => {
    const user = userEvent.setup();
    const { onChanged } = renderDrawer();
    await user.click(
      await screen.findByTestId(`session-history-purge-${A.thread_id}`),
    );
    const popup = await screen.findByRole("tooltip");
    await user.click(
      within(popup).getByRole("button", { name: /delete forever|彻底删除/i }),
    );
    await waitFor(() => expect(purgeMock).toHaveBeenCalledWith(A.thread_id));
    expect(onChanged).toHaveBeenCalled();
  });
});
