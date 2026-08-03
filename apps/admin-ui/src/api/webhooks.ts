/**
 * Webhook endpoints SDK — backed by ``/v1/webhook-endpoints`` (HX-9).
 *
 * Outbound webhook hooks (STREAM-HX § 13): the platform signs and POSTs
 * agent-lifecycle events to a tenant-registered URL. Mirrors the triggers
 * SDK — raw ``JSONResponse`` payloads (not the ``{success,data,error}``
 * envelope), so the SDK goes through ``apiClient`` directly.
 *
 * The HMAC signing ``secret`` is **show-once**: the create response carries
 * it once; subsequent GETs / PATCHes never do. Rotation = delete + re-create.
 */
import { apiClient, withTenantScope, type TenantScope } from "./client";

export type WebhookEventType =
  | "run.completed"
  | "run.failed"
  | "approval.requested"
  | "artifact.saved"
  | "skill_promote.requested"
  | "quality.drift";

export const WEBHOOK_EVENT_TYPES: readonly WebhookEventType[] = [
  "run.completed",
  "run.failed",
  "approval.requested",
  "artifact.saved",
  "skill_promote.requested",
  "quality.drift",
];

/** Delivery body shape — the signed expert_work envelope, or an IM bot message. */
export type WebhookPayloadFormat = "generic" | "feishu" | "dingtalk" | "wecom";

export const WEBHOOK_PAYLOAD_FORMATS: readonly WebhookPayloadFormat[] = [
  "generic",
  "feishu",
  "dingtalk",
  "wecom",
];

export interface WebhookEndpoint {
  id: string;
  name: string;
  url: string;
  event_types: WebhookEventType[];
  agent_name: string | null;
  enabled: boolean;
  source: string;
  payload_format: WebhookPayloadFormat;
  created_at: string;
  updated_at: string;
}

/** Create response carries the one-time HMAC ``secret`` — show once, never store. */
export interface WebhookEndpointCreateResponse extends WebhookEndpoint {
  secret?: string;
}

export interface WebhookEndpointList {
  items: WebhookEndpoint[];
  total: number;
  cross_tenant: boolean;
}

export interface ListWebhookEndpointsParams {
  tenantScope?: TenantScope;
  agentName?: string;
}

export async function listWebhookEndpoints(
  params: ListWebhookEndpointsParams = {},
): Promise<WebhookEndpointList> {
  const { tenantScope, agentName } = params;
  const query = withTenantScope({ agent_name: agentName }, tenantScope);
  const response = await apiClient.get<WebhookEndpointList>("/v1/webhook-endpoints", {
    params: query,
  });
  return response.data;
}

export interface CreateWebhookEndpointBody {
  name: string;
  url: string;
  event_types: WebhookEventType[];
  agent_name?: string | null;
  payload_format?: WebhookPayloadFormat;
}

export async function createWebhookEndpoint(
  body: CreateWebhookEndpointBody,
): Promise<WebhookEndpointCreateResponse> {
  const response = await apiClient.post<WebhookEndpointCreateResponse>(
    "/v1/webhook-endpoints",
    body,
  );
  return response.data;
}

export interface PatchWebhookEndpointBody {
  url?: string;
  event_types?: WebhookEventType[];
  agent_name?: string | null;
  enabled?: boolean;
  payload_format?: WebhookPayloadFormat;
}

export async function patchWebhookEndpoint(
  endpointId: string,
  body: PatchWebhookEndpointBody,
): Promise<WebhookEndpoint> {
  const response = await apiClient.patch<WebhookEndpoint>(
    `/v1/webhook-endpoints/${encodeURIComponent(endpointId)}`,
    body,
  );
  return response.data;
}

export async function deleteWebhookEndpoint(endpointId: string): Promise<void> {
  await apiClient.delete(`/v1/webhook-endpoints/${encodeURIComponent(endpointId)}`);
}
