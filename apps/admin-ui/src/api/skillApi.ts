/**
 * Skill API adapter — skill-authoring-ia Phase B.
 *
 * ``SkillDetail`` and its sub-components (``FileEditor`` / ``AddFileModal`` /
 * ``RenameDeleteModals``) used to import the *tenant* ``./skills`` SDK
 * directly, which hard-wired the detail page to ``/v1/skills``. To reuse the
 * same editor for the platform skill library (Phase C), they now take an
 * injected :class:`SkillApi` so the transport (tenant vs platform) is a
 * constructor choice, not a hard import.
 *
 * Two implementations:
 *   - :data:`tenantSkillApi` — thin pass-through to ``./skills`` (`/v1/skills`)
 *   - :data:`platformSkillApi` — wraps ``./platform-skills`` (`/v1/platform/skills`)
 *
 * The platform types (``PlatformSkill`` / ``PlatformSkillVersion``) are
 * structurally a subset of the tenant ``SkillRecord`` / ``SkillVersion``
 * (same field names; platform ``status`` omits ``"stale"``, which is a subset
 * of the tenant union), so the platform impl is assignable without an explicit
 * field-by-field mapping. The base64 codec helpers (``encodeUtf8Base64`` /
 * ``decodeBase64Utf8``) are pure and identical for both, so callers keep
 * importing them from ``./skills`` directly.
 */
import type { TenantScope } from "./client";
import {
  getSkill,
  listSkillVersions,
  patchSkillStatus,
  exportSkillVersion,
  getSupportingFile,
  putSupportingFile,
  deleteSupportingFile,
  renameSupportingFile,
  putSkillPrompt,
  type SkillRecord,
  type SkillVersion,
  type SkillVersionList,
  type SupportingFileBody,
  type PatchSkillStatusBody,
} from "./skills";
import {
  getPlatformSkill,
  listPlatformSkillVersions,
  listPlatformSkillCategories,
  patchPlatformSkill,
  exportPlatformSkillVersion,
  getPlatformSupportingFile,
  putPlatformSupportingFile,
  deletePlatformSupportingFile,
  putPlatformSkillPrompt,
} from "./platform-skills";

export interface SkillApi {
  /** Read calls take an optional ``scope`` (cross-tenant W3 — switched-in
   *  system_admin drill-in). The platform impl ignores it (platform skills
   *  are tenant-less); writes stay scope-less (read-only drill-in). */
  getSkill(id: string, scope?: TenantScope): Promise<SkillRecord>;
  listVersions(id: string, scope?: TenantScope): Promise<SkillVersionList>;
  patchStatus(id: string, body: PatchSkillStatusBody): Promise<SkillRecord>;
  exportVersion(id: string, version: number, scope?: TenantScope): Promise<Blob>;
  getSupportingFile(
    id: string,
    version: number,
    filePath: string,
    scope?: TenantScope,
  ): Promise<SupportingFileBody>;
  putSupportingFile(
    id: string,
    version: number,
    filePath: string,
    body: SupportingFileBody,
  ): Promise<SkillVersion>;
  deleteSupportingFile(
    id: string,
    version: number,
    filePath: string,
  ): Promise<SkillVersion>;
  renameSupportingFile(
    id: string,
    version: number,
    oldPath: string,
    newPath: string,
    body: SupportingFileBody,
  ): Promise<SkillVersion>;
  /** Edit SKILL.md (prompt fragment) → new version inheriting other fields. */
  putPrompt(
    id: string,
    version: number,
    promptFragment: string,
  ): Promise<SkillVersion>;
  /** Re-label the skill's category (platform library only; empty clears it).
   *  Undefined on the tenant SDK — the detail page hides the editor then. */
  patchCategory?(id: string, category: string): Promise<SkillRecord>;
  /** Distinct categories for the editor's picker (platform only). */
  listCategories?(): Promise<string[]>;
}

/** Tenant ``/v1/skills`` — thin pass-through. */
export const tenantSkillApi: SkillApi = {
  getSkill,
  listVersions: listSkillVersions,
  patchStatus: patchSkillStatus,
  exportVersion: exportSkillVersion,
  getSupportingFile,
  putSupportingFile,
  deleteSupportingFile,
  renameSupportingFile,
  putPrompt: putSkillPrompt,
};

/** Platform ``/v1/platform/skills`` — structurally-compatible wrapper. The
 *  platform SDK has no single ``rename`` call (server has no rename route), so
 *  it is composed put-new + delete-old, mirroring the tenant SDK's helper
 *  (put first so a failure leaves the original intact). */
export const platformSkillApi: SkillApi = {
  getSkill: getPlatformSkill,
  listVersions: listPlatformSkillVersions,
  patchStatus: patchPlatformSkill,
  exportVersion: exportPlatformSkillVersion,
  getSupportingFile: getPlatformSupportingFile,
  putSupportingFile: putPlatformSupportingFile,
  deleteSupportingFile: deletePlatformSupportingFile,
  renameSupportingFile: async (id, version, oldPath, newPath, body) => {
    const afterPut = await putPlatformSupportingFile(id, version, newPath, {
      content: body.content,
      size: body.size,
      mime: body.mime,
    });
    return deletePlatformSupportingFile(id, afterPut.version, oldPath);
  },
  putPrompt: putPlatformSkillPrompt,
  patchCategory: (id, category) => patchPlatformSkill(id, { category }),
  listCategories: listPlatformSkillCategories,
};
