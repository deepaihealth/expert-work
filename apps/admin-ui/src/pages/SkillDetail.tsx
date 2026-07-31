/**
 * Skill detail page — Stream H.4 PR 5 + Capability Uplift Sprint #3 PR C.
 *
 * Three sections stacked vertically:
 *
 *   1. **Hero + status select.** Title, status, latest-version tag, and
 *      🔒 high-risk + Lazy badges (Mini-ADRs U-15 / U-24). Status change
 *      goes through PATCH; if the latest version is high-risk and the
 *      caller is not admin / system_admin, the "Active" option is
 *      disabled with an explanatory tooltip.
 *   2. **Metadata panel.** Compact 2-column descriptor of the currently
 *      selected version's static fields. Lives in
 *      ``./skill_detail/MetadataPanel.tsx``.
 *   3. **Dual-pane editor.** ``FileTree`` on the left (260 px),
 *      ``FileEditor`` on the right. Mutation produces a new SkillVersion
 *      server-side; the page navigates the version picker to the new
 *      version after each successful PUT / DELETE / rename.
 *
 * Version picker + Export ZIP live in the small bar between the
 * metadata panel and the editor — every version is independently
 * inspectable + exportable for forensic rollback. The "Import ZIP" CTA
 * stays on ``SkillsList.tsx`` (creates a new top-level version).
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  Select,
  Skeleton,
  Space,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import {
  Download,
  FileCode2,
  Pin,
  PinOff,
  ShieldAlert,
  Sparkles,
  Zap,
} from "lucide-react";
import { useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { PageHeader } from "../components/PageHeader";
import {
  type SkillRecord,
  type SkillStatus,
  type SkillVersion,
} from "../api/skills";
import { type SkillApi, tenantSkillApi } from "../api/skillApi";
import { useAuth } from "../auth/AuthContext";
import { concreteTenantScope, useTenantScope } from "../tenant/TenantScopeContext";
import { AddFileModal } from "./skill_detail/AddFileModal";
import { FileEditor } from "./skill_detail/FileEditor";
import { EvalEvidencePanel } from "./skill_detail/EvalEvidencePanel";
import { GovernancePanel } from "./skill_detail/GovernancePanel";
import { LineagePanel } from "./skill_detail/LineagePanel";
import { FileTree, SKILL_MD_PATH } from "./skill_detail/FileTree";
import { MetadataPanel } from "./skill_detail/MetadataPanel";
import {
  DeleteConfirmModal,
  RenameModal,
} from "./skill_detail/RenameDeleteModals";

const { Text } = Typography;

const STATUS_OPTIONS: SkillStatus[] = ["draft", "active", "stale", "archived"];
// Platform skills have no Curator → no "stale" lifecycle state.
const PLATFORM_STATUS_OPTIONS: SkillStatus[] = ["draft", "active", "archived"];

const STATUS_COLOR: Record<SkillStatus, string> = {
  draft: "default",
  active: "success",
  stale: "default",
  archived: "warning",
};

const TIER_COLOR: Record<string, string> = {
  free: "default",
  pro: "blue",
  enterprise: "gold",
};

/** Tests assert against this set; keep in sync with `Role` enum on the
 *  backend (per [memory:cross-tenant-admin]). */
const ADMIN_ROLES = new Set(["admin", "system_admin"]);

interface SkillDetailProps {
  /** Transport — tenant ``/v1/skills`` (default) or platform
   *  ``/v1/platform/skills``. See ``api/skillApi.ts``. */
  api?: SkillApi;
  /** ``"platform"`` hides the tenant-flywheel panels (governance / lineage /
   *  eval evidence), drops the Curator-only "stale" status option, and shows
   *  the ``required_tier`` entitlement tag. (skill-authoring-ia Phase C.) */
  variant?: "tenant" | "platform";
  /** Back-link target; defaults to the tenant skills list. */
  backTo?: { label: string; to: string };
}

export function SkillDetail({
  api = tenantSkillApi,
  variant = "tenant",
  backTo,
}: SkillDetailProps = {}) {
  const { t } = useTranslation();
  const { message, modal } = App.useApp();
  const { skillId } = useParams<{ skillId: string }>();
  const { identity } = useAuth();
  // Cross-tenant W3 — detail reads take a concrete UUID only ("*" 400s).
  // The platform facade ignores the scope (platform skills are tenant-less).
  const { apiTenantScope } = useTenantScope();
  const readScope = concreteTenantScope(apiTenantScope);
  const isPlatform = variant === "platform";
  const statusOptions = isPlatform ? PLATFORM_STATUS_OPTIONS : STATUS_OPTIONS;
  const backLink = backTo ?? { label: t("nav.skills"), to: "/skills" };

  const [skill, setSkill] = useState<SkillRecord | null>(null);
  const [versions, setVersions] = useState<SkillVersion[]>([]);
  const [selectedVersionNumber, setSelectedVersionNumber] = useState<number | null>(null);
  const [selectedPath, setSelectedPath] = useState<string | null>(SKILL_MD_PATH);
  const [editorDirty, setEditorDirty] = useState(false);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusSubmitting, setStatusSubmitting] = useState(false);
  // Category editor (platform library only — the tenant SDK has no
  // patchCategory). Options feed the AutoComplete picker.
  const [categoryOptions, setCategoryOptions] = useState<string[]>([]);

  const [addOpen, setAddOpen] = useState(false);
  const [renamePath, setRenamePath] = useState<string | null>(null);
  const [deletePath, setDeletePath] = useState<string | null>(null);

  const isAdmin = useMemo(() => {
    if (identity === null) return false;
    if (identity.isSystemAdmin) return true;
    return identity.roles.some((r) => ADMIN_ROLES.has(r));
  }, [identity]);

  const refresh = useCallback(async () => {
    if (!skillId) return;
    setLoading(true);
    setError(null);
    try {
      const [skillResult, versionsResult] = await Promise.all([
        api.getSkill(skillId, readScope),
        api.listVersions(skillId, readScope),
      ]);
      setSkill(skillResult);
      setVersions(versionsResult.items);
      setSelectedVersionNumber((prev) => {
        if (prev !== null && versionsResult.items.some((v) => v.version === prev)) {
          return prev;
        }
        // Default to latest (last in list since backend returns sorted).
        const latest = versionsResult.items[versionsResult.items.length - 1];
        return latest ? latest.version : null;
      });
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `${err.code}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "unknown error";
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [skillId, api, readScope]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Category picker options (platform only). Best-effort — a failure just
  // leaves the AutoComplete free-text.
  useEffect(() => {
    if (!api.listCategories) return;
    void api
      .listCategories()
      .then(setCategoryOptions)
      .catch(() => setCategoryOptions([]));
  }, [api]);

  const onSaveCategory = useCallback(
    async (category: string) => {
      if (skill === null || !api.patchCategory) return;
      try {
        const updated = await api.patchCategory(skill.id, category);
        setSkill(updated);
        message.success(t("platform_skills.detail_category_saved"));
        if (api.listCategories) {
          api.listCategories().then(setCategoryOptions).catch(() => {});
        }
      } catch (err) {
        const msg =
          err instanceof ApiError
            ? `${err.code}: ${err.message}`
            : err instanceof Error
              ? err.message
              : "failed";
        message.error(msg);
      }
    },
    [skill, api, message, t],
  );

  const selectedVersion = useMemo(
    () => versions.find((v) => v.version === selectedVersionNumber) ?? null,
    [versions, selectedVersionNumber],
  );

  const isLatestHighRisk = useMemo(() => {
    if (skill === null) return false;
    const latest = versions.find((v) => v.version === skill.latest_version);
    return latest?.high_risk ?? false;
  }, [skill, versions]);

  // Status change with U-24 admin gate.
  const onChangeStatus = useCallback(
    async (next: SkillStatus) => {
      if (skill === null) return;
      setStatusSubmitting(true);
      try {
        const updated = await api.patchStatus(skill.id, { status: next });
        setSkill(updated);
        message.success(t("skills.status_changed", { status: t(`skills.status_${next}`) }));
      } catch (err) {
        const msg =
          err instanceof ApiError
            ? `${err.code}: ${err.message}`
            : err instanceof Error
              ? err.message
              : "failed";
        message.error(msg);
      } finally {
        setStatusSubmitting(false);
      }
    },
    [skill, message, t, api],
  );

  // Sprint #4 (Mini-ADR U-30) — pin / unpin. Same admin gate as
  // status active for high-risk rows: the backend returns 403 if a
  // non-admin tries to pin a high-risk skill, so the button is
  // disabled client-side for the same condition (avoids the failure
  // round-trip). Low-risk pins are open to any caller who can already
  // see the page.
  const onTogglePin = useCallback(async () => {
    if (skill === null) return;
    const next = !skill.pinned;
    setStatusSubmitting(true);
    try {
      const updated = await api.patchStatus(skill.id, { pinned: next });
      setSkill(updated);
      message.success(
        next ? t("skills.pinned_toast") : t("skills.unpinned_toast"),
      );
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `${err.code}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "failed";
      message.error(msg);
    } finally {
      setStatusSubmitting(false);
    }
  }, [skill, message, t, api]);

  const onExport = useCallback(async () => {
    if (skill === null || selectedVersion === null) return;
    try {
      const blob = await api.exportVersion(skill.id, selectedVersion.version, readScope);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${skill.name}-v${selectedVersion.version}.skill.zip`;
      document.body.appendChild(anchor);
      anchor.click();
      document.body.removeChild(anchor);
      URL.revokeObjectURL(url);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "export failed";
      message.error(msg);
    }
  }, [skill, selectedVersion, message, api, readScope]);

  const selectFileSafely = useCallback(
    (path: string) => {
      if (!editorDirty) {
        setSelectedPath(path);
        return;
      }
      modal.confirm({
        title: t("skills.detail_unsaved_changes_warning"),
        okText: t("skills.file_action_cancel"),
        cancelText: t("skills.file_action_save"),
        onOk: () => {
          setSelectedPath(path);
        },
      });
    },
    [editorDirty, modal, t],
  );

  // Used by AddFile / Save / Delete / Rename — after any mutation
  // we refetch versions, jump the picker to the new version, and
  // (for adds / renames) select the new path.
  const adoptNewVersion = useCallback(
    async (newVersion: SkillVersion, newPath?: string) => {
      await refresh();
      setSelectedVersionNumber(newVersion.version);
      if (newPath !== undefined) {
        setSelectedPath(newPath);
      }
    },
    [refresh],
  );

  if (loading) {
    return <Skeleton active paragraph={{ rows: 6 }} />;
  }

  if (error !== null || skill === null) {
    return (
      <Alert
        type="error"
        showIcon
        message={t("skills.failed_to_load")}
        description={error ?? "skill not found"}
        data-testid="skill-detail-error"
      />
    );
  }

  const supportingPaths =
    selectedVersion === null ? [] : Object.keys(selectedVersion.supporting_files).sort();

  return (
    <div data-testid="skill-detail-root">
      <PageHeader
        icon={<FileCode2 size={18} strokeWidth={1.5} />}
        title={skill.name}
        backTo={backLink}
        subtitle={
          <Space size={6} wrap>
            <Tag color={STATUS_COLOR[skill.status]}>{t(`skills.status_${skill.status}`)}</Tag>
            {isPlatform && skill.required_tier && (
              <Tag color={TIER_COLOR[skill.required_tier] ?? "default"}>
                {t(`platform_skills.tier_${skill.required_tier}`)}
              </Tag>
            )}
            {skill.latest_version !== null && skill.latest_version > 0 && (
              <Tooltip title={t("skills.latest_version_hint")}>
                <Tag bordered={false}>v{skill.latest_version}</Tag>
              </Tooltip>
            )}
            {/* Hero-level high-risk badge sourced from the latest version
                so the status select gate is visually obvious without
                expanding the version picker. */}
            {isLatestHighRisk && (
              <Tooltip title={t("skills.detail_high_risk_tooltip")}>
                <Tag
                  bordered={false}
                  color="error"
                  icon={<ShieldAlert size={11} strokeWidth={1.75} style={{ marginRight: 4 }} />}
                  data-testid="skill-hero-high-risk-badge"
                >
                  🔒 {t("skills.detail_high_risk_badge")}
                </Tag>
              </Tooltip>
            )}
          </Space>
        }
        actions={
          <Space size={6}>
            {/* Sprint #4 (Mini-ADR U-30) — pin button. Disabled for
                non-admin callers on high-risk skills (mirrors the
                backend role check, avoids the 403 round-trip).
                Pinned state shows the filled icon + brand color so
                the visual gate is obvious. */}
            <Tooltip
              title={
                skill.pinned
                  ? t("skills.pin_tooltip_on")
                  : isLatestHighRisk && !isAdmin
                    ? t("skills.detail_admin_required_tooltip")
                    : t("skills.pin_tooltip_off")
              }
            >
              <Button
                size="small"
                icon={
                  skill.pinned ? (
                    <Pin
                      size={13}
                      strokeWidth={2}
                      style={{ color: "var(--ew-color-brand-500)" }}
                    />
                  ) : (
                    <PinOff size={13} strokeWidth={1.75} />
                  )
                }
                onClick={onTogglePin}
                disabled={
                  statusSubmitting || (!skill.pinned && isLatestHighRisk && !isAdmin)
                }
                data-testid="skill-pin-button"
              >
                {skill.pinned ? t("skills.unpin") : t("skills.pin")}
              </Button>
            </Tooltip>
            <Select<SkillStatus>
              value={skill.status}
              onChange={(v) => onChangeStatus(v)}
              style={{ width: 160 }}
              loading={statusSubmitting}
              disabled={statusSubmitting}
              aria-label={t("skills.change_status")}
              data-testid="skill-status-select"
              options={statusOptions.map((s) => {
                const isActiveBlocked = s === "active" && isLatestHighRisk && !isAdmin;
                const label = t(`skills.status_${s}`);
                return {
                  value: s,
                  label: isActiveBlocked ? (
                    <Tooltip title={t("skills.detail_admin_required_tooltip")}>
                      <span style={{ color: "var(--ew-text-tertiary)" }}>
                        {label} 🔒
                      </span>
                    </Tooltip>
                  ) : (
                    label
                  ),
                  disabled: isActiveBlocked,
                };
              })}
            />
          </Space>
        }
      />

      {selectedVersion !== null && (
        <MetadataPanel
          skill={skill}
          version={selectedVersion}
          categoryOptions={api.patchCategory ? categoryOptions : undefined}
          onSaveCategory={api.patchCategory ? onSaveCategory : undefined}
        />
      )}

      {/* Tenant-flywheel panels — platform skills are human-curated, not
          agent-evolved, so governance/lineage/eval evidence don't apply
          (skill-authoring-ia Phase C). */}
      {!isPlatform && (
        <>
          <GovernancePanel skill={skill} isAdmin={isAdmin} onChanged={refresh} />
          <EvalEvidencePanel skillId={skill.id} />
          <LineagePanel skillId={skill.id} />
        </>
      )}

      <Card size="small" style={{ marginBottom: 16 }} data-testid="skill-version-bar">
        <Space size={12} wrap>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t("skills.detail_version_picker_label")}
          </Text>
          <Select<number>
            value={selectedVersionNumber ?? undefined}
            onChange={(v) => {
              if (editorDirty) {
                modal.confirm({
                  title: t("skills.detail_unsaved_changes_warning"),
                  okText: t("skills.file_action_cancel"),
                  cancelText: t("skills.file_action_save"),
                  onOk: () => {
                    setSelectedVersionNumber(v);
                    setSelectedPath(SKILL_MD_PATH);
                    setEditorDirty(false);
                  },
                });
              } else {
                setSelectedVersionNumber(v);
                setSelectedPath(SKILL_MD_PATH);
              }
            }}
            style={{ minWidth: 220 }}
            aria-label={t("skills.detail_version_picker_label")}
            data-testid="skill-version-picker"
            options={versions.map((v) => ({
              value: v.version,
              label: (
                <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                  <Text code style={{ fontSize: 12 }}>
                    v{v.version}
                  </Text>
                  {v.version === skill.latest_version && (
                    <Text type="secondary" style={{ fontSize: 11 }}>
                      {t("skills.detail_active_version_marker")}
                    </Text>
                  )}
                  {v.lazy_load && (
                    <Sparkles
                      size={11}
                      strokeWidth={1.75}
                      style={{ color: "var(--ew-color-brand-500)" }}
                    />
                  )}
                  {v.high_risk && (
                    <ShieldAlert
                      size={11}
                      strokeWidth={1.75}
                      style={{ color: "var(--ew-status-danger-fg)" }}
                    />
                  )}
                </span>
              ),
            }))}
          />
          {selectedVersion === null && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t("skills.no_versions")}
            </Text>
          )}
          <span style={{ flex: 1 }} />
          {selectedVersion !== null && (
            <Button
              size="small"
              icon={<Download size={13} strokeWidth={1.75} />}
              onClick={onExport}
              data-testid={`skill-export-${selectedVersion.version}`}
            >
              {t("skills.export_zip")}
            </Button>
          )}
        </Space>
      </Card>

      {selectedVersion !== null && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "260px 1fr",
            gap: 16,
            alignItems: "start",
          }}
          data-testid="skill-dual-pane"
        >
          <Card
            size="small"
            title={
              <Space size={6}>
                <Text strong style={{ fontSize: 13 }}>
                  {t("skills.detail_files_title")}
                </Text>
                {selectedVersion.lazy_load && (
                  <Tooltip title={t("skills.detail_lazy_tooltip")}>
                    <Tag
                      bordered={false}
                      color="blue"
                      icon={<Sparkles size={10} strokeWidth={1.75} style={{ marginRight: 4 }} />}
                    >
                      {t("skills.detail_lazy_badge")}
                    </Tag>
                  </Tooltip>
                )}
                {!selectedVersion.lazy_load && (
                  <Tooltip title={t("skills.detail_eager_tooltip")}>
                    <Tag
                      bordered={false}
                      icon={<Zap size={10} strokeWidth={1.75} style={{ marginRight: 4 }} />}
                    >
                      Eager
                    </Tag>
                  </Tooltip>
                )}
              </Space>
            }
            data-testid="skill-tree-card"
          >
            <FileTree
              paths={supportingPaths}
              selected={selectedPath}
              onSelect={selectFileSafely}
              onAddFile={() => setAddOpen(true)}
            />
          </Card>

          <FileEditor
            api={api}
            skillId={skill.id}
            version={selectedVersion}
            selectedPath={selectedPath}
            onDirtyChange={setEditorDirty}
            onSaved={(v) => void adoptNewVersion(v)}
            onRequestDelete={(p) => setDeletePath(p)}
            onRequestRename={(p) => setRenamePath(p)}
          />
        </div>
      )}

      {selectedVersion !== null && (
        <>
          <AddFileModal
            api={api}
            open={addOpen}
            skillId={skill.id}
            versionNumber={selectedVersion.version}
            onClose={() => setAddOpen(false)}
            onAdded={(v, p) => void adoptNewVersion(v, p)}
          />
          {renamePath !== null && (
            <RenameModal
              api={api}
              open
              skillId={skill.id}
              versionNumber={selectedVersion.version}
              oldPath={renamePath}
              onClose={() => setRenamePath(null)}
              onRenamed={(v, p) => void adoptNewVersion(v, p)}
            />
          )}
          {deletePath !== null && (
            <DeleteConfirmModal
              api={api}
              open
              skillId={skill.id}
              versionNumber={selectedVersion.version}
              path={deletePath}
              onClose={() => setDeletePath(null)}
              onDeleted={(v) => {
                setSelectedPath(SKILL_MD_PATH);
                void adoptNewVersion(v);
              }}
            />
          )}
        </>
      )}
    </div>
  );
}
