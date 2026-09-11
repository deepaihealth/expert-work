/**
 * Memory SDK — backed by ``/v1/memory`` (Stream K.K6).
 *
 * Stream H.1b PR 3 added list-only. Stream H.4 PR 2 fills in PATCH
 * (update content + kind, requires server-side embedder) and DELETE
 * (soft-delete with 30-day retention).
 *
 * Per-user scoping is enforced server-side — the caller's ``user_id``
 * is derived from their principal, not a query parameter. System_admin
 * via ``tenant_id=*`` cross-tenant aggregates across every user (note:
 * cross-tenant view intentionally drops the per-user binding so
 * platform admin sees the whole picture).
 */
import {
  apiClient,
  getJson,
  patchJson,
  postJson,
  withTenantScope,
  type TenantScope,
} from "./client";
import { listAudit, type AuditEntry } from "./audit";

export type MemoryKind = "fact" | "episodic";

export interface MemoryItem {
  id: string;
  tenant_id: string;
  user_id: string;
  kind: MemoryKind;
  content: string;
  created_at: string;
  /** Stream Memory-Enhance (M-2) — 0–1 scores. ``importance`` feeds the
   *  write-filter; ``confidence`` is 1.0 after a user correction (M-4). */
  importance: number;
  confidence: number;
  // P5b provenance + bi-temporal (already on the wire via full model_dump).
  source_thread_id?: string | null;
  source_run_id?: string | null;
  valid_at?: string | null;
  expired_at?: string | null;
  invalid_at?: string | null;
  expected_valid_days?: number | null;
}

export interface MemoryList {
  items: MemoryItem[];
  total: number;
  cross_tenant: boolean;
}

export interface ListMemoriesParams {
  tenantScope?: TenantScope;
  kind?: MemoryKind;
  limit?: number;
  /** Tenant-admin governance view of one member's memories (M2 user
   *  detail). Non-admins asking for someone else get a 403. */
  userId?: string;
  /** ISO8601 — time-travel: memories as they were valid at this instant.
   *  Omit for the current view. */
  as_of?: string;
}

export async function listMemories(
  params: ListMemoriesParams = {},
): Promise<MemoryList> {
  const { tenantScope, kind, limit, userId, as_of } = params;
  const query = withTenantScope({ kind, limit, user_id: userId, as_of }, tenantScope);
  return getJson<MemoryList>("/v1/memory", { params: query });
}

export interface UpdateMemoryBody {
  content: string;
  /** Optional re-classification when the reviewer corrects the
   *  worker's auto-tag. Backend keeps the existing kind when omitted. */
  kind?: MemoryKind;
}

/** ``userId`` is the tenant-admin governance target (M2 user detail —
 *  editing another user's memory via ``?user_id=``); omit for the
 *  caller's own memory. */
export async function updateMemory(
  memoryId: string,
  body: UpdateMemoryBody,
  userId?: string,
): Promise<MemoryItem> {
  return patchJson<MemoryItem>(
    `/v1/memory/${encodeURIComponent(memoryId)}`,
    body,
    userId ? { params: { user_id: userId } } : undefined,
  );
}

/** DELETE returns 204 No Content — no body. ``userId`` is the tenant-admin
 *  governance target (forgetting another user's memory). */
export async function deleteMemory(memoryId: string, userId?: string): Promise<void> {
  await apiClient.delete(`/v1/memory/${encodeURIComponent(memoryId)}`, {
    params: { user_id: userId },
  });
}

/** Stream Memory-Enhance (M-4) — an end-user's authoritative self-correction:
 *  ``rewrite`` replaces the content (sets confidence to 1.0) or ``forget``
 *  marks it wrong (soft-delete). Audited as ``MEMORY_CORRECT``. */
export interface CorrectMemoryBody {
  action: "rewrite" | "forget";
  /** Required for ``rewrite``. */
  content?: string;
}

export async function correctMemory(
  memoryId: string,
  body: CorrectMemoryBody,
): Promise<MemoryItem | null> {
  return postJson<MemoryItem | null>(
    `/v1/memory/${encodeURIComponent(memoryId)}/correct`,
    body,
  );
}

/**
 * B-51 — long-term memory consolidation health, read off the audit log.
 *
 * The consolidator single-flights across replicas behind an advisory lock, so
 * only one replica ever has in-process state; the per-sweep
 * ``memory:consolidator_run`` audit row is the one thing every replica (and
 * the console) can read back. Reading it needs the platform-wide scope, so
 * this is a system_admin-only read — callers skip it for everyone else.
 */
export interface ConsolidatorSweep {
  occurred_at: string | null;
  consolidated: number;
  errors: number;
  errors_by_reason: Record<string, number>;
  missing_credential_providers: string[];
}

export interface ConsolidatorHealth {
  /** 从最近一次往前数,连续几次 sweep 栽在「平台凭据未配置」上。0 = 最近一次没栽。 */
  consecutiveCredentialFailures: number;
  /** 那些 sweep 缺的 provider(去重)。 */
  providers: string[];
  lastSweepAt: string | null;
}

function toSweep(entry: AuditEntry): ConsolidatorSweep {
  const details = entry.details as Record<string, unknown>;
  const reasons = details.errors_by_reason;
  const providers = details.missing_credential_providers;
  return {
    occurred_at: entry.occurred_at,
    consolidated: typeof details.consolidated === "number" ? details.consolidated : 0,
    errors: typeof details.errors === "number" ? details.errors : 0,
    errors_by_reason:
      reasons !== null && typeof reasons === "object"
        ? (reasons as Record<string, number>)
        : {},
    missing_credential_providers: Array.isArray(providers)
      ? providers.filter((p): p is string => typeof p === "string")
      : [],
  };
}

/** 纯函数,好单测:审计行(新→旧)→ 连续凭据失败次数。 */
export function summariseConsolidatorHealth(
  sweeps: ConsolidatorSweep[],
): ConsolidatorHealth {
  const providers: string[] = [];
  let streak = 0;
  for (const sweep of sweeps) {
    if ((sweep.errors_by_reason.credentials_missing ?? 0) === 0) break;
    streak += 1;
    for (const provider of sweep.missing_credential_providers) {
      if (!providers.includes(provider)) providers.push(provider);
    }
  }
  return {
    consecutiveCredentialFailures: streak,
    providers,
    lastSweepAt: sweeps[0]?.occurred_at ?? null,
  };
}

export async function getConsolidatorHealth(
  limit = 20,
): Promise<ConsolidatorHealth> {
  const page = await listAudit({
    tenantScope: "*",
    action: "memory:consolidator_run",
    limit,
  });
  return summariseConsolidatorHealth(page.items.map(toSweep));
}
