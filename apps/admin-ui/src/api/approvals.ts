/**
 * Approvals SDK — backed by ``/v1/approvals`` (Stream HX-7).
 *
 * Both endpoints follow the standard envelope. The queue list is
 * oldest-first (``requested_at ASC``); ``decideApprovals`` applies up
 * to 20 verdicts in one call, each item failing independently — the
 * backend shares the single-run resume kernel, so the per-item error
 * vocabulary matches (404 / 409 / 422).
 */
import { getJson, postJson, withTenantScope, type TenantScope } from "./client";

export type ApprovalStatus = "pending" | "approved" | "rejected" | "modified" | "timeout";

/** B-20 approval triage — safety sign-offs (policy_gate / risk_confirmation)
 *  vs agent clarification questions (missing_info / ambiguous_requirement /
 *  approach_choice). Wire value of the ``kind_class`` query filter. */
export type ApprovalKindClass = "safety" | "clarification";

export interface ApprovalItem {
  id: string;
  tenant_id: string;
  user_id: string | null;
  run_id: string;
  thread_id: string;
  request_id: string;
  node: string;
  reason_kind: string;
  action_summary: string;
  proposed_args: Record<string, unknown>;
  requested_at: string;
  timeout_at: string;
  status: ApprovalStatus;
  decided_by: string | null;
  decided_at: string | null;
}

export interface ApprovalList {
  items: ApprovalItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface ListApprovalsParams {
  tenantScope?: TenantScope;
  status?: ApprovalStatus;
  limit?: number;
  offset?: number;
  kindClass?: ApprovalKindClass;
}

/** GET /v1/approvals — the cross-run queue (default ``status=pending``). */
export async function listApprovals(params: ListApprovalsParams = {}): Promise<ApprovalList> {
  const { tenantScope, status, limit, offset, kindClass } = params;
  const query = withTenantScope({ status, limit, offset, kind_class: kindClass }, tenantScope);
  return getJson<ApprovalList>("/v1/approvals", { params: query });
}

/** One verdict in the batch. ``modified_args`` only with ``"modify"``;
 *  the queue page only issues approve / reject — modify lives on the
 *  RunDetail ApprovalCard (Mini-ADR HX-G5). */
export interface ApprovalDecision {
  thread_id: string;
  run_id: string;
  decision: "approve" | "reject" | "modify";
  modified_args?: Record<string, unknown>;
  reason?: string;
}

export interface DecisionResult {
  run_id: string;
  ok: boolean;
  continuation_run_id?: string;
  error?: string;
  status_code?: number;
}

export interface DecideBatchResult {
  results: DecisionResult[];
  succeeded: number;
}

/** POST /v1/approvals:decide — up to 20 verdicts, per-item failures. */
export async function decideApprovals(
  decisions: ApprovalDecision[],
): Promise<DecideBatchResult> {
  return postJson<DecideBatchResult>("/v1/approvals:decide", { decisions });
}
