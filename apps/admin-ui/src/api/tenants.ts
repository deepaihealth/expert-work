/**
 * Tenants SDK — backed by ``POST /v1/tenants`` (Stream P, Mini-ADR P-1/P-2/P-5).
 *
 * Tenant creation is a **platform-level** operation: the backend gates it on
 * ``is_system_admin`` (no ``tenant`` RBAC resource — see ``api/tenants.py``),
 * so the UI hides the page behind the same check. ``tenant_id`` is optional —
 * omit it to let the server generate one (the common case); supply one for
 * idempotent provisioning from an upstream system.
 *
 * Backend returns the standard ``{success, data, error}`` envelope; the
 * unwrapped payload is a full ``TenantConfigRecord`` (reused from the
 * tenant-config SDK so the two stay in sync).
 */
import { getJson, postJson } from "./client";
import type { TenantConfigRecord, TenantPlan } from "./tenant_config";

export interface CreateTenantBody {
  /** Omit to let the server generate a UUID (recommended). */
  tenant_id?: string;
  display_name: string;
  plan?: TenantPlan;
  /**
   * Provision the company's first admin in the same step (Stream R W1). Omit
   * for a bare tenant. ``first_admin_display_name`` requires ``first_admin_email``.
   */
  first_admin_email?: string;
  first_admin_display_name?: string;
}

/** First-admin summary echoed back when ``first_admin_email`` was supplied. */
export interface FirstAdminSummary {
  member_id: string;
  email: string;
  status: string;
  keycloak_user_id: string | null;
  /**
   * The generated password, in the clear — but only on *this* response.
   * The backend never returns it again after this call. ``null`` when the
   * account was provisioned via the email/set-password flow instead of a
   * generated password.
   */
  initial_password?: string | null;
  /**
   * Password mode only: ``true`` when Keycloak accepted the account but the
   * initial-password reset failed, so ``initial_password`` is null *and* the
   * admin cannot sign in. Recover with :func:`resendFirstAdmin`.
   */
  credential_pending?: boolean;
}

/** ``POST /v1/tenants`` data payload: the tenant record + optional first admin. */
export type CreatedTenant = TenantConfigRecord & { first_admin?: FirstAdminSummary };

export async function createTenant(body: CreateTenantBody): Promise<CreatedTenant> {
  return postJson<CreatedTenant>("/v1/tenants", body);
}

/** ``GET /v1/tenants`` list row — a compact tenant summary. */
export interface TenantSummary {
  tenant_id: string;
  display_name: string;
  plan: TenantPlan;
  created_at: string;
  status: "active" | "suspended";
  /** The synthetic platform tenant — owns platform-level shared resources and
   *  is reached via the ``*`` scope, not as a peer row. Hidden from the
   *  customer-tenant table + switcher. Optional so pre-flag mocks stay valid. */
  is_platform?: boolean;
}

export async function listTenants(limit = 50, offset = 0): Promise<TenantSummary[]> {
  return getJson<TenantSummary[]>(`/v1/tenants?limit=${limit}&offset=${offset}`);
}

export async function deactivateTenant(tenantId: string): Promise<void> {
  await postJson(`/v1/tenants/${tenantId}/deactivate`, {});
}

export async function activateTenant(tenantId: string): Promise<void> {
  await postJson(`/v1/tenants/${tenantId}/activate`, {});
}

/** ``POST /v1/tenants/{id}/first-admin/resend`` data payload. */
export interface FirstAdminResendResult {
  member_id: string;
  email: string;
  status: string;
  keycloak_user_id: string | null;
  /** Fresh generated password (password mode) — shown once, never returned again. */
  initial_password?: string | null;
  credential_pending?: boolean;
}

/**
 * Re-drive a tenant's first admin's credential handoff from the platform
 * scope (system_admin). The per-tenant member "resend" needs a tenant-admin
 * principal, which nobody has when that very admin never got a password.
 */
export async function resendFirstAdmin(tenantId: string): Promise<FirstAdminResendResult> {
  return postJson<FirstAdminResendResult>(`/v1/tenants/${tenantId}/first-admin/resend`, {});
}
