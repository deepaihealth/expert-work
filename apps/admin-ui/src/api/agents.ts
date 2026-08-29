/**
 * Agents SDK — backed by control-plane ``/v1/agents``.
 *
 * Stream N: ``listAgents`` accepts a ``TenantScope`` so system_admin
 * callers can pass ``"*"`` for the cross-tenant aggregate; the
 * ``cross_tenant`` flag on the response tells the UI which mode it got.
 */
import { apiClient, getJson, postJson, putJson, withTenantScope, type TenantScope } from "./client";

export interface AgentRecord {
  id: string;
  tenant_id: string;
  name: string;
  version: string;
  status: string;
  spec_sha256: string;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface AgentList {
  items: AgentRecord[];
  total: number;
  cross_tenant: boolean;
}

export interface ListAgentsParams {
  tenantScope?: TenantScope;
  status?: string;
  name?: string;
  limit?: number;
  offset?: number;
}

export async function listAgents(params: ListAgentsParams = {}): Promise<AgentList> {
  const { tenantScope, status, name, limit, offset } = params;
  const query = withTenantScope(
    { status, name, limit, offset },
    tenantScope,
  );
  return getJson<AgentList>("/v1/agents", { params: query });
}

/** Stream RT-4 (RT-ADR-16) — agent-level kill-switch state. Present on the
 *  detail response only while the agent is disabled (reason / who / when for
 *  the status tooltip); ``null`` when the agent is enabled. */
export interface AgentDisableRecord {
  tenant_id: string;
  agent_name: string;
  disabled: boolean;
  reason: string | null;
  disabled_by: string | null;
  disabled_at: string | null;
  updated_at: string;
}

export interface AgentDetailResponse {
  record: AgentRecord & {
    /** The full manifest ({apiVersion, kind, metadata, spec}) — same
     *  document POST /v1/agents accepts. NOT the inner ``spec`` block: pass
     *  it to the form_model readers as-is (``readPromptJinja(record.spec)``),
     *  never wrapped in another ``{ spec }`` shell. */
    spec: Record<string, unknown>;
  };
  /** Stream RT-4 — whether the agent name is currently kill-switched. */
  disabled?: boolean;
  /** The kill-switch record when ``disabled`` is true; ``null`` otherwise. */
  disable?: AgentDisableRecord | null;
  /** 保存/创建时 dry-run 构建的结果:非 null = **存下来了,但这个部署还跑不了
   *  它**(平台没配这个 provider 的凭据、或声明了长期记忆却没配 embedding)。
   *  这类问题作者改不了,所以后端不拒绝保存 —— 但绿灯不等于能跑,必须显式
   *  告诉人。manifest 本身写错(技能解析不到、子 Agent 成环……)是另一条路:
   *  后端直接 422 ``MANIFEST_UNBUILDABLE``,根本不落库。
   *  只在 create / update 的响应里出现,GET 详情没有这个字段。 */
  build_warning?: string | null;
}

/** Result of POST /v1/agents/{name}/disable|enable. ``cancelled_runs`` is the
 *  count of in-flight runs the disable bulk-cancelled (absent on enable). */
export interface AgentDisableResult {
  name: string;
  disabled: boolean;
  cancelled_runs?: number;
}

/** ``tenantScope`` carries the concrete tenant id when a system_admin has
 *  switched into a foreign tenant (Track C W2 — read-only drill-in); it rides
 *  as ``?tenant_id=`` like :func:`listAgents`. ``undefined`` = home tenant. */
export async function getAgent(
  name: string,
  version: string,
  tenantScope?: TenantScope,
): Promise<AgentDetailResponse> {
  return getJson<AgentDetailResponse>(
    `/v1/agents/${encodeURIComponent(name)}/${encodeURIComponent(version)}`,
    { params: withTenantScope({}, tenantScope) },
  );
}

/** GET /v1/agents/{name}/{version}/tools — the agent's live tool contract
 *  (PR-A.3 §十.2 Schema tab): one entry per tool currently registered on
 *  this agent version, including MCP / skill-sourced and deferred (lazily
 *  promoted) tools. */
export interface AgentToolSchema {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  source: string;
  from_skill: string | null;
  deferred: boolean;
}

export interface AgentToolList {
  items: AgentToolSchema[];
  total: number;
}

export async function getAgentTools(
  name: string,
  version: string,
  tenantScope?: TenantScope,
): Promise<AgentToolList> {
  return getJson<AgentToolList>(
    `/v1/agents/${encodeURIComponent(name)}/${encodeURIComponent(version)}/tools`,
    { params: withTenantScope({}, tenantScope) },
  );
}

/** POST /v1/agents/{name}/{version}/delegation-policy:generate — 委派增强层 3。
 *  辅助 LLM 读该 Agent 的已保存 manifest 起草一段领域化「委派策略」。只返回
 *  草稿,不落库 — 采纳与否由前端把文本并进 prompt 编辑器,走既有保存流程。
 *  dynamic_workers 关闭的 Agent 400(DYNAMIC_WORKERS_DISABLED)。 */
export interface DelegationPolicyDraft {
  draft: string;
}

export async function generateDelegationPolicy(
  name: string,
  version: string,
): Promise<DelegationPolicyDraft> {
  return postJson<DelegationPolicyDraft>(
    `/v1/agents/${encodeURIComponent(name)}/${encodeURIComponent(version)}/delegation-policy:generate`,
    {},
  );
}

/** Server-side ``ManifestPayload`` accepts the manifest YAML text; ``{{ … }}``
 *  inside it is run-time Jinja, stored verbatim. The backend validates it
 *  end-to-end (Pydantic + ManifestError) on save. */
export interface ManifestPayload {
  manifest_yaml: string;
}

/** PUT /v1/agents/{name}/{version} — in-place spec update. The
 *  ``manifest_yaml`` metadata block MUST match the path's ``name`` and
 *  ``version`` or the server rejects with ``MANIFEST_PATH_MISMATCH``
 *  (422). */
export async function updateAgent(
  name: string,
  version: string,
  payload: ManifestPayload,
): Promise<AgentDetailResponse> {
  return putJson<AgentDetailResponse>(
    `/v1/agents/${encodeURIComponent(name)}/${encodeURIComponent(version)}`,
    payload,
  );
}

/** POST /v1/agents — create a new agent from raw YAML. The backend
 *  derives ``name + version`` from the manifest's ``metadata`` block.
 *  409 ``MANIFEST_DUPLICATE`` on collision; 422 with envelope code on
 *  Pydantic / template validation errors. */
export async function createAgent(
  payload: ManifestPayload,
): Promise<AgentDetailResponse> {
  return postJson<AgentDetailResponse>("/v1/agents", payload);
}

/** DELETE /v1/agents/{name}/{version} — soft delete: flips this exact
 *  version's status to ``DELETED`` (204, no body). Requires
 *  ``manifest:delete``. Scoped to one version — other versions of ``name``
 *  are untouched (unlike disable/enable, which cover the whole name) — and
 *  there is no undelete endpoint. */
export async function deleteAgent(name: string, version: string): Promise<void> {
  await apiClient.delete(
    `/v1/agents/${encodeURIComponent(name)}/${encodeURIComponent(version)}`,
  );
}

/** POST /v1/agents/{name}/disable — Stream RT-4 (RT-ADR-16). Engages the
 *  agent-level kill switch: rejects new runs/sessions across all versions of
 *  ``name`` and bulk-cancels its in-flight runs. Requires ``manifest:write``.
 *  Reversible via {@link enableAgent}. */
export async function disableAgent(
  name: string,
  reason?: string,
): Promise<AgentDisableResult> {
  return postJson<AgentDisableResult>(
    `/v1/agents/${encodeURIComponent(name)}/disable`,
    { reason: reason ?? null },
  );
}

/** POST /v1/agents/{name}/enable — releases the kill switch. New runs resume
 *  immediately; the runs the disable cancelled are not auto-restarted. */
export async function enableAgent(
  name: string,
  reason?: string,
): Promise<AgentDisableResult> {
  return postJson<AgentDisableResult>(
    `/v1/agents/${encodeURIComponent(name)}/enable`,
    { reason: reason ?? null },
  );
}

/** Stream HX-5 — one revision-history entry (summary; no spec payload).
 *  The diff view fetches the two full snapshots it compares. */
export interface RevisionSummary {
  revision: number;
  spec_sha256: string;
  actor_id: string;
  created_at: string;
}

export interface RevisionList {
  items: RevisionSummary[];
}

export interface RevisionDetail {
  record: {
    revision: number;
    spec_sha256: string;
    actor_id: string;
    created_at: string;
    /** Full manifest snapshot at this revision. */
    spec: Record<string, unknown>;
  };
}

export interface RollbackResult {
  record: AgentDetailResponse["record"];
  /** History row the rollback appended; null = current content already
   *  matched the target snapshot (recorded no-op). */
  revision: number | null;
  rolled_back_to: number;
}

export async function listRevisions(
  name: string,
  version: string,
  tenantScope?: TenantScope,
): Promise<RevisionList> {
  return getJson<RevisionList>(
    `/v1/agents/${encodeURIComponent(name)}/${encodeURIComponent(version)}/revisions`,
    { params: withTenantScope({}, tenantScope) },
  );
}

export async function getRevision(
  name: string,
  version: string,
  revision: number,
  tenantScope?: TenantScope,
): Promise<RevisionDetail> {
  return getJson<RevisionDetail>(
    `/v1/agents/${encodeURIComponent(name)}/${encodeURIComponent(version)}/revisions/${revision}`,
    { params: withTenantScope({}, tenantScope) },
  );
}

/** POST .../revisions/{n}/rollback — rolls *forward* to the old
 *  snapshot's content by appending a new revision (history is never
 *  rewritten). */
export async function rollbackToRevision(
  name: string,
  version: string,
  revision: number,
): Promise<RollbackResult> {
  return postJson<RollbackResult>(
    `/v1/agents/${encodeURIComponent(name)}/${encodeURIComponent(version)}/revisions/${revision}/rollback`,
    {},
  );
}
