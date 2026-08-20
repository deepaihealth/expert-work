/**
 * ApprovalGate — split out of ``TurnCard.tsx`` (调试台重设计 PR-B Task 1).
 * Covers the approve/reject card render and the ``approvalItemFromEvent``
 * live-frame → ``ApprovalItem`` adapter (approval frame vs. anything else).
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import "../../../i18n";

import type { ApprovalItem } from "../../../api/approvals";
import { ApprovalGate, approvalItemFromEvent } from "../ApprovalGate";

const pendingApproval: ApprovalItem = {
  id: "a1",
  tenant_id: "t",
  user_id: null,
  run_id: "run-hist-1",
  thread_id: "th-1",
  request_id: "a1",
  node: "tools",
  reason_kind: "tool_policy",
  action_summary: "run exec_python",
  proposed_args: { code: "1+1" },
  requested_at: "2026-05-25T00:00:00Z",
  timeout_at: "2026-05-25T00:10:00Z",
  status: "pending",
  decided_by: null,
  decided_at: null,
};

describe("ApprovalGate", () => {
  it("renders the 批准/拒绝 buttons for a pending approval", () => {
    render(<ApprovalGate approval={pendingApproval} busy={false} onDecide={vi.fn()} />);

    expect(screen.getByTestId("playground-approval")).toBeInTheDocument();
    expect(screen.getByTestId("playground-approval-approve")).toBeInTheDocument();
    expect(screen.getByTestId("playground-approval-reject")).toBeInTheDocument();
  });
});

describe("approvalItemFromEvent", () => {
  it("builds an ApprovalItem from a live `approval` SSE frame's data", () => {
    const item = approvalItemFromEvent({
      run_id: "run-1",
      thread_id: "th-1",
      request_id: "req-1",
      tenant_id: "t1",
      node: "tools",
      reason_kind: "tool_policy",
      action_summary: "run exec_python",
      proposed_args: { code: "1+1" },
      requested_at: "2026-05-25T00:00:00Z",
      timeout_at: "2026-05-25T00:10:00Z",
    });

    expect(item).toEqual({
      id: "req-1",
      tenant_id: "t1",
      user_id: null,
      run_id: "run-1",
      thread_id: "th-1",
      request_id: "req-1",
      node: "tools",
      reason_kind: "tool_policy",
      action_summary: "run exec_python",
      proposed_args: { code: "1+1" },
      requested_at: "2026-05-25T00:00:00Z",
      timeout_at: "2026-05-25T00:10:00Z",
      status: "pending",
      decided_by: null,
      decided_at: null,
    });
  });

  it("returns null for a non-approval frame's data (missing run_id/thread_id)", () => {
    expect(approvalItemFromEvent({ node: "tools" })).toBeNull();
    expect(approvalItemFromEvent(null)).toBeNull();
    expect(approvalItemFromEvent("not an object")).toBeNull();
  });
});
