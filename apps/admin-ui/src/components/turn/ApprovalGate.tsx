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
// Re-export the pure frame→item adapter from its own module (终审 M-8) so
// logic-only consumers (``console_turns``) don't pull antd/lucide through
// this component file.
export { approvalItemFromEvent } from "./approval_item";

const { Text } = Typography;


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
