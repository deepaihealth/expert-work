/**
 * Skill-evolution governance SDK — backed by ``/v1/skill-evolution/*`` (SE-8).
 *
 * The backend returns **raw** ``JSONResponse`` payloads (no ``{success, data,
 * error}`` envelope — matching ``/v1/skills`` / ``/v1/curation``), so we go
 * through ``apiClient`` directly and read ``response.data`` verbatim (NOT
 * ``getJson``, which would try to unwrap an envelope). See
 * ``api/curation.ts`` for the same contract note.
 */
import { apiClient, withTenantScope, type TenantScope } from "./client";
import type { SkillRecord, SkillVersion } from "./skills";

export type PromoteRequestStatus =
  | "pending"
  | "approved"
  | "rejected"
  | "superseded";

export interface PromoteRequest {
  id: string;
  tenant_id: string;
  skill_id: string;
  skill_version: number;
  status: PromoteRequestStatus;
  requested_by_user_id: string | null;
  requested_by_agent_name: string | null;
  reason: string;
  decided_by_user_id: string | null;
  decided_at: string | null;
  decision_reason: string;
  created_at: string;
}

export interface PromoteRequestList {
  items: PromoteRequest[];
  next_cursor: string | null;
  cross_tenant: boolean;
}

export interface ListPromoteRequestsParams {
  tenantScope?: TenantScope;
  status?: PromoteRequestStatus;
  cursor?: string | null;
  limit?: number;
}

export async function listPromoteRequests(
  params: ListPromoteRequestsParams = {},
): Promise<PromoteRequestList> {
  const { tenantScope, status, cursor, limit } = params;
  const query = withTenantScope(
    { status, cursor: cursor ?? undefined, limit },
    tenantScope,
  );
  const response = await apiClient.get<PromoteRequestList>(
    "/v1/skill-evolution/promote-requests",
    { params: query },
  );
  return response.data;
}

export async function requestPromote(
  skillId: string,
  body: { skill_version: number; reason?: string },
): Promise<PromoteRequest> {
  const response = await apiClient.post<PromoteRequest>(
    `/v1/skill-evolution/skills/${encodeURIComponent(skillId)}/promote-requests`,
    body,
  );
  return response.data;
}

export async function approvePromote(
  requestId: string,
  body: { decision_reason?: string } = {},
): Promise<PromoteRequest> {
  const response = await apiClient.post<PromoteRequest>(
    `/v1/skill-evolution/promote-requests/${encodeURIComponent(requestId)}/approve`,
    body,
  );
  return response.data;
}

export async function rejectPromote(
  requestId: string,
  body: { decision_reason?: string } = {},
): Promise<PromoteRequest> {
  const response = await apiClient.post<PromoteRequest>(
    `/v1/skill-evolution/promote-requests/${encodeURIComponent(requestId)}/reject`,
    body,
  );
  return response.data;
}

// ── eval evidence (SE-8-5) ──────────────────────────────────────────────────

export type EvalVerdict = "pass" | "fail" | "inconclusive";

export interface SkillEvalResult {
  id: string;
  tenant_id: string | null;
  skill_id: string;
  skill_version: number;
  baseline_score: number;
  skill_score: number;
  delta: number;
  n_cases: number;
  replay_source: "trajectory" | "eval_dataset";
  verdict: EvalVerdict;
  high_risk: boolean;
  evolution_round: number;
  created_at: string;
}

export async function listEvalResults(
  skillId: string,
  tenantScope?: TenantScope,
): Promise<SkillEvalResult[]> {
  const response = await apiClient.get<{ items: SkillEvalResult[] }>(
    `/v1/skill-evolution/skills/${encodeURIComponent(skillId)}/eval-results`,
    { params: withTenantScope({}, tenantScope) },
  );
  return response.data.items ?? [];
}

// ── lineage (SE-8-5) ────────────────────────────────────────────────────────

export interface SkillLineage {
  skill: SkillRecord;
  forked_from_source: SkillRecord | null;
  versions: SkillVersion[];
}

export async function getLineage(
  skillId: string,
  tenantScope?: TenantScope,
): Promise<SkillLineage> {
  const response = await apiClient.get<SkillLineage>(
    `/v1/skill-evolution/skills/${encodeURIComponent(skillId)}/lineage`,
    { params: withTenantScope({}, tenantScope) },
  );
  return response.data;
}

// ── kill-switch (SE-8-5) ────────────────────────────────────────────────────

export type KillSwitchScope = "global" | "tenant";

export interface KillSwitch {
  id: string;
  scope: KillSwitchScope;
  tenant_id: string | null;
  engaged: boolean;
  reason: string;
  engaged_by_user_id: string | null;
  engaged_at: string | null;
  released_by_user_id: string | null;
  released_at: string | null;
  updated_at: string;
}

export interface KillSwitchState {
  global: KillSwitch | null;
  tenant: KillSwitch | null;
  effective_halted: boolean;
}

export async function getKillSwitch(tenantScope?: TenantScope): Promise<KillSwitchState> {
  const response = await apiClient.get<KillSwitchState>("/v1/skill-evolution/kill-switch", {
    params: withTenantScope({}, tenantScope),
  });
  return response.data;
}

export async function engageKillSwitch(
  body: { scope: KillSwitchScope; reason?: string },
): Promise<KillSwitch> {
  const response = await apiClient.post<KillSwitch>(
    "/v1/skill-evolution/kill-switch/engage",
    body,
  );
  return response.data;
}

export async function releaseKillSwitch(
  body: { scope: KillSwitchScope },
): Promise<KillSwitch> {
  const response = await apiClient.post<KillSwitch>(
    "/v1/skill-evolution/kill-switch/release",
    body,
  );
  return response.data;
}
