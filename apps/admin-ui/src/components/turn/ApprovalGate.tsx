/**
 * ApprovalGate — the run-paused-on-approval card (「批准」/「拒绝」) plus the
 * frame-to-item adapter that builds it straight off a live SSE ``approval``
 * frame, without waiting for the terminal ``end`` frame + a poll.
 *
 * Lifted verbatim out of ``components/turn/TurnCard.tsx`` (调试台重设计 PR-B
 * Task 1) — TurnCard.tsx itself was retired in PR-B Task 5. No logic changed
 * by the move.
 */
import { Button, Space, Typography } from "antd";
import { AlertTriangle, Check, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { ApprovalItem } from "../../api/approvals";

const { Text } = Typography;

/** #5 — build an ``ApprovalItem`` from a backend ``approval`` SSE frame so the
 *  gate renders the instant the run pauses, without waiting for the terminal
 *  ``end`` frame + a ``/v1/approvals`` poll (which never fires when the client
 *  misses ``end``). The decide call only needs ``thread_id`` + ``run_id``; the
 *  rest feeds the gate card. Fields absent from the stream default safely. */
export function approvalItemFromEvent(data: unknown): ApprovalItem | null {
  if (data === null || typeof data !== "object") return null;
  const d = data as Record<string, unknown>;
  if (typeof d.run_id !== "string" || typeof d.thread_id !== "string")
    return null;
  const str = (v: unknown): string => (typeof v === "string" ? v : "");
  return {
    id: str(d.request_id) || d.run_id,
    tenant_id: str(d.tenant_id),
    user_id: null,
    run_id: d.run_id,
    thread_id: d.thread_id,
    request_id: str(d.request_id),
    node: str(d.node),
    reason_kind: str(d.reason_kind),
    action_summary: str(d.action_summary),
    proposed_args:
      d.proposed_args !== null && typeof d.proposed_args === "object"
        ? (d.proposed_args as Record<string, unknown>)
        : {},
    requested_at: str(d.requested_at),
    timeout_at: str(d.timeout_at),
    status: "pending",
    decided_by: null,
    decided_at: null,
  };
}

export function ApprovalGate({
  approval,
  busy,
  disabled = false,
  onDecide,
}: {
  approval: ApprovalItem;
  busy: boolean;
  /** Track C W2 — 切入态只读:审批决策是写操作,置灰两个按钮
   *  (照 ``FeedbackBar.disabled`` 的现有传法)。 */
  disabled?: boolean;
  onDecide: (decision: "approve" | "reject") => void;
}) {
  const { t } = useTranslation();
  return (
    <div
      data-testid="playground-approval"
      style={{
        border: "1px solid var(--ew-color-warning, #d4a017)",
        borderRadius: 6,
        padding: 10,
        marginTop: 8,
        background: "var(--ew-surface-raised)",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          marginBottom: 4,
        }}
      >
        <AlertTriangle size={14} strokeWidth={1.75} />
        <Text strong style={{ fontSize: 12 }}>
          {approval.node} — {t("playground.approval_awaiting")}
        </Text>
      </div>
      <Text style={{ fontSize: 12, display: "block", marginBottom: 6 }}>
        {approval.action_summary}
      </Text>
      <pre
        style={{
          margin: 0,
          fontSize: 11,
          fontFamily: "var(--ew-font-mono)",
          color: "var(--ew-text-secondary)",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          maxHeight: 160,
          overflow: "auto",
          marginBottom: 8,
        }}
      >
        {JSON.stringify(approval.proposed_args, null, 2)}
      </pre>
      <Space size={8}>
        <Button
          type="primary"
          size="small"
          icon={<Check size={13} strokeWidth={1.75} />}
          loading={busy}
          disabled={disabled}
          onClick={() => onDecide("approve")}
          data-testid="playground-approval-approve"
        >
          {t("playground.approval_approve")}
        </Button>
        <Button
          danger
          size="small"
          icon={<X size={13} strokeWidth={1.75} />}
          loading={busy}
          disabled={disabled}
          onClick={() => onDecide("reject")}
          data-testid="playground-approval-reject"
        >
          {t("playground.approval_reject")}
        </Button>
        <Text type="secondary" style={{ fontSize: 11 }}>
          {t("playground.approval_modify_hint")}
        </Text>
      </Space>
    </div>
  );
}
