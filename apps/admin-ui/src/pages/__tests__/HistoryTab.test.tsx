/**
 * HistoryTab tests — Stream HX-5 PR 2.
 *
 * The revisions SDK is stubbed; Monaco's DiffEditor is mocked to a pair
 * of textareas (the real component cannot mount in jsdom).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../i18n";

vi.mock("@monaco-editor/react", () => ({
  DiffEditor: ({ original, modified }: { original?: string; modified?: string }) => (
    <div data-testid="diff-stub">
      <pre data-testid="diff-original">{original}</pre>
      <pre data-testid="diff-modified">{modified}</pre>
    </div>
  ),
  default: () => null,
}));

import * as agentsSdk from "../../api/agents";
import { HistoryTab } from "../agent_detail/HistoryTab";
import type { AgentDetailResponse } from "../../api/agents";

// Track C W2 — 组件内直取 tenant scope + 切入态 hook;这些测试不挂
// Provider,默认 home 态;``tenantScopeRef`` 可变做 "*" 映射断言,
// ``isTenantSwitchedMock`` 可翻转做两态断言。
const { isTenantSwitchedMock, tenantScopeRef } = vi.hoisted(() => ({
  isTenantSwitchedMock: vi.fn(() => false),
  tenantScopeRef: { current: undefined as string | undefined },
}));
// Spread the real module so the tab keeps the real ``concreteTenantScope``
// ("*" → undefined) instead of a test-local copy that could drift.
vi.mock("../../tenant/TenantScopeContext", async (importOriginal) => {
  const { mockTenantScopeModule } = await import("../../test-utils/tenantScopeMock");
  return mockTenantScopeModule(
    await importOriginal<typeof import("../../tenant/TenantScopeContext")>(),
    tenantScopeRef,
  );
});
vi.mock("../../tenant/useIsTenantSwitched", () => ({
  useIsTenantSwitched: isTenantSwitchedMock,
}));

const SHA_V1 = "a".repeat(64);
const SHA_V2 = "b".repeat(64);

const detail: AgentDetailResponse = {
  record: {
    id: "11111111-1111-1111-1111-111111111111",
    tenant_id: "22222222-2222-2222-2222-222222222222",
    name: "demo-agent",
    version: "1.0.0",
    status: "active",
    spec_sha256: SHA_V2,
    created_by: "user-1",
    created_at: "2026-06-11T00:00:00Z",
    updated_at: "2026-06-11T00:00:00Z",
    spec: {},
  },
} as AgentDetailResponse;

const summaries = [
  { revision: 2, spec_sha256: SHA_V2, actor_id: "alice", created_at: "2026-06-11T01:00:00Z" },
  { revision: 1, spec_sha256: SHA_V1, actor_id: "bob", created_at: "2026-06-11T00:00:00Z" },
];

function snapshot(revision: number, sha: string, prompt: string) {
  return {
    record: {
      revision,
      spec_sha256: sha,
      actor_id: "x",
      created_at: "2026-06-11T00:00:00Z",
      spec: { spec: { system_prompt: { template: prompt } } },
    },
  };
}

afterEach(() => vi.restoreAllMocks());

// vitest 4 的 restore 不复位 vi.fn 的 mockReturnValue — 显式归位防串台。
beforeEach(() => {
  isTenantSwitchedMock.mockReturnValue(false);
  tenantScopeRef.current = undefined;
});

describe("HistoryTab", () => {
  it("renders the revision table, marks current, hides rollback on it", async () => {
    vi.spyOn(agentsSdk, "listRevisions").mockResolvedValue({ items: summaries });

    render(<HistoryTab detail={detail} onRolledBack={vi.fn()} />);

    await waitFor(() => expect(screen.getByText("#2")).toBeInTheDocument());
    expect(screen.getByText("#1")).toBeInTheDocument();
    expect(screen.getByText("alice")).toBeInTheDocument();
    // Revision 2 is current — no rollback button; revision 1 has one.
    expect(screen.queryByTestId("history-rollback-2")).toBeNull();
    expect(screen.getByTestId("history-rollback-1")).toBeInTheDocument();
  });

  it('maps the "*" aggregate scope to no tenant_id (backend 422s a literal "*")', async () => {
    tenantScopeRef.current = "*";
    const listMock = vi
      .spyOn(agentsSdk, "listRevisions")
      .mockResolvedValue({ items: summaries });

    render(<HistoryTab detail={detail} onRolledBack={vi.fn()} />);

    await waitFor(() => expect(listMock).toHaveBeenCalled());
    expect(listMock).toHaveBeenCalledWith("demo-agent", "1.0.0", undefined);
  });

  it("selecting two revisions loads and renders the diff older→newer", async () => {
    vi.spyOn(agentsSdk, "listRevisions").mockResolvedValue({ items: summaries });
    const getRevisionMock = vi
      .spyOn(agentsSdk, "getRevision")
      .mockImplementation(async (_n, _v, revision) =>
        revision === 1
          ? snapshot(1, SHA_V1, "old prompt")
          : snapshot(2, SHA_V2, "new prompt"),
      );

    render(<HistoryTab detail={detail} onRolledBack={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("#2")).toBeInTheDocument());

    const checkboxes = screen.getAllByRole("checkbox");
    await userEvent.click(checkboxes[0]);
    await userEvent.click(checkboxes[1]);

    await waitFor(() => expect(screen.getByTestId("diff-stub")).toBeInTheDocument());
    expect(getRevisionMock).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("diff-original").textContent).toContain("old prompt");
    expect(screen.getByTestId("diff-modified").textContent).toContain("new prompt");
  });

  it("rollback confirms, calls the SDK, and refreshes", async () => {
    const listMock = vi
      .spyOn(agentsSdk, "listRevisions")
      .mockResolvedValue({ items: summaries });
    const rollbackMock = vi.spyOn(agentsSdk, "rollbackToRevision").mockResolvedValue({
      record: detail.record,
      revision: 3,
      rolled_back_to: 1,
    });
    const onRolledBack = vi.fn();

    render(<HistoryTab detail={detail} onRolledBack={onRolledBack} />);
    await waitFor(() => expect(screen.getByTestId("history-rollback-1")).toBeInTheDocument());

    await userEvent.click(screen.getByTestId("history-rollback-1"));
    // Popconfirm OK button carries the rollback label.
    const confirm = await screen.findAllByRole("button", { name: /roll back|回滚/i });
    await userEvent.click(confirm[confirm.length - 1]);

    await waitFor(() => expect(rollbackMock).toHaveBeenCalledWith("demo-agent", "1.0.0", 1));
    await waitFor(() => expect(onRolledBack).toHaveBeenCalled());
    expect(listMock).toHaveBeenCalledTimes(2); // initial + post-rollback refresh
  });

  // Track C W2 — 切入态只读:回滚是写操作,置灰(归属态可用由上一条覆盖)。
  it("切入态置灰回滚按钮", async () => {
    isTenantSwitchedMock.mockReturnValue(true);
    vi.spyOn(agentsSdk, "listRevisions").mockResolvedValue({ items: summaries });

    render(<HistoryTab detail={detail} onRolledBack={vi.fn()} />);

    expect(await screen.findByTestId("history-rollback-1")).toBeDisabled();
  });

  it("surfaces SDK errors in an alert", async () => {
    vi.spyOn(agentsSdk, "listRevisions").mockRejectedValue(new Error("boom"));
    render(<HistoryTab detail={detail} onRolledBack={vi.fn()} />);
    await waitFor(() => expect(screen.getByTestId("history-tab-error")).toBeInTheDocument());
  });
});
