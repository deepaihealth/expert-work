/**
 * Curation + Eval SDK — backed by ``/v1/curation/*`` + ``/v1/eval-datasets``.
 *
 * Stream H.1b PR 3 added the list-only skeletons; H.4 PR 1 fills in all
 * mutations + a latent bug fix:
 *
 * **Latent bug fix (H.4 PR 1)**: the H.1b PR 3 SDK used ``getJson`` which
 * unwraps a ``{success, data, error}`` envelope, but the curation backend
 * returns *raw* ``JSONResponse(content={...})`` payloads — there is no
 * envelope middleware (envelopes are authored per-endpoint, see
 * ``api/agents.py``). The SDK tests passed only because they mocked
 * enveloped responses. We now match the actual backend contract by going
 * through ``apiClient`` directly, mirroring the ``getRun`` pattern in
 * ``runs.ts``.
 */
import { apiClient, withTenantScope, type TenantScope } from "./client";

export type CandidateStatus = "pending" | "promoted" | "dismissed";

export type CurationSignal =
  | "manual"
  | "negative_feedback"
  | "tool_failure"
  | "timeout"
  | "policy_block";

export type EvalDatasetSource = "golden" | "promoted_candidate";

export interface CurationCandidate {
  id: string;
  tenant_id: string;
  agent_name: string;
  agent_version: string;
  thread_id: string;
  user_id: string | null;
  trajectory_key: string;
  outcome: string;
  signal: string;
  feedback_rating: number | null;
  status: CandidateStatus;
  eval_dataset_id: string | null;
  detected_at: string;
  reviewed_at: string | null;
}

export interface CandidateTrajectory {
  /** ObjectStore-backed message list pulled at detail-fetch time. */
  messages: Record<string, unknown>[];
  step_count: number;
}

export interface CurationCandidateDetail extends CurationCandidate {
  trajectory: CandidateTrajectory | null;
}

export interface CurationCandidateList {
  items: CurationCandidate[];
  total: number;
  cross_tenant: boolean;
}

export interface ListCandidatesParams {
  tenantScope?: TenantScope;
  status?: CandidateStatus;
  signal?: CurationSignal;
  agentName?: string;
  limit?: number;
  offset?: number;
}

export async function listCandidates(
  params: ListCandidatesParams = {},
): Promise<CurationCandidateList> {
  const { tenantScope, status, signal, agentName, limit, offset } = params;
  const query = withTenantScope(
    { status, signal, agent_name: agentName, limit, offset },
    tenantScope,
  );
  const response = await apiClient.get<CurationCandidateList>(
    "/v1/curation/candidates",
    { params: query },
  );
  return response.data;
}

export async function getCandidate(
  candidateId: string,
  tenantScope?: TenantScope,
): Promise<CurationCandidateDetail> {
  const response = await apiClient.get<CurationCandidateDetail>(
    `/v1/curation/candidates/${encodeURIComponent(candidateId)}`,
    { params: withTenantScope({}, tenantScope) },
  );
  return response.data;
}

export interface PromoteCandidateBody {
  name: string;
  /** Optional override of the candidate's trajectory input. Empty
   *  ``{}`` is fine — backend defaults to empty dict. */
  input?: Record<string, unknown>;
  expected?: Record<string, unknown> | null;
  source: EvalDatasetSource;
}

export async function promoteCandidate(
  candidateId: string,
  body: PromoteCandidateBody,
): Promise<EvalDataset> {
  const response = await apiClient.post<EvalDataset>(
    `/v1/curation/candidates/${encodeURIComponent(candidateId)}/promote`,
    body,
  );
  return response.data;
}

export async function dismissCandidate(candidateId: string): Promise<void> {
  await apiClient.post<{ dismissed: true }>(
    `/v1/curation/candidates/${encodeURIComponent(candidateId)}/dismiss`,
  );
}

export interface EvalDataset {
  id: string;
  tenant_id: string;
  agent_name: string;
  name: string;
  input: Record<string, unknown>;
  expected: Record<string, unknown> | null;
  source: EvalDatasetSource;
  source_trajectory_key: string | null;
  source_user_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface EvalDatasetList {
  items: EvalDataset[];
  total: number;
  cross_tenant: boolean;
}

export interface ListEvalDatasetsParams {
  tenantScope?: TenantScope;
  agentName?: string;
  limit?: number;
  offset?: number;
}

export async function listEvalDatasets(
  params: ListEvalDatasetsParams = {},
): Promise<EvalDatasetList> {
  const { tenantScope, agentName, limit, offset } = params;
  const query = withTenantScope(
    { agent_name: agentName, limit, offset },
    tenantScope,
  );
  const response = await apiClient.get<EvalDatasetList>("/v1/eval-datasets", {
    params: query,
  });
  return response.data;
}

export interface CreateEvalDatasetBody {
  agent_name: string;
  name: string;
  input?: Record<string, unknown>;
  expected?: Record<string, unknown> | null;
  source?: EvalDatasetSource;
}

export async function createEvalDataset(
  body: CreateEvalDatasetBody,
): Promise<EvalDataset> {
  const response = await apiClient.post<EvalDataset>(
    "/v1/eval-datasets",
    body,
  );
  return response.data;
}

export async function getEvalDataset(
  datasetId: string,
  tenantScope?: TenantScope,
): Promise<EvalDataset> {
  const response = await apiClient.get<EvalDataset>(
    `/v1/eval-datasets/${encodeURIComponent(datasetId)}`,
    { params: withTenantScope({}, tenantScope) },
  );
  return response.data;
}

export interface PatchEvalDatasetBody {
  name?: string;
  input?: Record<string, unknown>;
  expected?: Record<string, unknown> | null;
}

export async function patchEvalDataset(
  datasetId: string,
  body: PatchEvalDatasetBody,
): Promise<EvalDataset> {
  const response = await apiClient.patch<EvalDataset>(
    `/v1/eval-datasets/${encodeURIComponent(datasetId)}`,
    body,
  );
  return response.data;
}

export async function deleteEvalDataset(datasetId: string): Promise<void> {
  await apiClient.delete<{ deleted: true }>(
    `/v1/eval-datasets/${encodeURIComponent(datasetId)}`,
  );
}
