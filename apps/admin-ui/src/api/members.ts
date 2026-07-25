/**
 * Members SDK — backed by ``/v1/members`` (Stream R W2).
 *
 * Backend returns the ``{success, data, error}`` envelope. Tenant-scoped:
 * an admin manages members of their own tenant. Invitation is a batch
 * call returning per-item results so a partial failure (Keycloak
 * conflict / unavailable) surfaces without aborting the whole batch.
 */
import { apiClient, getJson, postJson } from "./client";
import type { PurgeSummary } from "./users";

export type MemberRole = "admin" | "operator" | "viewer";

export type MemberStatus = "invited" | "active" | "suspended" | "revoked";

export interface TenantMember {
  id: string;
  tenant_id: string;
  email: string;
  display_name: string | null;
  role: MemberRole;
  status: MemberStatus;
  keycloak_user_id: string | null;
  subject_id: string | null;
  invited_by: string;
  invited_at: string | null;
  activated_at: string | null;
  updated_at: string | null;
}

export interface MemberList {
  items: TenantMember[];
  total: number;
}

export interface InvitationItem {
  email: string;
  role: MemberRole;
  display_name?: string;
}

export interface InviteResultItem {
  email: string;
  member_id: string | null;
  status: string | null;
  error_code: string | null;
}

export interface InviteResult {
  results: InviteResultItem[];
}

export interface ResendResult {
  member_id: string;
  status: string;
  keycloak_user_id: string | null;
}

export interface ListMembersParams {
  status?: MemberStatus;
  limit?: number;
  offset?: number;
  /** ``true`` requests the cross-tenant aggregate (``tenant_id="*"``);
   *  requires system_admin server-side (403 otherwise). Read-only. */
  crossTenant?: boolean;
}

export async function listMembers(
  params: ListMembersParams = {},
): Promise<MemberList> {
  const { status, limit, offset, crossTenant } = params;
  return getJson<MemberList>("/v1/members", {
    params: {
      status,
      limit,
      offset,
      ...(crossTenant ? { tenant_id: "*" } : {}),
    },
  });
}

export async function inviteMembers(
  invitations: InvitationItem[],
): Promise<InviteResult> {
  return postJson<InviteResult>("/v1/members/invite", { invitations });
}

export async function resendMember(memberId: string): Promise<ResendResult> {
  return postJson<ResendResult>(
    `/v1/members/${encodeURIComponent(memberId)}/resend`,
    {},
  );
}

export async function revokeMember(memberId: string): Promise<void> {
  await apiClient.delete(`/v1/members/${encodeURIComponent(memberId)}`);
}

export async function resetMemberPassword(
  memberId: string,
  password: string,
): Promise<void> {
  await postJson(
    `/v1/members/${encodeURIComponent(memberId)}/reset-password`,
    { password },
  );
}

/** ``POST /v1/members/{id}:purge`` response — deletion-hygiene PR5. Every
 *  step after the lifecycle transition is best-effort, so failures surface
 *  as booleans instead of an error status. ``purge`` is ``null`` when the
 *  member never signed in (``subject_id`` null — no business data). */
export interface MemberPurgeResult {
  member_id: string;
  status: MemberStatus;
  kc_deleted: boolean;
  kc_delete_failed: boolean;
  role_bindings_removed: number;
  role_bindings_cleanup_failed: boolean;
  data_purged: boolean;
  purge: PurgeSummary | null;
}

/** ``true`` when any best-effort step failed — the confirm modal stays
 *  open so a retry (the endpoint is idempotent) is one click away. Note
 *  ``purge.ok === false`` is the expected shape on deployments without a
 *  supervisor (the workspace step fails there) — still "partial". */
export function isMemberPurgePartial(result: MemberPurgeResult): boolean {
  return (
    result.kc_delete_failed ||
    result.role_bindings_cleanup_failed ||
    (result.purge !== null && !result.purge.ok)
  );
}

/** POST /v1/members/{id}:purge — one-shot deactivate + purge (admin-only).
 *  Lifecycle transition (invited→revoked / active→suspended), role-binding
 *  cleanup, Keycloak account DELETE, then the ``purge_user`` data cascade
 *  for members who have signed in. Idempotent — safe to re-run to finish
 *  a partial purge. */
export async function purgeMember(memberId: string): Promise<MemberPurgeResult> {
  return postJson<MemberPurgeResult>(
    `/v1/members/${encodeURIComponent(memberId)}:purge`,
    {},
  );
}
