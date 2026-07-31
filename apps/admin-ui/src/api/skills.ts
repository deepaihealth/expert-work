/**
 * Skills SDK — backed by ``/v1/skills`` (Stream J.7a).
 *
 * Stream H.1b PR 3 added the list-only skeleton; Stream H.4 PR 5 fills
 * in the full surface (create / version / status patch / ZIP import +
 * export) plus a latent bug fix:
 *
 * **Latent bug fix (H.4 PR 5)**: H.1b PR 3 used ``getJson`` which
 * unwraps a ``{success, data, error}`` envelope, but the skills
 * backend returns raw ``JSONResponse(content={...})`` payloads —
 * matches the curation latent-bug fix in H.4 PR 1. The SDK now goes
 * through ``apiClient`` directly.
 *
 * Also: cursor pagination (no ``total``), so the list response
 * exposes ``next_cursor`` + ``cross_tenant`` only.
 */
import { apiClient, withTenantScope, type TenantScope } from "./client";

export type SkillStatus = "draft" | "active" | "stale" | "archived";

/** Stream SE (SE-8) — skill visibility + version provenance literals. */
export type SkillVisibility = "agent_private" | "tenant";
export type EvolutionOrigin = "in_session" | "distilled";

export interface SkillRecord {
  id: string;
  /** Cross-tenant W3 — owning tenant, present on list + detail responses
   *  from a W3 backend. Powers the "*" aggregate row-jump (SkillsList →
   *  SkillDetail with ``?tenant_id=``). Optional: absent on older backends
   *  and on platform rows. */
  tenant_id?: string;
  name: string;
  status: SkillStatus;
  latest_version: number | null;
  description: string;
  category: string;
  /** Capability Uplift Sprint #4 (Mini-ADR U-25). The Curator's
   *  escape-hatch flag — pinned skills are skipped at every state
   *  transition. Surfaced on every skill_dict response. */
  pinned: boolean;
  /** ISO timestamp of the most recent bind or skill_view activity,
   *  throttled to once per skill per hour per replica. ``null`` when
   *  the skill was created but never touched (newly-created drafts). */
  last_used_at: string | null;
  /** ISO timestamp of the most recent status transition (manual or
   *  Curator). Powers the "X days until stale" hint. */
  state_changed_at: string | null;
  created_at: string;
  updated_at: string;
  /** Stream X-6 merged view — present on rows from ``GET /v1/skills``.
   *  ``"tenant"`` for the tenant's own skills, ``"platform"`` for curated
   *  platform skills surfaced alongside them. */
  source?: "tenant" | "platform";
  /** Stream X-6 — whether the tenant's plan tier meets the skill's
   *  ``required_tier``. Tenant rows are always entitled; platform rows
   *  may be locked. */
  entitled?: boolean;
  /** Stream X-6 — platform-skill entitlement gate. */
  required_tier?: "free" | "pro" | "enterprise";
  /** Skill Marketplace Phase 2 — on ``platform`` rows from the merged
   *  ``GET /v1/skills`` view, whether the tenant has an active subscription
   *  to this platform skill. Semantic A: a UX/accounting marker only, it does
   *  not gate runtime binding. Absent on tenant rows. */
  subscribed?: boolean;
  /** Stream SE (SE-8) — ``agent_private`` = only the authoring agent sees
   *  it; ``tenant`` = shared tenant-wide. Optional so pre-SE mocks stay valid. */
  visibility?: SkillVisibility;
  /** Stream SE (SE-8) — owning per-user agent (agent-authored skills). */
  created_by_user_id?: string | null;
  created_by_agent_name?: string | null;
  /** Stream SE (SE-8) — fork lineage source skill id. */
  forked_from?: string | null;
}

/** Metadata for one supporting file on a skill version.
 *
 *  Capability Uplift Sprint #3 (Mini-ADR U-16/U-20). The version detail
 *  response carries metadata only — the file's base64 ``content`` is
 *  fetched lazily through :func:`getSupportingFile` when the user clicks
 *  a path in the file tree. Keeps version-list responses cheap even when
 *  a skill has megabytes of supporting files.
 */
export interface SupportingFileMeta {
  size: number;
  mime: string;
}

export interface SupportingFileBody extends SupportingFileMeta {
  /** Base64-encoded raw bytes. UTF-8 text → ``atob`` to render; binary
   *  → preview as ``[BINARY: ...]`` placeholder per Admin UI design. */
  content: string;
}

export interface SkillVersion {
  id: string;
  skill_id: string;
  version: number;
  prompt_fragment: string;
  tool_names: string[];
  description: string;
  category: string;
  required_models: string[];
  authored_by: string;
  /** Path → metadata. Empty object when the version has no supporting
   *  files (e.g., versions created via the JSON-API ``POST .../versions``
   *  endpoint instead of ZIP import). */
  supporting_files: Record<string, SupportingFileMeta>;
  /** Mini-ADR U-15 — when ``true``, body is fetched via ``skill_view``
   *  tool at agent runtime instead of being eager-loaded into the system
   *  prompt. UI shows a debug badge. */
  lazy_load: boolean;
  /** Mini-ADR U-24 — version declares any of ``exec_python``/
   *  ``exec_shell``/``http`` OR has a ``scripts/*`` supporting file.
   *  Activate flow requires admin role + UI shows a 🔒 badge. */
  high_risk: boolean;
  /** Stream SE (SE-8) — evolution provenance for the lineage view. Optional
   *  so pre-SE mocks stay valid. ``null`` origin = human-authored. */
  evolution_origin?: EvolutionOrigin | null;
  distilled_from_trajectory_key?: string | null;
  distilled_from_candidate_id?: string | null;
  evolution_round?: number;
  created_at: string;
}

export interface SkillList {
  items: SkillRecord[];
  /** Stream X-6 merged view — active platform skills surfaced alongside
   *  the tenant's own. ``[]`` when none. Name-shadowing (a tenant skill
   *  of the same name hides the platform one) is applied server-side. */
  platform_items: SkillRecord[];
  /** Opaque UUID-encoded cursor; pass back verbatim. */
  next_cursor: string | null;
  cross_tenant: boolean;
}

export interface ListSkillsParams {
  tenantScope?: TenantScope;
  status?: SkillStatus;
  category?: string;
  cursor?: string | null;
  limit?: number;
  /** Stream SE (SE-8) — filter to the agent-self-authored slice. */
  visibility?: SkillVisibility;
  createdByUserId?: string;
  /** Stream H.6 (Mini-ADR H-11) — skills authored by this agent
   *  (AgentDetail Skills tab). */
  createdByAgentName?: string;
}

export async function listSkills(
  params: ListSkillsParams = {},
): Promise<SkillList> {
  const {
    tenantScope,
    status,
    category,
    cursor,
    limit,
    visibility,
    createdByUserId,
    createdByAgentName,
  } = params;
  const query = withTenantScope(
    {
      status,
      category,
      cursor: cursor ?? undefined,
      limit,
      visibility,
      created_by_user_id: createdByUserId,
      created_by_agent_name: createdByAgentName,
    },
    tenantScope,
  );
  const response = await apiClient.get<SkillList>("/v1/skills", { params: query });
  return response.data;
}

export async function getSkill(
  skillId: string,
  tenantScope?: TenantScope,
): Promise<SkillRecord> {
  const response = await apiClient.get<SkillRecord>(
    `/v1/skills/${encodeURIComponent(skillId)}`,
    { params: withTenantScope({}, tenantScope) },
  );
  return response.data;
}

export interface CreateSkillBody {
  name: string;
  description: string;
  category: string;
}

export async function createSkill(body: CreateSkillBody): Promise<SkillRecord> {
  const response = await apiClient.post<SkillRecord>("/v1/skills", body);
  return response.data;
}

export interface PatchSkillStatusBody {
  status?: SkillStatus;
  /** Sprint #4 (Mini-ADR U-30) — admin pin / unpin. Pinning a
   *  high-risk skill requires admin role server-side (403 otherwise). */
  pinned?: boolean;
}

export async function patchSkillStatus(
  skillId: string,
  body: PatchSkillStatusBody,
): Promise<SkillRecord> {
  const response = await apiClient.patch<SkillRecord>(
    `/v1/skills/${encodeURIComponent(skillId)}`,
    body,
  );
  return response.data;
}

export interface AddSkillVersionBody {
  prompt_fragment: string;
  tool_names: string[];
  description: string;
  category: string;
  required_models?: string[];
  authored_by?: string;
}

export async function addSkillVersion(
  skillId: string,
  body: AddSkillVersionBody,
): Promise<SkillVersion> {
  const response = await apiClient.post<SkillVersion>(
    `/v1/skills/${encodeURIComponent(skillId)}/versions`,
    body,
  );
  return response.data;
}

/** Edit ``SKILL.md`` (the prompt fragment) → new version that inherits all
 *  other fields incl. supporting files (skill-authoring-ia Phase D-2). */
export async function putSkillPrompt(
  skillId: string,
  versionNumber: number,
  promptFragment: string,
): Promise<SkillVersion> {
  const response = await apiClient.put<SkillVersion>(
    `/v1/skills/${encodeURIComponent(skillId)}/versions/${versionNumber}/prompt`,
    { prompt_fragment: promptFragment },
  );
  return response.data;
}

export interface SkillVersionList {
  items: SkillVersion[];
}

export async function listSkillVersions(
  skillId: string,
  tenantScope?: TenantScope,
): Promise<SkillVersionList> {
  const response = await apiClient.get<SkillVersionList>(
    `/v1/skills/${encodeURIComponent(skillId)}/versions`,
    { params: withTenantScope({}, tenantScope) },
  );
  return response.data;
}

export async function getSkillVersion(
  skillId: string,
  versionNumber: number,
  tenantScope?: TenantScope,
): Promise<SkillVersion> {
  const response = await apiClient.get<SkillVersion>(
    `/v1/skills/${encodeURIComponent(skillId)}/versions/${versionNumber}`,
    { params: withTenantScope({}, tenantScope) },
  );
  return response.data;
}

export interface ImportSkillZipResponse {
  skill: SkillRecord;
  version: SkillVersion;
}

/** Multipart upload of a ``.skill`` ZIP — creates the skill (if absent)
 *  + appends a new version. */
export async function importSkillZip(file: File | Blob): Promise<ImportSkillZipResponse> {
  const form = new FormData();
  form.append("file", file);
  const response = await apiClient.post<ImportSkillZipResponse>(
    "/v1/skills/import",
    form,
    {
      // Let axios + the browser pick the multipart boundary.
      headers: { "Content-Type": "multipart/form-data" },
    },
  );
  return response.data;
}

/** Download a versioned skill as a ``.skill`` ZIP. Returns the raw
 *  ``Blob`` so the caller can drive an ``<a download>`` flow. */
export async function exportSkillVersion(
  skillId: string,
  versionNumber: number,
  tenantScope?: TenantScope,
): Promise<Blob> {
  const response = await apiClient.get<Blob>(
    `/v1/skills/${encodeURIComponent(skillId)}/versions/${versionNumber}/export`,
    { responseType: "blob", params: withTenantScope({}, tenantScope) },
  );
  return response.data;
}

// ─── Capability Uplift Sprint #3 (Mini-ADR U-17/U-20) ───────────────────
// Single-file supporting-files mutation API. Each call creates a NEW
// SkillVersion (preserves D3 immutability); callers should navigate to
// the new version after the response.

function encodeFilePath(filePath: string): string {
  // Mirror FastAPI's ``{path:path}`` behavior: keep slashes intact,
  // encode each segment individually so spaces / unicode / reserved
  // chars in filenames survive transit.
  return filePath
    .split("/")
    .map((segment) => encodeURIComponent(segment))
    .join("/");
}

export async function getSupportingFile(
  skillId: string,
  versionNumber: number,
  filePath: string,
  tenantScope?: TenantScope,
): Promise<SupportingFileBody> {
  const response = await apiClient.get<SupportingFileBody>(
    `/v1/skills/${encodeURIComponent(skillId)}/versions/${versionNumber}/supporting-files/${encodeFilePath(filePath)}`,
    { params: withTenantScope({}, tenantScope) },
  );
  return response.data;
}

export interface PutSupportingFileBody {
  /** Base64-encoded raw bytes. UI converts text via ``btoa(unescape(encodeURIComponent(text)))``
   *  to preserve multi-byte UTF-8. */
  content: string;
  size: number;
  mime: string;
}

/** Add or replace a single supporting file. Returns the new
 *  SkillVersion. Path validation + threat scan + high_risk recompute
 *  all happen server-side; a 400 means the content was rejected. */
export async function putSupportingFile(
  skillId: string,
  versionNumber: number,
  filePath: string,
  body: PutSupportingFileBody,
): Promise<SkillVersion> {
  const response = await apiClient.put<SkillVersion>(
    `/v1/skills/${encodeURIComponent(skillId)}/versions/${versionNumber}/supporting-files/${encodeFilePath(filePath)}`,
    body,
  );
  return response.data;
}

/** Remove a single supporting file. Returns the new SkillVersion. */
export async function deleteSupportingFile(
  skillId: string,
  versionNumber: number,
  filePath: string,
): Promise<SkillVersion> {
  const response = await apiClient.delete<SkillVersion>(
    `/v1/skills/${encodeURIComponent(skillId)}/versions/${versionNumber}/supporting-files/${encodeFilePath(filePath)}`,
  );
  return response.data;
}

/** Rename = put-new + delete-old. We do put first so a failure leaves
 *  the original intact; a successful put followed by a failed delete
 *  leaves both, which the user can recover from in the UI. Returns the
 *  version produced by the delete (the latest one). */
export async function renameSupportingFile(
  skillId: string,
  versionNumber: number,
  oldPath: string,
  newPath: string,
  body: SupportingFileBody,
): Promise<SkillVersion> {
  const afterPut = await putSupportingFile(skillId, versionNumber, newPath, {
    content: body.content,
    size: body.size,
    mime: body.mime,
  });
  return await deleteSupportingFile(skillId, afterPut.version, oldPath);
}

/** Base64-encode a UTF-8 string for ``putSupportingFile``. ``btoa``
 *  alone breaks on non-ASCII; this preserves multi-byte chars. */
export function encodeUtf8Base64(text: string): string {
  return btoa(
    Array.from(new TextEncoder().encode(text))
      .map((byte) => String.fromCharCode(byte))
      .join(""),
  );
}

/** Decode a base64 string returned by ``getSupportingFile`` back to
 *  UTF-8 text. Returns ``null`` for non-UTF-8 (binary) payloads — UI
 *  shows a ``[BINARY: ... bytes]`` placeholder rather than corrupted
 *  text. */
export function decodeBase64Utf8(base64: string): string | null {
  try {
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i);
    }
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return null;
  }
}
