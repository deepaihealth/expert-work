/**
 * Eval-runs SDK — backed by ``/v1/eval-runs`` (P1-S2.5-BE).
 *
 * Every endpoint in this module returns the **raw** payload (no
 * ``{ success, data, error }`` envelope) — consistent with the
 * control-plane ``api/eval_runs.py`` (which mirrors runs / curation).
 * So these calls go through ``apiClient`` directly and read
 * ``response.data``, NOT through ``getJson`` (which unwraps an envelope).
 *
 * Scope (cross-tenant W3): the read endpoints accept an optional
 * ``tenantScope`` (``?tenant_id=``) so a switched-in system_admin reads the
 * target tenant's runs; ``undefined`` falls through to the home tenant.
 */
import { apiClient, withTenantScope, type TenantScope } from "./client";

export type EvalRunStatus = "queued" | "running" | "passed" | "failed" | "error";

/** ``summary`` is a free-form dict server-side; the worker writes
 *  ``{ pass_count, total }``. Kept loose so a summary-shape change
 *  doesn't break the client. */
export interface EvalRunRecord {
  id: string;
  /** Cross-tenant W4 — owning tenant in the ``tenant_id=*`` aggregate;
   *  absent on pre-W4 backends. */
  tenant_id?: string | null;
  suite: string;
  status: EvalRunStatus;
  triggered_by: string;
  summary: Record<string, unknown> | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface EvalRunList {
  items: EvalRunRecord[];
  total: number;
}

export interface EvalCaseResult {
  id: number;
  capability: string;
  case_id: string;
  passed: boolean;
  session_id: string | null;
  scores: Record<string, number> | null;
  /** 11.3 session-level metrics — populated by S2.2; ``null`` until then. */
  session_metrics: Record<string, unknown> | null;
}

export interface EvalRunCases {
  cases: EvalCaseResult[];
}

export interface ListEvalRunsParams {
  tenantScope?: TenantScope;
  status?: EvalRunStatus;
  limit?: number;
  offset?: number;
}

/** GET /v1/eval-runs — a page of runs (newest first). */
export async function listEvalRuns(params: ListEvalRunsParams = {}): Promise<EvalRunList> {
  const { tenantScope, status, limit, offset } = params;
  const response = await apiClient.get<EvalRunList>("/v1/eval-runs", {
    params: withTenantScope({ status, limit, offset }, tenantScope),
  });
  return response.data;
}

/** GET /v1/eval-runs/{id} — one run's status + summary. */
export async function getEvalRun(
  runId: string,
  tenantScope?: TenantScope,
): Promise<EvalRunRecord> {
  const response = await apiClient.get<EvalRunRecord>(
    `/v1/eval-runs/${encodeURIComponent(runId)}`,
    { params: withTenantScope({}, tenantScope) },
  );
  return response.data;
}

/** GET /v1/eval-runs/{id}/cases — per-case results for a run. */
export async function getEvalRunCases(
  runId: string,
  tenantScope?: TenantScope,
): Promise<EvalRunCases> {
  const response = await apiClient.get<EvalRunCases>(
    `/v1/eval-runs/${encodeURIComponent(runId)}/cases`,
    { params: withTenantScope({}, tenantScope) },
  );
  return response.data;
}

/** POST /v1/eval-runs — enqueue a suite (202 + the queued run). */
export async function enqueueEvalRun(suite: string): Promise<EvalRunRecord> {
  const response = await apiClient.post<EvalRunRecord>("/v1/eval-runs", { suite });
  return response.data;
}
