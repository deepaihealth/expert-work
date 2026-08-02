/**
 * MemoryPane tests — P5b-2c T4: source-run column + jump-to-run;
 * T5: as_of historical view.
 *
 * Focused unit test for the tenant-admin memory governance pane in
 * isolation (UserProfile.test.tsx already covers the pane end-to-end
 * through the Memory tab). ``useNavigate`` is mocked directly since the
 * pane needs no other router primitives.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "antd";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../i18n";

const mockNavigate = vi.fn();
vi.mock("react-router-dom", () => ({
  useNavigate: () => mockNavigate,
}));

import * as memorySdk from "../../api/memory";
import type { MemoryItem } from "../../api/memory";
import { MemoryPane } from "./MemoryPane";

// Cross-tenant W3 — the pane reads the ambient tenant scope; these tests
// don't mount a TenantScopeProvider, so mock it (switchable per test;
// undefined = home state).
const scopeRef = vi.hoisted(() => ({ current: undefined as string | undefined }));
vi.mock("../../tenant/TenantScopeContext", async (importOriginal) => {
  const { mockTenantScopeModule } = await import("../../test-utils/tenantScopeMock");
  return mockTenantScopeModule(
    await importOriginal<typeof import("../../tenant/TenantScopeContext")>(),
    scopeRef,
  );
});

// Cross-tenant W3 — 切入态判定 hook 会触 useAuth(这里不挂 AuthProvider),
// mock 成 home 态(两态断言在 UserProfile.test.tsx 的 Memory tab 用例)。
vi.mock("../../tenant/useIsTenantSwitched", () => ({
  useIsTenantSwitched: () => false,
}));

const USER_ID = "aaaaaaaa-0000-0000-0000-000000000001";

const WITH_SOURCE: MemoryItem = {
  id: "m-src",
  tenant_id: "t",
  user_id: USER_ID,
  kind: "fact",
  content: "Memory with a source run",
  created_at: "2026-06-01T10:00:00Z",
  importance: 0.9,
  confidence: 0.5,
  source_thread_id: "thread-123",
  source_run_id: "run-456",
};

const NO_SOURCE: MemoryItem = {
  id: "m-nosrc",
  tenant_id: "t",
  user_id: USER_ID,
  kind: "episodic",
  content: "Memory without a source run",
  created_at: "2026-06-02T10:00:00Z",
  importance: 0.2,
  confidence: 0.9,
  source_thread_id: null,
  source_run_id: null,
};

function renderPane() {
  return render(
    <App>
      <MemoryPane userId={USER_ID} />
    </App>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
  scopeRef.current = undefined;
});

describe("MemoryPane — cross-tenant W3 scope passthrough", () => {
  it("threads the switched tenant scope into listMemories", async () => {
    scopeRef.current = "22222222-2222-2222-2222-222222222222";
    const listSpy = vi.spyOn(memorySdk, "listMemories").mockResolvedValue({
      items: [],
      total: 0,
      cross_tenant: false,
    });
    renderPane();

    await waitFor(() =>
      expect(listSpy).toHaveBeenCalledWith(
        expect.objectContaining({ userId: USER_ID, tenantScope: scopeRef.current }),
      ),
    );
  });
});

describe("MemoryPane — source-run column", () => {
  it("navigates to the source run when both source ids are present", async () => {
    vi.spyOn(memorySdk, "listMemories").mockResolvedValue({
      items: [WITH_SOURCE],
      total: 1,
      cross_tenant: false,
    });
    const user = userEvent.setup();
    renderPane();

    const link = await screen.findByTestId(`memory-source-run-${WITH_SOURCE.id}`);
    await user.click(link);

    expect(mockNavigate).toHaveBeenCalledWith("/runs/thread-123/run-456");
  });

  it("renders no source link when source ids are null", async () => {
    vi.spyOn(memorySdk, "listMemories").mockResolvedValue({
      items: [NO_SOURCE],
      total: 1,
      cross_tenant: false,
    });
    renderPane();

    await screen.findByText(NO_SOURCE.content);
    expect(
      screen.queryByTestId(`memory-source-run-${NO_SOURCE.id}`),
    ).not.toBeInTheDocument();
  });
});

describe("MemoryPane — as_of historical view", () => {
  it("shows a time-enabled DatePicker for historical lookups", async () => {
    vi.spyOn(memorySdk, "listMemories").mockResolvedValue({
      items: [],
      total: 0,
      cross_tenant: false,
    });
    renderPane();

    expect(await screen.findByTestId("memory-as-of-picker")).toBeInTheDocument();
    expect(screen.queryByTestId("memory-as-of-banner")).not.toBeInTheDocument();
  });

  it("picking a time re-fetches with as_of, shows a historical banner, and disables edit/forget; clearing restores the live view", async () => {
    const listSpy = vi.spyOn(memorySdk, "listMemories").mockResolvedValue({
      items: [WITH_SOURCE],
      total: 1,
      cross_tenant: false,
    });
    const user = userEvent.setup();
    renderPane();

    await screen.findByTestId(`memory-edit-${WITH_SOURCE.id}`);
    expect(listSpy).toHaveBeenCalledTimes(1);
    expect(listSpy).toHaveBeenLastCalledWith({ userId: USER_ID, as_of: undefined });
    expect(screen.getByTestId(`memory-edit-${WITH_SOURCE.id}`)).not.toBeDisabled();
    expect(screen.getByTestId(`memory-forget-${WITH_SOURCE.id}`)).not.toBeDisabled();

    // AntD's DatePicker opens its panel on focus/click and portals it to
    // document.body; the single (non-range) picker's "Now" preset both
    // sets and confirms a value immediately (no separate OK click needed).
    const picker = screen.getByTestId("memory-as-of-picker");
    await user.click(picker);
    await user.click(await screen.findByText(/^Now$/i));

    await waitFor(() => expect(listSpy).toHaveBeenCalledTimes(2));
    const pickedParams = listSpy.mock.calls[1][0]!;
    expect(pickedParams.userId).toBe(USER_ID);
    expect(typeof pickedParams.as_of).toBe("string");
    const pickedAsOf = pickedParams.as_of as string;

    const banner = await screen.findByTestId("memory-as-of-banner");
    expect(banner).toHaveTextContent(new Date(pickedAsOf).toLocaleString());
    expect(screen.getByTestId(`memory-edit-${WITH_SOURCE.id}`)).toBeDisabled();
    expect(screen.getByTestId(`memory-forget-${WITH_SOURCE.id}`)).toBeDisabled();

    const clearBtn = picker.closest(".ant-picker")!.querySelector(".ant-picker-clear")!;
    fireEvent.click(clearBtn);

    await waitFor(() => expect(listSpy).toHaveBeenCalledTimes(3));
    expect(listSpy).toHaveBeenLastCalledWith({ userId: USER_ID, as_of: undefined });
    expect(screen.queryByTestId("memory-as-of-banner")).not.toBeInTheDocument();
    expect(screen.getByTestId(`memory-edit-${WITH_SOURCE.id}`)).not.toBeDisabled();
    expect(screen.getByTestId(`memory-forget-${WITH_SOURCE.id}`)).not.toBeDisabled();
  });
});
