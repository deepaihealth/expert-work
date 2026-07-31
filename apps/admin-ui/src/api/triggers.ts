/**
 * Triggers SDK — backed by ``/v1/triggers`` (Stream J.10).
 *
 * Stream H.1b PR 3 added list-only; Stream H.4 PR 6 fills in the full
 * CRUD surface (create / get / patch / delete) plus a latent bug fix:
 *
 * **Latent bug fix (H.4 PR 6)**: H.1b PR 3 used ``getJson`` which
 * unwraps a ``{success, data, error}`` envelope, but the triggers
 * backend returns raw ``JSONResponse(content={...})`` payloads — same
 * shape as curation / skills (H.4 PR 1 + PR 5 fixed those). The SDK
 * now goes through ``apiClient`` directly.
 *
 * Webhook secret is **show-once**: backend returns ``webhook_secret``
 * in the create response when ``kind=webhook``, and never again
 * (subsequent GETs / PATCHes carry no secret). Rotation = delete +
 * re-create (M0 — see § 6.6.5 § 6.6.10).
 */
import { apiClient, withTenantScope, type TenantScope } from "./client";

export type TriggerKind = "cron" | "webhook";

export interface TriggerRecord {
  id: string;
  tenant_id: string;
  user_id: string | null;
  agent_name: string;
  agent_version: string;
  name: string;
  kind: TriggerKind;
  config: Record<string, unknown>;
  enabled: boolean;
  source: string;
  created_at: string;
  updated_at: string;
}

/** Create response carries an extra one-time ``webhook_secret`` field
 *  when ``kind=webhook`` — show it once and never store it. */
export interface TriggerCreateResponse extends TriggerRecord {
  webhook_secret?: string;
}

export interface TriggerList {
  items: TriggerRecord[];
  total: number;
  cross_tenant: boolean;
}

export interface ListTriggersParams {
  tenantScope?: TenantScope;
  agentName?: string;
  /** Stream H.6 — narrow to one agent version (AgentDetail Triggers
   *  tab). Requires ``agentName`` (backend 422s otherwise). */
  agentVersion?: string;
}

export async function listTriggers(
  params: ListTriggersParams = {},
): Promise<TriggerList> {
  const { tenantScope, agentName, agentVersion } = params;
  const query = withTenantScope(
    { agent_name: agentName, agent_version: agentVersion },
    tenantScope,
  );
  const response = await apiClient.get<TriggerList>("/v1/triggers", { params: query });
  return response.data;
}

export async function getTrigger(
  triggerId: string,
  tenantScope?: TenantScope,
): Promise<TriggerRecord> {
  const response = await apiClient.get<TriggerRecord>(
    `/v1/triggers/${encodeURIComponent(triggerId)}`,
    { params: withTenantScope({}, tenantScope) },
  );
  return response.data;
}

export interface CreateTriggerBody {
  agent_name: string;
  agent_version: string;
  name: string;
  kind: TriggerKind;
  /** For cron: ``{expr: "0 9 * * *"}``. For webhook: optional config
   *  (e.g. expected content-type). */
  config: Record<string, unknown>;
}

export async function createTrigger(
  body: CreateTriggerBody,
): Promise<TriggerCreateResponse> {
  const response = await apiClient.post<TriggerCreateResponse>(
    "/v1/triggers",
    body,
  );
  return response.data;
}

export interface PatchTriggerBody {
  enabled?: boolean;
  config?: Record<string, unknown>;
}

export async function patchTrigger(
  triggerId: string,
  body: PatchTriggerBody,
): Promise<TriggerRecord> {
  const response = await apiClient.patch<TriggerRecord>(
    `/v1/triggers/${encodeURIComponent(triggerId)}`,
    body,
  );
  return response.data;
}

export async function deleteTrigger(triggerId: string): Promise<void> {
  await apiClient.delete(`/v1/triggers/${encodeURIComponent(triggerId)}`);
}

/** Spec 1 PR4 Task 3's synchronous "run now" result — mirrors the backend's
 *  ``_FireNowResponse`` (``POST /v1/triggers/{id}:fire``). ``delivery`` is
 *  one of ``delivered / pending / skipped / no_output / no_checkpointer /
 *  agent_unavailable / error``; ``delivered_text`` is only set when
 *  ``delivery === "delivered"``. */
export interface FireNowResult {
  run_id: string;
  thread_id: string;
  run_status: string;
  trigger_run_status: string;
  delivery: string;
  delivered_text?: string | null;
}

/** Fire a cron task immediately (debug-console 立即触发). The backend holds
 *  the request open for a bounded poll (``trigger_fire_now_timeout_s``,
 *  default 60s) until the run reaches a terminal state, so callers must
 *  show a loading state for up to that long. ``:fire`` is a literal URL
 *  segment (not part of the id) — same ``:verb`` suffix convention as
 *  ``purgeSession``'s ``:purge``, so only the id itself is encoded. */
export async function fireTriggerNow(triggerId: string): Promise<FireNowResult> {
  const response = await apiClient.post<FireNowResult>(
    `/v1/triggers/${encodeURIComponent(triggerId)}:fire`,
  );
  return response.data;
}
