/**
 * SessionSidebar — the debug console's left-hand session list (PR-A Task 6).
 *
 * Migrated from ``SessionHistoryDrawer.test.tsx`` (browse / search / resume /
 * rename / archive / purge over the caller's threads for one agent), plus
 * the sidebar-specific behaviours the drawer never had: last_activity
 * ordering, current-thread highlight + running dot, disabling switching
 * mid-run, and the readOnly gate on every write control.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "antd";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import "../../../i18n";

import { SessionSidebar, type SessionSidebarProps } from "../SessionSidebar";
import * as sessionsSdk from "../../../api/sessions";
import type { ThreadMeta } from "../../../api/sessions";

// Stream N — the sidebar reads the ambient tenant scope; these tests don't
// mount a TenantScopeProvider, so mock it (mutable for passthrough asserts).
const { tenantScopeRef } = vi.hoisted(() => ({
  tenantScopeRef: { current: undefined as string | undefined },
}));
vi.mock("../../../tenant/TenantScopeContext", async (importOriginal) => {
  const { mockTenantScopeModule } = await import(
    "../../../test-utils/tenantScopeMock"
  );
  return mockTenantScopeModule(
    await importOriginal<typeof import("../../../tenant/TenantScopeContext")>(),
    tenantScopeRef,
  );
});

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

function tree(overrides: Partial<SessionSidebarProps> = {}) {
  return (
    <MemoryRouter>
      <App>
        <SessionSidebar
          agentName="demo-agent"
          currentThreadId={null}
          running={false}
          onNew={vi.fn()}
          onResume={vi.fn()}
          {...overrides}
        />
      </App>
    </MemoryRouter>
  );
}

function renderSidebar(overrides: Partial<SessionSidebarProps> = {}) {
  const onNew = vi.fn();
  const onResume = vi.fn();
  const props: Partial<SessionSidebarProps> = { onNew, onResume, ...overrides };
  const result = render(tree(props));
  return { ...result, onNew, onResume: props.onResume as ReturnType<typeof vi.fn> };
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

describe("SessionSidebar", () => {
  // --- migrated from SessionHistoryDrawer.test.tsx (9) ------------------

  it("lists the agent's sessions with their titles", async () => {
    renderSidebar();
    expect(await screen.findByText("Quarterly report")).toBeInTheDocument();
    expect(screen.getByText("今天天气")).toBeInTheDocument();
    expect(listMock).toHaveBeenCalledWith(
      expect.objectContaining({ agentName: "demo-agent" }),
    );
  });

  it("passes the ambient tenant scope through to the sessions list", async () => {
    tenantScopeRef.current = "99999999-9999-9999-9999-999999999999";
    renderSidebar();
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
    renderSidebar();
    expect(await screen.findByText(/cccccccc…/)).toBeInTheDocument();
  });

  it("debounces the search box into the server q param", async () => {
    const user = userEvent.setup();
    renderSidebar();
    await screen.findByText("Quarterly report");
    await user.type(screen.getByTestId("console-session-search"), "report");
    await waitFor(() =>
      expect(listMock).toHaveBeenCalledWith(
        expect.objectContaining({ q: "report" }),
      ),
    );
  });

  it("renders the status filter", async () => {
    renderSidebar();
    expect(
      await screen.findByTestId("console-session-filter"),
    ).toBeInTheDocument();
  });

  it("resumes the picked thread", async () => {
    const user = userEvent.setup();
    const { onResume } = renderSidebar();
    await user.click(
      await screen.findByTestId(`console-session-item-${A.thread_id}`),
    );
    expect(onResume).toHaveBeenCalledWith(A);
  });

  it("renames a session", async () => {
    const user = userEvent.setup();
    const onChanged = vi.fn();
    renderSidebar({ onChanged });
    await user.click(
      await screen.findByTestId(`console-session-rename-${A.thread_id}`),
    );
    const input = await screen.findByTestId("console-session-rename-input");
    await user.clear(input);
    await user.type(input, "New name");
    await user.click(screen.getByRole("button", { name: /save|保存/i }));
    await waitFor(() =>
      expect(renameMock).toHaveBeenCalledWith(A.thread_id, "New name"),
    );
    expect(onChanged).toHaveBeenCalledWith({
      kind: "rename",
      threadId: A.thread_id,
      title: "New name",
    });
  });

  it("archives a session after confirmation", async () => {
    const user = userEvent.setup();
    const onChanged = vi.fn();
    renderSidebar({ onChanged });
    await user.click(
      await screen.findByTestId(`console-session-archive-${A.thread_id}`),
    );
    const popup = await screen.findByRole("tooltip");
    await user.click(
      within(popup).getByRole("button", { name: /archive|归档/i }),
    );
    await waitFor(() => expect(archiveMock).toHaveBeenCalledWith(A.thread_id));
    expect(onChanged).toHaveBeenCalledWith({
      kind: "archive",
      threadId: A.thread_id,
    });
  });

  it("purges a session after the second confirmation", async () => {
    const user = userEvent.setup();
    const onChanged = vi.fn();
    renderSidebar({ onChanged });
    await user.click(
      await screen.findByTestId(`console-session-purge-${A.thread_id}`),
    );
    const popup = await screen.findByRole("tooltip");
    await user.click(
      within(popup).getByRole("button", { name: /delete forever|彻底删除/i }),
    );
    await waitFor(() => expect(purgeMock).toHaveBeenCalledWith(A.thread_id));
    expect(onChanged).toHaveBeenCalledWith({
      kind: "purge",
      threadId: A.thread_id,
    });
  });

  // --- new for the sidebar (6) --------------------------------------------

  it("asks the server for last_activity order and the agent's own sessions", async () => {
    renderSidebar();
    await waitFor(() => expect(listMock).toHaveBeenCalled());
    expect(listMock.mock.calls[0][0]).toMatchObject({
      agentName: "demo-agent",
      orderBy: "last_activity",
      limit: 50,
    });
  });

  it("highlights the current thread and shows the running dot only when running", async () => {
    const { rerender } = renderSidebar({
      currentThreadId: A.thread_id,
      running: false,
    });
    await screen.findByTestId(`console-session-item-${A.thread_id}`);
    expect(screen.queryByTestId("console-session-running-dot")).toBeNull();
    rerender(tree({ currentThreadId: A.thread_id, running: true }));
    expect(
      within(
        screen.getByTestId(`console-session-item-${A.thread_id}`),
      ).getByTestId("console-session-running-dot"),
    ).toBeInTheDocument();
  });

  it("disables switching sessions while a run is in flight", async () => {
    const onResume = vi.fn();
    renderSidebar({ running: true, onResume });
    await userEvent.click(
      await screen.findByTestId(`console-session-item-${B.thread_id}`),
    );
    expect(onResume).not.toHaveBeenCalled();
  });

  it("loads more with the next offset", async () => {
    listMock.mockResolvedValueOnce(
      Array.from({ length: 50 }, (_, i) => ({ ...A, thread_id: `t-${i}` })),
    );
    renderSidebar();
    await userEvent.click(await screen.findByTestId("console-session-load-more"));
    expect(listMock.mock.calls[1][0]).toMatchObject({ offset: 50 });
  });

  it("archived filter passes status=archived", async () => {
    renderSidebar();
    // Dual-language, like the rename/archive/purge assertions above — the
    // jsdom test environment's navigator.languages resolves this suite to
    // "en" by default (no explicit locale override in test/setup.ts), so a
    // Chinese-only matcher would deterministically miss.
    await userEvent.click(await screen.findByText(/已归档|Archived/));
    await waitFor(() =>
      expect(listMock.mock.calls.at(-1)?.[0]).toMatchObject({
        status: "archived",
      }),
    );
  });

  it("read-only: new/rename/archive/purge disabled, list still clickable", async () => {
    const onResume = vi.fn();
    renderSidebar({ readOnly: true, onResume });
    expect(screen.getByTestId("playground-new-session")).toBeDisabled();
    await userEvent.click(
      await screen.findByTestId(`console-session-item-${A.thread_id}`),
    );
    expect(onResume).toHaveBeenCalledWith(A);
  });

  it("read-only: rename/archive/purge are disabled and inert (no popup, no mutation)", async () => {
    renderSidebar({ readOnly: true });
    await screen.findByTestId(`console-session-item-${A.thread_id}`);

    const renameBtn = screen.getByTestId(`console-session-rename-${A.thread_id}`);
    const archiveBtn = screen.getByTestId(`console-session-archive-${A.thread_id}`);
    const purgeBtn = screen.getByTestId(`console-session-purge-${A.thread_id}`);
    expect(renameBtn).toBeDisabled();
    expect(archiveBtn).toBeDisabled();
    expect(purgeBtn).toBeDisabled();

    // ``userEvent.click`` correctly *throws* here — the ReadonlyTooltip
    // wrapper sets ``pointer-events: none`` on an ancestor, and user-event's
    // realistic pointer simulation refuses to click through that (same as a
    // real mouse in a real browser; see ../../turn/__tests__/TurnCard.test.tsx,
    // which only ever *hovers* this wrapper, never clicks the control inside
    // it). ``fireEvent.click`` bypasses that hit-testing simulation and
    // dispatches the click straight at the button/Popconfirm's own React
    // handlers — proving the *application-level* gate (Button's native
    // ``disabled`` + Popconfirm's own ``disabled`` prop) independently holds,
    // not just the outer pointer-events cosmetic layer.
    fireEvent.click(archiveBtn);
    expect(screen.queryByRole("tooltip")).toBeNull();
    expect(archiveMock).not.toHaveBeenCalled();

    fireEvent.click(purgeBtn);
    expect(screen.queryByRole("tooltip")).toBeNull();
    expect(purgeMock).not.toHaveBeenCalled();

    fireEvent.click(renameBtn);
    expect(screen.queryByTestId("console-session-rename-input")).toBeNull();
    expect(renameMock).not.toHaveBeenCalled();
  });

  it("keeps the row actions out of layout flow (hover-reveal container, not an antd actions sibling)", async () => {
    renderSidebar();
    const renameBtn = await screen.findByTestId(
      `console-session-rename-${A.thread_id}`,
    );
    expect(renameBtn.closest(".ew-session-item__acts")).toBeInTheDocument();
    const item = screen.getByTestId(`console-session-item-${A.thread_id}`);
    expect(
      within(item).getByText("Quarterly report").closest(".ew-session-item__title"),
    ).toBeInTheDocument();
  });
});
