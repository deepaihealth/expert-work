/**
 * ``approval`` SSE frame → ``ApprovalItem`` — the pure adapter behind
 * ``ApprovalGate`` (终审 M-8: extracted so logic-only modules can import it
 * without dragging antd/lucide/react-i18next into their dependency graph).
 */
import type { ApprovalItem } from "../../api/approvals";

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
