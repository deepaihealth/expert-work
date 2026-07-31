/**
 * User-scoped workspace SDK — the persistent volume keyed on ``(tenant, user)``,
 * reachable independent of any thread (so it survives session deletion, the
 * whole point of ``/v1/workspace`` vs the thread-scoped session routes).
 *
 * Mirrors the thread-scoped calls in ``sessions.ts`` but drops the ``threadId``:
 * the backend resolves the caller's own user, or — for a tenant admin — the
 * ``userId`` governance target (the M2 user-detail Workspace tab). The
 * playground inspector uses the self form (``userId`` omitted).
 */
import {
  apiClient,
  getStoredToken,
  unwrap,
  withTenantScope,
  type ApiEnvelope,
  type TenantScope,
} from "./client";
import type { SessionWorkspace, WorkspaceFile } from "./sessions";

/** GET /v1/workspace — the target user's persistent workspace + artifacts.
 *  ``workspace`` is null when no VM has ever started for that user. */
export async function getUserWorkspace(
  userId?: string,
  tenantScope?: TenantScope,
): Promise<SessionWorkspace> {
  const response = await apiClient.get<ApiEnvelope<SessionWorkspace>>(
    "/v1/workspace",
    { params: withTenantScope({ user_id: userId }, tenantScope) },
  );
  return unwrap(response.data);
}

/** GET /v1/workspace/files — browse the files in the target user's volume. */
export async function getUserWorkspaceFiles(
  userId?: string,
  tenantScope?: TenantScope,
): Promise<WorkspaceFile[]> {
  const response = await apiClient.get<ApiEnvelope<{ files: WorkspaceFile[] }>>(
    "/v1/workspace/files",
    { params: withTenantScope({ user_id: userId }, tenantScope) },
  );
  return unwrap(response.data).files;
}

/** Download one workspace file (manual Bearer + Blob save — a plain anchor
 *  href can't carry the token). Triggers a browser save. */
export async function downloadUserWorkspaceFile(
  path: string,
  userId?: string,
  tenantScope?: TenantScope,
): Promise<void> {
  const token = getStoredToken();
  const params = new URLSearchParams({ path });
  if (userId) params.set("user_id", userId);
  if (tenantScope !== undefined) params.set("tenant_id", tenantScope);
  const url = `/v1/workspace/file?${params.toString()}`;
  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const response = await fetch(url, { headers });
  if (!response.ok) {
    throw new Error(`workspace file download failed: HTTP ${response.status}`);
  }
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = path.split("/").pop() || "download";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}

/** DELETE /v1/workspace/file — hard-delete one file from the target user's
 *  workspace volume. */
export async function deleteUserWorkspaceFile(
  path: string,
  userId?: string,
): Promise<void> {
  await apiClient.delete("/v1/workspace/file", {
    params: { path, user_id: userId },
  });
}
