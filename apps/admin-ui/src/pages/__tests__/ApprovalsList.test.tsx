/**
 * ApprovalsList tests — Stream HX-7 PR 3.
 *
 * The approvals SDK is stubbed; the page is rendered inside a
 * MemoryRouter (row action links) with the default home tenant scope.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import { AuthProvider } from "../../auth/AuthContext";
import { TenantScopeProvider } from "../../tenant/TenantScopeContext";
import "../../i18n";

import * as approvalsSdk from "../../api/approvals";
import { ApprovalsList } from "../ApprovalsList";
import type { ApprovalItem } from "../../api/approvals";

function item(overrides: Partial<ApprovalItem> = {}): ApprovalItem {
  const runId = overrides.run_id ?? crypto.randomUUID();
  return {
    id: crypto.randomUUID(),
    tenant_id: "22222222-2222-2222-2222-222222222222",
    user_id: null,
    run_id: runId,
    thread_id: "33333333-3333-3333-3333-333333333333",
    request_id: `approval:${runId}`,
    node: "tools",
    reason_kind: "policy_gate",
    action_summary: "approval-gated tool 'send_email'",
    proposed_args: { to: "ops@example.com" },
    requested_at: new Date(Date.now() - 5 * 60_000).toISOString(),
    timeout_at: new Date(Date.now() + 60 * 60_000).toISOString(),
    status: "pending",
    decided_by: null,
    decided_at: null,
    ...overrides,
  };
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/approvals"]}>
      <AuthProvider>
        <TenantScopeProvider>
          <ApprovalsList />
        </TenantScopeProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

afterEach(() => vi.restoreAllMocks());

describe("ApprovalsList", () => {
  it("renders the pending queue with run links and actions", async () => {
    const row = item();
    vi.spyOn(approvalsSdk, "listApprovals").mockResolvedValue({
      items: [row],
      total: 1,
      limit: 100,
      offset: 0,
    });

    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId(`approval-link-${row.run_id}`)).toBeInTheDocument(),
    );
    expect(screen.getByTestId(`approval-link-${row.run_id}`)).toHaveAttribute(
      "href",
      `/runs/${row.thread_id}/${row.run_id}`,
    );
    expect(screen.getByTestId(`approval-approve-${row.run_id}`)).toBeInTheDocument();
    expect(screen.getByTestId(`approval-reject-${row.run_id}`)).toBeInTheDocument();
  });

  it("inline reject confirms, calls :decide with one item, refreshes", async () => {
    const row = item();
    const listMock = vi.spyOn(approvalsSdk, "listApprovals").mockResolvedValue({
      items: [row],
      total: 1,
      limit: 100,
      offset: 0,
    });
    const decideMock = vi.spyOn(approvalsSdk, "decideApprovals").mockResolvedValue({
      results: [{ run_id: row.run_id, ok: true, continuation_run_id: crypto.randomUUID() }],
      succeeded: 1,
    });

    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId(`approval-reject-${row.run_id}`)).toBeInTheDocument(),
    );

    await userEvent.click(screen.getByTestId(`approval-reject-${row.run_id}`));
    const confirm = await screen.findAllByRole("button", { name: /reject|拒绝/i });
    await userEvent.click(confirm[confirm.length - 1]);

    await waitFor(() =>
      expect(decideMock).toHaveBeenCalledWith([
        { thread_id: row.thread_id, run_id: row.run_id, decision: "reject" },
      ]),
    );
    // initial + post-decide refresh — each refresh also fires one badge
    // probe for the inactive tab (limit: 1), so count main-list calls only.
    await waitFor(() =>
      expect(
        listMock.mock.calls.filter(([p]) => p?.limit === undefined),
      ).toHaveLength(2),
    );
  });

  it("multi-select batch approve sends every selected row", async () => {
    const a = item({ run_id: "11111111-aaaa-aaaa-aaaa-111111111111" });
    const b = item({ run_id: "22222222-bbbb-bbbb-bbbb-222222222222" });
    vi.spyOn(approvalsSdk, "listApprovals").mockResolvedValue({
      items: [a, b],
      total: 2,
      limit: 100,
      offset: 0,
    });
    const decideMock = vi.spyOn(approvalsSdk, "decideApprovals").mockResolvedValue({
      results: [
        { run_id: a.run_id, ok: true },
        { run_id: b.run_id, ok: false, error: "approval already decided", status_code: 409 },
      ],
      succeeded: 1,
    });

    renderPage();
    await waitFor(() => expect(screen.getByTestId("approvals-table")).toBeInTheDocument());

    const checkboxes = screen.getAllByRole("checkbox");
    await userEvent.click(checkboxes[0]); // header select-all
    await waitFor(() => expect(screen.getByTestId("approvals-batch-bar")).toBeInTheDocument());

    await userEvent.click(screen.getByTestId("approvals-batch-approve"));
    const confirm = await screen.findAllByRole("button", { name: /approve|批准/i });
    await userEvent.click(confirm[confirm.length - 1]);

    await waitFor(() => expect(decideMock).toHaveBeenCalledTimes(1));
    const sent = decideMock.mock.calls[0][0];
    expect(sent).toHaveLength(2);
    expect(sent.every((d) => d.decision === "approve")).toBe(true);
  });

  it("surfaces SDK errors in an alert", async () => {
    vi.spyOn(approvalsSdk, "listApprovals").mockRejectedValue(new Error("boom"));
    renderPage();
    await waitFor(() => expect(screen.getByTestId("approvals-error")).toBeInTheDocument());
  });

  it("approve-all decides every pending row on the page in one batch", async () => {
    const a = item({ run_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" });
    const b = item({ run_id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" });
    vi.spyOn(approvalsSdk, "listApprovals").mockResolvedValue({
      items: [a, b],
      total: 2,
      limit: 100,
      offset: 0,
    });
    const decideMock = vi.spyOn(approvalsSdk, "decideApprovals").mockResolvedValue({
      results: [
        { run_id: a.run_id, ok: true },
        { run_id: b.run_id, ok: true },
      ],
      succeeded: 2,
    });

    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId("approvals-approve-all")).toBeInTheDocument(),
    );

    await userEvent.click(screen.getByTestId("approvals-approve-all"));
    const confirm = await screen.findAllByRole("button", { name: /approve|批准/i });
    await userEvent.click(confirm[confirm.length - 1]);

    await waitFor(() => expect(decideMock).toHaveBeenCalledTimes(1));
    const sent = decideMock.mock.calls[0][0];
    expect(sent).toHaveLength(2);
    expect(sent.every((d) => d.decision === "approve")).toBe(true);
  });

  it("flags an approval past its timeout as timing out", async () => {
    const overdue = item({
      run_id: "cccccccc-cccc-cccc-cccc-cccccccccccc",
      timeout_at: new Date(Date.now() - 60_000).toISOString(),
    });
    vi.spyOn(approvalsSdk, "listApprovals").mockResolvedValue({
      items: [overdue],
      total: 1,
      limit: 100,
      offset: 0,
    });

    renderPage();
    await waitFor(() =>
      expect(screen.getByText(/timing out|即将超时/i)).toBeInTheDocument(),
    );
  });
});

// ---------------------------------------------------------------------------
// B-20 approval triage — safety / clarification tabs
// ---------------------------------------------------------------------------

describe("ApprovalsList kind tabs", () => {
  it("defaults to the safety tab: main list kindClass=safety, badge probe clarification limit=1", async () => {
    const listMock = vi.spyOn(approvalsSdk, "listApprovals").mockResolvedValue({
      items: [],
      total: 0,
      limit: 100,
      offset: 0,
    });
    renderPage();
    await waitFor(() =>
      expect(listMock).toHaveBeenCalledWith(
        expect.objectContaining({ kindClass: "safety", status: "pending" }),
      ),
    );
    expect(listMock).toHaveBeenCalledWith(
      expect.objectContaining({ kindClass: "clarification", limit: 1 }),
    );
  });

  it("switching to the clarification tab refetches with kindClass=clarification", async () => {
    const listMock = vi.spyOn(approvalsSdk, "listApprovals").mockResolvedValue({
      items: [],
      total: 0,
      limit: 100,
      offset: 0,
    });
    renderPage();
    await waitFor(() => expect(listMock).toHaveBeenCalled());
    listMock.mockClear();

    await userEvent.click(screen.getByTestId("approvals-tab-clarification"));
    await waitFor(() =>
      expect(
        listMock.mock.calls.some(
          ([params]) => params?.kindClass === "clarification" && params?.limit === undefined,
        ),
      ).toBe(true),
    );
    // The badge probe now targets the inactive safety tab.
    expect(
      listMock.mock.calls.some(
        ([params]) => params?.kindClass === "safety" && params?.limit === 1,
      ),
    ).toBe(true);
  });

  it("shows the inactive tab's pending count as a badge", async () => {
    vi.spyOn(approvalsSdk, "listApprovals").mockImplementation(async (params) =>
      params?.limit === 1
        ? { items: [item()], total: 7, limit: 1, offset: 0 }
        : { items: [], total: 0, limit: 100, offset: 0 },
    );
    renderPage();
    const clarTab = await screen.findByTestId("approvals-tab-clarification");
    await waitFor(() => expect(clarTab.textContent).toContain("7"));
  });
});
