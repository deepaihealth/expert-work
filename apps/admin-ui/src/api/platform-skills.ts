/**
 * Platform Skill SDK — backed by ``/v1/platform/skills`` (Stream X,
 * system_admin only).
 *
 * A *platform skill* is a curated, cross-tenant reusable skill managed by
 * a platform admin: it carries a lifecycle (draft / active / archived), a
 * ``required_tier`` entitlement gate, and an immutable version history of
 * prompt fragments + tool allow-lists. Tenants see active platform skills
 * merged into their own library (see ``GET /v1/skills`` ``platform_items``)
 * but cannot mutate them.
 *
 * **Envelope-vs-raw (Stream ACCT bug fix)**: the platform-skills backend
 * returns *raw* ``JSONResponse(content={...})`` payloads — NOT the
 * ``{success, data, error}`` envelope. The original SDK went through
 * ``getJson``/``postJson``/``patchJson`` (which ``unwrap()`` an envelope),
 * so every call threw ``request failed`` against the real backend even
 * though the endpoint returned 200 with data. Now it goes through
 * ``apiClient`` directly, matching the tenant ``/v1/skills`` SDK in
 * ``./skills.ts`` (same latent bug fixed there in H.4 PR 5). HTTP errors
 * still surface as :class:`ApiError` via the response interceptor.
 */
import { apiClient } from "./client";

// ── Domain types ─────────────────────────────────────────────────────────

export type PlatformSkillStatus = "draft" | "active" | "archived";
export type PlatformSkillTier = "free" | "pro" | "enterprise";

export interface PlatformSkill {
  id: string;
  name: string;
  status: PlatformSkillStatus;
  latest_version: number | null;
  description: string;
  category: string;
  pinned: boolean;
  required_tier: PlatformSkillTier;
  last_used_at: string | null;
  state_changed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface PlatformSkillVersion {
  id: string;
  skill_id: string;
  version: number;
  prompt_fragment: string;
  tool_names: string[];
  description: string;
  category: string;
  required_models: string[];
  authored_by: string;
  supporting_files: Record<string, { size: number; mime: string }>;
  lazy_load: boolean;
  high_risk: boolean;
  created_at: string;
}

export interface ListPlatformSkillsParams {
  status?: PlatformSkillStatus;
  category?: string;
  /** Case-insensitive substring over name + description. */
  q?: string;
  /** Offset-based pagination (server-side). */
  offset?: number;
  limit?: number;
}

export interface PlatformSkillList {
  items: PlatformSkill[];
  /** Total rows matching the filter (drives the table's page count). */
  total: number;
}

export interface CreatePlatformSkillBody {
  name: string;
  description?: string;
  category?: string;
  required_tier?: PlatformSkillTier;
}

export interface AddPlatformSkillVersionBody {
  prompt_fragment: string;
  tool_names?: string[];
  description?: string;
  category?: string;
  required_models?: string[];
  authored_by?: string;
}

export interface PatchPlatformSkillBody {
  status?: PlatformSkillStatus;
  pinned?: boolean;
  /** New category label; empty string clears it. */
  category?: string;
}

export interface PlatformSkillVersionList {
  items: PlatformSkillVersion[];
}

/** ``POST /v1/platform/skills/import`` response — the ZIP either created a
 *  new skill (+version) or appended a version to an existing one. ``created``
 *  is ``false`` for an idempotent re-import of identical content. */
/** skill-runtime §5.2 — advisory: can this skill's bundled scripts run in the
 *  Python-only sandbox? ``runnable=false`` (node/browser) → steer to MCP. */
export interface SkillRuntime {
  kind: "knowledge" | "python" | "node" | "browser" | "unknown";
  runnable: boolean;
  hint: string;
}

export interface ImportPlatformSkillResponse {
  skill: PlatformSkill;
  version: PlatformSkillVersion;
  created: boolean;
  runtime?: SkillRuntime;
}

// ── Endpoints ──────────────────────────────────────────────────────────────
//
// All endpoints read the *raw* ``response.data`` (the backend does NOT
// envelope these routes); see the module header. HTTP errors are turned into
// ``ApiError`` by the shared response interceptor.

/** ``GET /v1/platform/skills`` — offset-paginated platform skill list. */
export async function listPlatformSkills(
  params: ListPlatformSkillsParams = {},
): Promise<PlatformSkillList> {
  const { status, category, q, offset, limit } = params;
  const response = await apiClient.get<PlatformSkillList>("/v1/platform/skills", {
    params: { status, category, q: q || undefined, offset, limit },
  });
  return response.data;
}

/** ``GET /v1/platform/skills/{id}`` — single platform skill (404 unknown). */
export async function getPlatformSkill(id: string): Promise<PlatformSkill> {
  const response = await apiClient.get<PlatformSkill>(
    `/v1/platform/skills/${encodeURIComponent(id)}`,
  );
  return response.data;
}

/** ``POST /v1/platform/skills`` — create a platform skill (409 duplicate). */
export async function createPlatformSkill(
  body: CreatePlatformSkillBody,
): Promise<PlatformSkill> {
  const response = await apiClient.post<PlatformSkill>("/v1/platform/skills", body);
  return response.data;
}

/** ``POST /v1/platform/skills/{id}/versions`` — append a version
 *  (400 moderation/threat, 404 unknown skill). */
export async function addPlatformSkillVersion(
  id: string,
  body: AddPlatformSkillVersionBody,
): Promise<PlatformSkillVersion> {
  const response = await apiClient.post<PlatformSkillVersion>(
    `/v1/platform/skills/${encodeURIComponent(id)}/versions`,
    body,
  );
  return response.data;
}

/** ``POST /v1/platform/skills/import`` — multipart ``.skill`` ZIP → a
 *  platform (NULL-tenant) skill + version. Idempotent on content hash
 *  (200 + ``created:false`` for an identical re-import). Mirrors the tenant
 *  ``importSkillZip`` flow. */
export async function importPlatformSkill(
  file: File | Blob,
): Promise<ImportPlatformSkillResponse> {
  const form = new FormData();
  form.append("file", file);
  const response = await apiClient.post<ImportPlatformSkillResponse>(
    "/v1/platform/skills/import",
    form,
    { headers: { "Content-Type": "multipart/form-data" } },
  );
  return response.data;
}

/** One per-pack outcome of the bulk ZIP import (``:import-batch``).
 *  ``skipped`` = content-hash idempotent no-op (already present). */
export interface BulkImportResultRow {
  name: string;
  status: "imported" | "skipped" | "failed";
  version?: number;
  reason?: string;
}

export interface BulkImportResponse {
  results: BulkImportResultRow[];
}

/** ``POST /v1/platform/skills:import-batch`` — outer ZIP of ``.skill`` packs
 *  (the ``:export-all`` layout: ``<name>/<name>.skill`` + ``<name>/meta.json``
 *  category sidecar). Partial success: one bad pack never aborts the rest. */
export async function importPlatformSkillsBatch(
  file: File | Blob,
): Promise<BulkImportResponse> {
  const form = new FormData();
  form.append("file", file);
  const response = await apiClient.post<BulkImportResponse>(
    "/v1/platform/skills:import-batch",
    form,
    { headers: { "Content-Type": "multipart/form-data" } },
  );
  return response.data;
}

/** ``POST /v1/platform/skills/import-from-github`` request body. ``source`` =
 *  ``owner/repo`` / a ``github.com`` URL / a ``skills.sh`` URL (the latter also
 *  supplies ``skill``). ``skill`` picks one skill in a multi-skill repo;
 *  ``ref`` = branch/tag/SHA (default: the repo's default branch). */
export interface ImportFromGithubBody {
  source: string;
  skill?: string;
  ref?: string;
}

/** ``POST /v1/platform/skills/import-from-github`` — fetch a skill from a public
 *  GitHub repo → platform library, reusing the ZIP import pipeline. Same
 *  response shape + content-hash idempotency as :func:`importPlatformSkill`. */
export async function importPlatformSkillFromGithub(
  body: ImportFromGithubBody,
): Promise<ImportPlatformSkillResponse> {
  const response = await apiClient.post<ImportPlatformSkillResponse>(
    "/v1/platform/skills/import-from-github",
    body,
  );
  return response.data;
}

/** ``POST /v1/platform/skills/list-github-skills`` request body — discover what
 *  skills a source contains without importing. */
export interface ListGithubSkillsBody {
  source: string;
  ref?: string;
}

export interface ListGithubSkillsResponse {
  candidates: string[];
}

/** ``POST /v1/platform/skills/list-github-skills`` — list the importable skill
 *  folders in a source so the picker can populate before the operator commits
 *  to an import. A single-skill repo returns one candidate. */
export async function listGithubSkills(
  body: ListGithubSkillsBody,
): Promise<ListGithubSkillsResponse> {
  const response = await apiClient.post<ListGithubSkillsResponse>(
    "/v1/platform/skills/list-github-skills",
    body,
  );
  return response.data;
}

/** ``POST /v1/platform/skills/import-from-github/batch`` request body —
 *  ``skills`` is a list of folder paths/basenames to import in one pass. */
export interface BatchImportFromGithubBody {
  source: string;
  skills: string[];
  ref?: string;
}

/** One per-skill outcome in a batch import. ``status`` is ``created`` (new
 *  version written), ``exists`` (idempotent no-op), or ``failed`` (with a
 *  path-free ``reason``). */
export interface BatchImportResult {
  skill: string;
  status: "created" | "exists" | "failed";
  name?: string;
  version?: number;
  reason?: string;
  runtime?: SkillRuntime;
}

export interface BatchImportFromGithubResponse {
  results: BatchImportResult[];
}

/** ``POST /v1/platform/skills/import-from-github/batch`` — import multiple
 *  skills from one repo. Downloads the archive once; partial success (a failed
 *  skill doesn't abort the batch). */
export async function importPlatformSkillsFromGithubBatch(
  body: BatchImportFromGithubBody,
): Promise<BatchImportFromGithubResponse> {
  const response = await apiClient.post<BatchImportFromGithubResponse>(
    "/v1/platform/skills/import-from-github/batch",
    body,
  );
  return response.data;
}

/** ``PATCH /v1/platform/skills/{id}`` — set status, pinned, and/or category. */
export async function patchPlatformSkill(
  id: string,
  body: PatchPlatformSkillBody,
): Promise<PlatformSkill> {
  const response = await apiClient.patch<PlatformSkill>(
    `/v1/platform/skills/${encodeURIComponent(id)}`,
    body,
  );
  return response.data;
}

/** ``GET /v1/platform/skills/categories`` — distinct categories for the filter
 *  dropdown + the detail-page category picker. */
export async function listPlatformSkillCategories(): Promise<string[]> {
  const response = await apiClient.get<{ categories: string[] }>(
    "/v1/platform/skills/categories",
  );
  return response.data.categories;
}

/** Selector for the "apply to ALL matching" bulk path (across pages). */
export interface BulkPlatformSkillsFilter {
  status?: PlatformSkillStatus;
  category?: string;
  q?: string;
}

/** ``POST /v1/platform/skills/batch`` body — patch (set_status / set_pinned /
 *  set_category) over exactly one selector: ``ids`` (page selection) or
 *  ``filter`` (all matching). ``set_category``: "" clears, a name sets, omitted
 *  leaves the category untouched. */
export interface BulkUpdatePlatformSkillsBody {
  set_status?: PlatformSkillStatus;
  set_pinned?: boolean;
  set_category?: string;
  ids?: string[];
  filter?: BulkPlatformSkillsFilter;
}

/** ``POST /v1/platform/skills/batch`` — bulk lock/unlock/archive/activate.
 *  Returns the affected row count. */
export async function bulkUpdatePlatformSkills(
  body: BulkUpdatePlatformSkillsBody,
): Promise<{ updated: number }> {
  const response = await apiClient.post<{ updated: number }>(
    "/v1/platform/skills/batch",
    body,
  );
  return response.data;
}

/** ``GET /v1/platform/skills/{id}/versions`` — version history. */
export async function listPlatformSkillVersions(
  id: string,
): Promise<PlatformSkillVersionList> {
  const response = await apiClient.get<PlatformSkillVersionList>(
    `/v1/platform/skills/${encodeURIComponent(id)}/versions`,
  );
  return response.data;
}

// ── Supporting files + export (skill-authoring-ia Phase A) ───────────────────
// Mirror the tenant ``/v1/skills`` supporting-file surface so the platform
// admin can iterate a curated skill's bundled scripts/references in-UI. Each
// mutation forks a NEW version server-side (immutable history).

export interface PlatformSupportingFileBody {
  /** Base64-encoded raw bytes. */
  content: string;
  size: number;
  mime: string;
}

export interface PutPlatformSupportingFileBody {
  content: string;
  size: number;
  mime: string;
}

/** Mirror FastAPI ``{path:path}``: keep slashes, encode each segment so
 *  spaces / unicode / reserved chars survive transit. */
function encodeFilePath(filePath: string): string {
  return filePath
    .split("/")
    .map((segment) => encodeURIComponent(segment))
    .join("/");
}

/** ``GET /v1/platform/skills/{id}/versions/{v}/supporting-files/{path}`` —
 *  single-file base64 body (404 unknown skill/version/file, 400 bad path). */
export async function getPlatformSupportingFile(
  id: string,
  version: number,
  filePath: string,
): Promise<PlatformSupportingFileBody> {
  const response = await apiClient.get<PlatformSupportingFileBody>(
    `/v1/platform/skills/${encodeURIComponent(id)}/versions/${version}/supporting-files/${encodeFilePath(filePath)}`,
  );
  return response.data;
}

/** ``PUT …/supporting-files/{path}`` — add/replace a file → new version
 *  (400 threat/bad path, 404 unknown). Returns the new version. */
export async function putPlatformSupportingFile(
  id: string,
  version: number,
  filePath: string,
  body: PutPlatformSupportingFileBody,
): Promise<PlatformSkillVersion> {
  const response = await apiClient.put<PlatformSkillVersion>(
    `/v1/platform/skills/${encodeURIComponent(id)}/versions/${version}/supporting-files/${encodeFilePath(filePath)}`,
    body,
  );
  return response.data;
}

/** ``DELETE …/supporting-files/{path}`` — remove a file → new version. */
export async function deletePlatformSupportingFile(
  id: string,
  version: number,
  filePath: string,
): Promise<PlatformSkillVersion> {
  const response = await apiClient.delete<PlatformSkillVersion>(
    `/v1/platform/skills/${encodeURIComponent(id)}/versions/${version}/supporting-files/${encodeFilePath(filePath)}`,
  );
  return response.data;
}

/** ``GET …/versions/{v}/export`` — download the version as a ``.skill`` ZIP
 *  (includes bundled supporting files). Returns the raw ``Blob``. */
export async function exportPlatformSkillVersion(
  id: string,
  version: number,
): Promise<Blob> {
  const response = await apiClient.get<Blob>(
    `/v1/platform/skills/${encodeURIComponent(id)}/versions/${version}/export`,
    { responseType: "blob" },
  );
  return response.data;
}

/** ``GET /v1/platform/skills:export-all`` — one ZIP holding EVERY platform
 *  skill's latest version (inner packs byte-identical to the per-version
 *  export; ``meta.json`` sidecars carry the skill-row category). Raw ``Blob``
 *  for the blob-anchor download. */
export async function exportAllPlatformSkills(): Promise<Blob> {
  const response = await apiClient.get<Blob>("/v1/platform/skills:export-all", {
    responseType: "blob",
  });
  return response.data;
}

/** ``PUT …/versions/{v}/prompt`` — edit SKILL.md → new version inheriting
 *  all other fields incl. supporting files (skill-authoring-ia Phase D-2). */
export async function putPlatformSkillPrompt(
  id: string,
  version: number,
  promptFragment: string,
): Promise<PlatformSkillVersion> {
  const response = await apiClient.put<PlatformSkillVersion>(
    `/v1/platform/skills/${encodeURIComponent(id)}/versions/${version}/prompt`,
    { prompt_fragment: promptFragment },
  );
  return response.data;
}
