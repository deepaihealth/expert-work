/**
 * SDK for ``/v1/artifacts`` — Stream H.8 PR 1 (design § 6.8).
 *
 * Every endpoint here is RAW (no ``{success,data,error}`` envelope), so
 * calls go through ``apiClient`` directly — never ``getJson``
 * ([memory:envelope-vs-raw-contract-check]).
 *
 * Scope semantics (Mini-ADR H-14): the backend resolves the *caller's*
 * user for download / delete / patch / versions and hides cross-user
 * rows behind 404 — a tenant admin only ever operates on their own
 * artifacts. The cross-tenant ``"*"`` list aggregates every user but
 * carries no per-user context, so it is list-only.
 */
import { apiClient, withTenantScope, type TenantScope } from "./client";

export type ArtifactKind = "document" | "code" | "data" | "other";

export interface ArtifactListItem {
  name: string;
  kind: ArtifactKind;
  latest_version: number;
  /** Present only in the cross-tenant (``"*"``) aggregate view. */
  tenant_id?: string;
  user_id?: string;
}

export interface ArtifactList {
  items: ArtifactListItem[];
  cross_tenant: boolean;
}

export interface ArtifactVersion {
  version: number;
  path_in_workspace: string;
  /** NULL until the version's first download backfills the digest. */
  size_bytes: number | null;
  sha256: string | null;
  created_in_thread: string | null;
  created_at: string | null;
}

export interface ArtifactVersionList {
  name: string;
  versions: ArtifactVersion[];
}

export async function listArtifacts(
  params: {
    tenantScope?: TenantScope;
    /** Tenant-admin governance view of one member's artifacts (M2 user
     *  detail). Non-admins asking for someone else get a 403. */
    userId?: string;
  } = {},
): Promise<ArtifactList> {
  const query = withTenantScope({ user_id: params.userId }, params.tenantScope);
  const response = await apiClient.get<ArtifactList>("/v1/artifacts", { params: query });
  return response.data;
}

/** Extract the plain filename from a ``Content-Disposition`` header.
 *  Prefers the RFC 5987 ``filename*=UTF-8''…`` form (the backend always
 *  sends both); falls back to the quoted ASCII-safe form, then to the
 *  artifact name the caller already has. */
export function filenameFromDisposition(header: string | undefined, fallback: string): string {
  if (!header) return fallback;
  const utf8 = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (utf8?.[1]) {
    try {
      return decodeURIComponent(utf8[1]);
    } catch {
      // fall through to the quoted form
    }
  }
  const quoted = /filename="([^"]+)"/i.exec(header);
  return quoted?.[1] ?? fallback;
}

/** GET /v1/artifacts/download?name=… — fetch the latest version as a
 *  blob (the Bearer header rides the axios instance; a bare
 *  ``window.open`` would arrive unauthenticated) and hand it to the
 *  browser via an object URL. Returns the saved filename. ``userId``
 *  is the tenant-admin governance target (H.8-F1). */
export async function downloadArtifact(
  name: string,
  userId?: string,
  tenantScope?: TenantScope,
): Promise<string> {
  const response = await apiClient.get<Blob>("/v1/artifacts/download", {
    params: withTenantScope({ name, user_id: userId }, tenantScope),
    responseType: "blob",
  });
  const disposition = (response.headers as Record<string, string | undefined>)[
    "content-disposition"
  ];
  const filename = filenameFromDisposition(disposition, name);
  const url = URL.createObjectURL(response.data);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    URL.revokeObjectURL(url);
  }
  return filename;
}

/** DELETE /v1/artifacts/{name} — soft-delete (metadata only; bytes stay
 *  until the retention sweep, and re-saving the same name un-deletes). */
export async function deleteArtifact(name: string, userId?: string): Promise<void> {
  await apiClient.delete(`/v1/artifacts/${encodeURIComponent(name)}`, {
    params: { user_id: userId },
  });
}

/** PATCH /v1/artifacts/{name} — re-classify ``kind``. The backend 409s
 *  on a no-op change; callers should skip the request when unchanged
 *  (Mini-ADR H-16). */
export async function patchArtifactKind(
  name: string,
  kind: ArtifactKind,
  userId?: string,
): Promise<{ name: string; kind: ArtifactKind; latest_version: number }> {
  const response = await apiClient.patch<{
    name: string;
    kind: ArtifactKind;
    latest_version: number;
  }>(`/v1/artifacts/${encodeURIComponent(name)}`, { kind }, { params: { user_id: userId } });
  return response.data;
}

export async function listArtifactVersions(
  name: string,
  userId?: string,
  tenantScope?: TenantScope,
): Promise<ArtifactVersionList> {
  const response = await apiClient.get<ArtifactVersionList>(
    `/v1/artifacts/${encodeURIComponent(name)}/versions`,
    { params: withTenantScope({ user_id: userId }, tenantScope) },
  );
  return response.data;
}
