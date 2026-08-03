/**
 * Settings — Platform Skills page (Stream X, system_admin only).
 *
 * Platform-level (NOT tenant-scoped) management of the curated reusable
 * skill catalog. system_admin only (mirrors the backend ``is_system_admin``
 * gate); non-admins see a notice. Lists platform skills with name /
 * category / required-tier badge / status badge / latest-version badge /
 * pin marker, plus a "New skill" action, a create drawer, and a per-row
 * Manage drawer for the version + lifecycle controls.
 *
 * No DELETE endpoint exists — retiring a platform skill means setting
 * ``status=archived`` from the Manage drawer.
 *
 * Mirrors ``SettingsMcpCatalog`` gating + layout (PageHeader + admin gate
 * + antd Table + ``ApiError`` → ``${code}: ${message}`` toasts).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Key } from "react";
import {
  Alert,
  App,
  Button,
  Checkbox,
  Dropdown,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { TableColumnsType } from "antd";
import { GitBranch, Pin, RefreshCw, Sparkles, Upload } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { PageHeader } from "../components/PageHeader";
import { FieldHelp } from "../components/FieldHelp";
import {
  bulkUpdatePlatformSkills,
  exportPlatformSkillVersion,
  importPlatformSkill,
  importPlatformSkillFromGithub,
  importPlatformSkillsFromGithubBatch,
  listGithubSkills,
  listPlatformSkills,
  listPlatformSkillCategories,
  patchPlatformSkill,
  type BatchImportResult,
  type BulkUpdatePlatformSkillsBody,
  type PlatformSkill,
  type PlatformSkillStatus,
  type PlatformSkillTier,
  type SkillRuntime,
} from "../api/platform-skills";
import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";

const { Text } = Typography;

const TIER_COLOR: Record<PlatformSkillTier, string> = {
  free: "default",
  pro: "blue",
  enterprise: "gold",
};

const STATUS_COLOR: Record<PlatformSkillStatus, string> = {
  draft: "default",
  active: "success",
  archived: "warning",
};

const GH_RESULT_COLOR: Record<BatchImportResult["status"], string> = {
  created: "success",
  exists: "default",
  failed: "error",
};

/** Sentinel menu key for the batch "set category" dropdown's clear item —
 *  distinguishes "clear the category" (posts ``set_category: ""``) from any
 *  real category name (which may itself be an empty-looking string in
 *  theory, so a dedicated key avoids ambiguity). */
const CLEAR_CATEGORY_KEY = "__ps_clear_category__";

export function SettingsPlatformSkills() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const auth = useAuth();
  const isSystemAdmin = auth.identity?.isSystemAdmin ?? false;

  const [rows, setRows] = useState<PlatformSkill[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();

  // Server-side pagination + search + status filter. The list is fetched per
  // page (offset/limit) so a >50-skill library is fully reachable; ``q`` is
  // debounced (300ms) before it drives a refetch.
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [q, setQ] = useState("");
  const [qDebounced, setQDebounced] = useState("");
  const [statusFilter, setStatusFilter] = useState<PlatformSkillStatus | undefined>(
    undefined,
  );
  const [categoryFilter, setCategoryFilter] = useState<string | undefined>(undefined);
  const [categories, setCategories] = useState<string[]>([]);

  useEffect(() => {
    const handle = setTimeout(() => setQDebounced(q), 300);
    return () => clearTimeout(handle);
  }, [q]);

  // Changing the search text or a filter resets to page 1 (the old offset
  // would otherwise overshoot the narrowed result set).
  useEffect(() => {
    setPage(1);
  }, [qDebounced, statusFilter, categoryFilter]);

  // GitHub import modal (方案 A).
  const [ghOpen, setGhOpen] = useState(false);
  const [ghSource, setGhSource] = useState("");
  const [ghSkill, setGhSkill] = useState("");
  const [ghRef, setGhRef] = useState("");
  const [ghBusy, setGhBusy] = useState(false);
  // Auto-populated (debounced) as soon as a source is entered, via the
  // list-github-skills probe — the operator picks from the multi-select without
  // an import-to-discover round-trip. A single-skill repo lists one candidate
  // (pre-selected). ``ghListing`` drives the picker's loading state; the ref
  // discards stale responses when the source changes mid-flight.
  const [ghCandidates, setGhCandidates] = useState<string[]>([]);
  const [ghSelected, setGhSelected] = useState<string[]>([]);
  const [ghListing, setGhListing] = useState(false);
  const ghListReqId = useRef(0);
  // Per-skill outcomes of the last batch import (shown in the modal).
  const [ghResults, setGhResults] = useState<BatchImportResult[] | null>(null);

  const errText = useCallback(
    (err: unknown): string =>
      err instanceof ApiError
        ? `${err.code}: ${err.message}`
        : err instanceof Error
          ? err.message
          : "unknown error",
    [],
  );

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listPlatformSkills({
        q: qDebounced || undefined,
        status: statusFilter,
        category: categoryFilter,
        offset: (page - 1) * pageSize,
        limit: pageSize,
      });
      setRows(result.items);
      setTotal(result.total);
    } catch (err) {
      setError(err instanceof Error ? err.message : "unknown error");
    } finally {
      setLoading(false);
    }
  }, [qDebounced, statusFilter, categoryFilter, page, pageSize]);

  useEffect(() => {
    if (isSystemAdmin) {
      void refresh();
    }
  }, [isSystemAdmin, refresh]);

  // Load the distinct category list once (admin only) to feed the filter
  // dropdown; refreshed after any import so a newly-seen category shows up.
  const loadCategories = useCallback(async () => {
    try {
      setCategories(await listPlatformSkillCategories());
    } catch {
      setCategories([]); // non-fatal — the filter just shows "All".
    }
  }, []);

  useEffect(() => {
    if (isSystemAdmin) {
      void loadCategories();
    }
  }, [isSystemAdmin, loadCategories]);

  // skill-runtime §5.2 — a node/browser skill still imports, but its bundled
  // scripts won't run in the Python-only sandbox; warn (longer duration) so the
  // operator knows to reach for an MCP server instead.
  const warnIfNotRunnable = useCallback(
    (runtime?: SkillRuntime) => {
      if (runtime && !runtime.runnable) {
        message.warning(runtime.hint, 8);
      }
    },
    [message],
  );

  const onImportClick = useCallback(() => fileInputRef.current?.click(), []);

  const onImportFile = useCallback(
    async (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (!file) return;
      try {
        const result = await importPlatformSkill(file);
        message.success(
          result.created
            ? t("platform_skills.imported", {
                name: result.skill.name,
                version: result.version.version,
              })
            : t("platform_skills.import_noop", { name: result.skill.name }),
        );
        warnIfNotRunnable(result.runtime);
        void refresh();
      } catch (err) {
        message.error(errText(err));
      } finally {
        if (fileInputRef.current) fileInputRef.current.value = "";
      }
    },
    [errText, message, refresh, t, warnIfNotRunnable],
  );

  const resetGhForm = useCallback(() => {
    setGhSource("");
    setGhSkill("");
    setGhRef("");
    setGhCandidates([]);
    setGhSelected([]);
    setGhResults(null);
    ghListReqId.current += 1; // cancel any in-flight listing
  }, []);

  // Bug fix — list the source's skills as soon as it's entered (debounced), so
  // the picker is populated without clicking "import" first. A single candidate
  // is pre-selected; failure falls back to the manual skill-path input.
  useEffect(() => {
    const source = ghSource.trim();
    if (!ghOpen || source.length === 0) {
      setGhListing(false);
      return;
    }
    const reqId = (ghListReqId.current += 1);
    setGhListing(true);
    const handle = setTimeout(() => {
      void (async () => {
        try {
          const { candidates } = await listGithubSkills({
            source,
            ref: ghRef.trim() || undefined,
          });
          if (reqId !== ghListReqId.current) return; // stale — a newer source won
          setGhCandidates(candidates);
          setGhSelected(candidates.length === 1 ? candidates : []);
          setGhResults(null);
        } catch {
          if (reqId !== ghListReqId.current) return;
          // Listing failed (bad source / private repo) → fall back to the
          // manual skill-path input; the import attempt surfaces the real error.
          setGhCandidates([]);
        } finally {
          if (reqId === ghListReqId.current) setGhListing(false);
        }
      })();
    }, 500);
    return () => clearTimeout(handle);
  }, [ghOpen, ghSource, ghRef]);

  // Batch path: a multi-skill repo's candidates are shown, the operator picks a
  // subset, and we import them in one request (partial success).
  const onGithubBatchImport = useCallback(async () => {
    const source = ghSource.trim();
    if (!source || ghSelected.length === 0) return;
    setGhBusy(true);
    try {
      const { results } = await importPlatformSkillsFromGithubBatch({
        source,
        skills: ghSelected,
        ref: ghRef.trim() || undefined,
      });
      const ok = results.filter((r) => r.status !== "failed").length;
      const failed = results.length - ok;
      message.success(t("platform_skills.github_batch_done", { ok, failed }));
      void refresh();
      if (failed === 0) {
        // Bug fix — auto-close on full success. On partial failure keep the
        // modal open + show per-skill results so the operator can retry.
        setGhOpen(false);
        resetGhForm();
      } else {
        setGhResults(results);
      }
    } catch (err) {
      message.error(errText(err));
    } finally {
      setGhBusy(false);
    }
  }, [errText, ghRef, ghSelected, ghSource, message, refresh, resetGhForm, t]);

  const onGithubImport = useCallback(async () => {
    const source = ghSource.trim();
    if (!source) return;
    // Once candidates are listed, the picker is multi-select → go batch.
    if (ghCandidates.length > 0) {
      void onGithubBatchImport();
      return;
    }
    setGhBusy(true);
    try {
      const result = await importPlatformSkillFromGithub({
        source,
        skill: ghSkill.trim() || undefined,
        ref: ghRef.trim() || undefined,
      });
      message.success(
        result.created
          ? t("platform_skills.imported", {
              name: result.skill.name,
              version: result.version.version,
            })
          : t("platform_skills.import_noop", { name: result.skill.name }),
      );
      warnIfNotRunnable(result.runtime);
      setGhOpen(false);
      resetGhForm();
      void refresh();
    } catch (err) {
      // Multi-skill repo → the backend returns SKILL_AMBIGUOUS + a candidate
      // list. Render it as a multi-select picker (keep the modal open).
      const candidates =
        err instanceof ApiError && err.code === "SKILL_AMBIGUOUS"
          ? err.details?.candidates
          : undefined;
      if (Array.isArray(candidates)) {
        setGhCandidates(candidates as string[]);
        setGhSelected([]);
        setGhResults(null);
      } else {
        message.error(errText(err));
      }
    } finally {
      setGhBusy(false);
    }
  }, [
    errText,
    ghCandidates.length,
    ghRef,
    ghSkill,
    ghSource,
    message,
    onGithubBatchImport,
    refresh,
    resetGhForm,
    t,
    warnIfNotRunnable,
  ]);

  // Phase C: "Manage" opens the full detail page (version editor + lifecycle
  // + supporting files), replacing the old in-place drawer.
  const openManage = useCallback(
    (row: PlatformSkill) => navigate(`/settings/platform-skills/${row.id}`),
    [navigate],
  );

  const onPinToggle = useCallback(
    async (row: PlatformSkill) => {
      try {
        await patchPlatformSkill(row.id, { pinned: !row.pinned });
        void refresh();
      } catch (err) {
        message.error(errText(err));
      }
    },
    [errText, message, refresh],
  );

  // W4 D1 — row-level export of the latest version as a ``.skill`` ZIP
  // (mirrors the SkillsList inline export; blob-anchor download pattern
  // from the detail page's onExport).
  const onExportRow = useCallback(
    async (row: PlatformSkill) => {
      if (row.latest_version === null) return;
      try {
        const blob = await exportPlatformSkillVersion(row.id, row.latest_version);
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = `${row.name}-v${row.latest_version}.skill`;
        document.body.appendChild(anchor);
        anchor.click();
        document.body.removeChild(anchor);
        URL.revokeObjectURL(url);
      } catch (err) {
        message.error(errText(err));
      }
    },
    [errText, message],
  );

  // Bulk actions over the table's row selection — lock/unlock + archive/activate
  // via the server-side ``POST /v1/platform/skills/batch`` endpoint (one atomic
  // UPDATE, returns the affected count). Two scopes:
  //   • default — the rows selected on this page (``ids``);
  //   • "apply to all N matching" — everything matching the current
  //     search/status filter across pages (``filter``), surfaced as a checkbox
  //     only when a filter is active AND the match set spills past this page.
  const [selectedRowKeys, setSelectedRowKeys] = useState<Key[]>([]);
  const [batchBusy, setBatchBusy] = useState(false);
  const [applyToAllMatching, setApplyToAllMatching] = useState(false);

  const filterActive = qDebounced.length > 0 || statusFilter !== undefined;
  const canApplyToAll = filterActive && total > rows.length;
  const scopeIsAll = canApplyToAll && applyToAllMatching;

  const runBatch = useCallback(
    async (
      patch: Pick<BulkUpdatePlatformSkillsBody, "set_status" | "set_pinned" | "set_category">,
    ) => {
      const ids = selectedRowKeys.map(String);
      const useFilter = canApplyToAll && applyToAllMatching;
      if (!useFilter && ids.length === 0) return;
      setBatchBusy(true);
      try {
        const body: BulkUpdatePlatformSkillsBody = useFilter
          ? {
              ...patch,
              filter: { q: qDebounced || undefined, status: statusFilter },
            }
          : { ...patch, ids };
        const { updated } = await bulkUpdatePlatformSkills(body);
        message.success(t("platform_skills.batch_done", { ok: updated }));
        setSelectedRowKeys([]);
        setApplyToAllMatching(false);
        void refresh();
      } catch (err) {
        message.error(errText(err));
      } finally {
        setBatchBusy(false);
      }
    },
    [
      selectedRowKeys,
      canApplyToAll,
      applyToAllMatching,
      qDebounced,
      statusFilter,
      message,
      refresh,
      errText,
      t,
    ],
  );

  const columns: TableColumnsType<PlatformSkill> = useMemo(
    () => [
      {
        title: t("platform_skills.col_name"),
        key: "name",
        render: (_v, row) => (
          <Space size={6}>
            {row.pinned && (
              <Tooltip title={t("platform_skills.pinned")}>
                <Pin
                  size={12}
                  strokeWidth={2}
                  style={{ color: "var(--ew-color-brand-500)" }}
                  data-testid={`ps-pin-icon-${row.id}`}
                />
              </Tooltip>
            )}
            <Text strong>{row.name}</Text>
          </Space>
        ),
      },
      {
        title: t("platform_skills.col_description"),
        dataIndex: "description",
        key: "description",
        ellipsis: true,
        render: (description: string) =>
          description ? (
            <Tooltip title={description} styles={{ root: { maxWidth: 420 } }}>
              <Text type="secondary" style={{ fontSize: 13 }}>
                {description}
              </Text>
            </Tooltip>
          ) : (
            <Text type="secondary" style={{ fontSize: 12, opacity: 0.6 }}>
              {t("platform_skills.no_description")}
            </Text>
          ),
      },
      {
        title: t("platform_skills.col_category"),
        dataIndex: "category",
        key: "category",
        width: 140,
        render: (category: string) =>
          category ? <Tag>{category}</Tag> : <Text type="secondary">—</Text>,
      },
      {
        title: t("platform_skills.col_tier"),
        dataIndex: "required_tier",
        key: "required_tier",
        width: 120,
        render: (tier: PlatformSkillTier) => (
          <Tag color={TIER_COLOR[tier]}>{t(`platform_skills.tier_${tier}`)}</Tag>
        ),
      },
      {
        title: t("platform_skills.col_status"),
        dataIndex: "status",
        key: "status",
        width: 120,
        render: (status: PlatformSkillStatus) => (
          <Tag color={STATUS_COLOR[status]}>{t(`platform_skills.status_${status}`)}</Tag>
        ),
      },
      {
        title: t("platform_skills.col_version"),
        dataIndex: "latest_version",
        key: "latest_version",
        width: 110,
        render: (v: number | null) =>
          v !== null ? (
            <Tag bordered={false}>v{v}</Tag>
          ) : (
            <Text type="secondary">—</Text>
          ),
      },
      {
        title: t("platform_skills.col_actions"),
        key: "actions",
        width: 260,
        render: (_v, row) => (
          <div style={{ display: "flex", gap: 6 }}>
            <Button size="small" onClick={() => openManage(row)} data-testid={`ps-manage-${row.id}`}>
              {t("platform_skills.manage")}
            </Button>
            <Button
              size="small"
              onClick={() => onPinToggle(row)}
              data-testid={`ps-pin-toggle-${row.id}`}
            >
              {row.pinned ? t("platform_skills.unpin") : t("platform_skills.pin")}
            </Button>
            <Button
              size="small"
              disabled={row.latest_version === null}
              onClick={() => void onExportRow(row)}
              data-testid={`ps-export-${row.id}`}
            >
              {t("skills.export_zip")}
            </Button>
          </div>
        ),
      },
    ],
    [t, openManage, onPinToggle, onExportRow],
  );

  const emptyText = (
    <div style={{ textAlign: "center", padding: "32px 0" }} data-testid="ps-empty">
      <Sparkles size={32} strokeWidth={1.25} style={{ opacity: 0.35, marginBottom: 8 }} />
      <div style={{ fontWeight: 600, marginBottom: 4 }}>{t("platform_skills.empty_title")}</div>
      <div
        style={{
          color: "var(--ew-text-tertiary, #666)",
          maxWidth: 360,
          margin: "0 auto 16px",
        }}
      >
        {t("platform_skills.empty_hint")}
      </div>
      <Button
        type="primary"
        icon={<Upload size={14} strokeWidth={1.75} />}
        onClick={onImportClick}
        data-testid="ps-empty-import"
      >
        {t("platform_skills.import_zip")}
      </Button>
    </div>
  );

  return (
    <div data-testid="ps-root">
      <input
        ref={fileInputRef}
        type="file"
        accept=".zip,.skill,application/zip"
        style={{ display: "none" }}
        onChange={onImportFile}
        data-testid="ps-import-input"
      />
      <PageHeader
        icon={<Sparkles size={18} strokeWidth={1.5} />}
        title={t("platform_skills.page_title")}
        subtitle={t("platform_skills.subtitle")}
        actions={
          isSystemAdmin && (
            <div style={{ display: "flex", gap: 8 }}>
              <Button
                onClick={() => void refresh()}
                loading={loading}
                icon={<RefreshCw size={14} strokeWidth={1.5} />}
              >
                {t("common.refresh")}
              </Button>
              <Button
                onClick={() => setGhOpen(true)}
                icon={<GitBranch size={14} strokeWidth={1.5} />}
                data-testid="ps-import-github-btn"
              >
                {t("platform_skills.import_github")}
              </Button>
              <Button
                type="primary"
                onClick={onImportClick}
                icon={<Upload size={14} strokeWidth={1.75} />}
                data-testid="ps-import-btn"
              >
                {t("platform_skills.import_zip")}
              </Button>
            </div>
          )
        }
      />

      <Modal
        open={ghOpen}
        title={t("platform_skills.github_modal_title")}
        okText={
          ghCandidates.length > 0
            ? t("platform_skills.github_batch_submit", { count: ghSelected.length })
            : t("platform_skills.github_submit")
        }
        onOk={() => void onGithubImport()}
        confirmLoading={ghBusy}
        okButtonProps={{
          disabled:
            ghSource.trim().length === 0 ||
            (ghCandidates.length > 0 && ghSelected.length === 0),
          "data-testid": "ps-github-submit",
        }}
        onCancel={() => {
          setGhOpen(false);
          resetGhForm();
        }}
        destroyOnHidden
        data-testid="ps-github-modal"
      >
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t("platform_skills.github_hint")}
        </Text>
        <div style={{ display: "flex", flexDirection: "column", gap: 12, marginTop: 12 }}>
          <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <Text style={{ fontSize: 12, fontWeight: 600 }}>
              {t("platform_skills.github_source_label")}
              <FieldHelp text={t("platform_skills.github_source_help")} testId="ps-github-source" />
            </Text>
            <Input
              value={ghSource}
              onChange={(e) => {
                setGhSource(e.target.value);
                // repo changed → stale candidate/selection/results
                setGhCandidates([]);
                setGhSelected([]);
                setGhResults(null);
              }}
              placeholder={t("platform_skills.github_source_ph")}
              data-testid="ps-github-source"
            />
          </label>
          {ghCandidates.length > 0 ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <div
                style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}
              >
                <Text style={{ fontSize: 12, fontWeight: 600 }}>
                  {t("platform_skills.github_skill_label")}
                  <FieldHelp
                    text={t("platform_skills.github_skill_help")}
                    testId="ps-github-skill-picker"
                  />
                </Text>
                <Button
                  size="small"
                  type="link"
                  onClick={() =>
                    setGhSelected(
                      ghSelected.length === ghCandidates.length ? [] : [...ghCandidates],
                    )
                  }
                  data-testid="ps-github-select-all"
                >
                  {ghSelected.length === ghCandidates.length
                    ? t("platform_skills.github_clear_all")
                    : t("platform_skills.github_select_all")}
                </Button>
              </div>
              <Alert
                type="info"
                showIcon
                message={t("platform_skills.github_pick_skill", {
                  count: ghCandidates.length,
                })}
                style={{ marginBottom: 4 }}
                data-testid="ps-github-candidates-hint"
              />
              <Select
                mode="multiple"
                showSearch
                loading={ghListing}
                value={ghSelected}
                onChange={(v) => setGhSelected(v)}
                placeholder={t("platform_skills.github_pick_ph")}
                options={ghCandidates.map((c) => ({ label: c, value: c }))}
                data-testid="ps-github-skill-select"
              />
              {ghResults !== null && (
                <div
                  style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 4 }}
                  data-testid="ps-github-results"
                >
                  {ghResults.map((r) => (
                    <div
                      key={r.skill}
                      style={{ display: "flex", alignItems: "center", gap: 8 }}
                    >
                      <Tag color={GH_RESULT_COLOR[r.status]}>
                        {t(`platform_skills.github_result_${r.status}`)}
                      </Tag>
                      <Text style={{ fontFamily: "var(--ew-font-mono)", fontSize: 12 }}>
                        {r.skill}
                      </Text>
                      {r.reason && (
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {r.reason}
                        </Text>
                      )}
                      {r.runtime && !r.runtime.runnable && (
                        <Text type="warning" style={{ fontSize: 12 }}>
                          ⚠ {r.runtime.hint}
                        </Text>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          ) : (
            <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <Text style={{ fontSize: 12, fontWeight: 600 }}>
                {t("platform_skills.github_skill_label")}
                <FieldHelp text={t("platform_skills.github_skill_help")} testId="ps-github-skill" />
              </Text>
              <Input
                value={ghSkill}
                onChange={(e) => setGhSkill(e.target.value)}
                placeholder={t("platform_skills.github_skill_ph")}
                data-testid="ps-github-skill"
              />
            </label>
          )}
          <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <Text style={{ fontSize: 12, fontWeight: 600 }}>
              {t("platform_skills.github_ref_label")}
              <FieldHelp text={t("platform_skills.github_ref_help")} testId="ps-github-ref" />
            </Text>
            <Input
              value={ghRef}
              onChange={(e) => setGhRef(e.target.value)}
              placeholder={t("platform_skills.github_ref_ph")}
              data-testid="ps-github-ref"
            />
          </label>
        </div>
      </Modal>

      {!isSystemAdmin ? (
        <Alert
          type="warning"
          showIcon
          message={t("platform_skills.not_admin_title")}
          description={t("platform_skills.not_admin_body")}
          data-testid="ps-not-admin"
        />
      ) : (
        <>
          {error !== null && (
            <Alert
              type="error"
              showIcon
              message={t("platform_skills.failed_to_load")}
              description={error}
              style={{ marginBottom: 12 }}
              data-testid="ps-error"
            />
          )}
          <Space
            style={{ marginBottom: 12 }}
            wrap
            data-testid="ps-filter-bar"
          >
            <Input.Search
              allowClear
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder={t("platform_skills.search_ph")}
              aria-label={t("platform_skills.search_aria")}
              style={{ width: 280 }}
              data-testid="ps-search"
            />
            <Select<PlatformSkillStatus | "all">
              value={statusFilter ?? "all"}
              onChange={(v) => setStatusFilter(v === "all" ? undefined : v)}
              aria-label={t("platform_skills.status_filter_aria")}
              style={{ width: 160 }}
              data-testid="ps-status-filter"
              options={[
                { label: t("platform_skills.status_filter_all"), value: "all" },
                { label: t("platform_skills.status_draft"), value: "draft" },
                { label: t("platform_skills.status_active"), value: "active" },
                { label: t("platform_skills.status_archived"), value: "archived" },
              ]}
            />
            <Select<string>
              value={categoryFilter ?? "__all__"}
              onChange={(v) => setCategoryFilter(v === "__all__" ? undefined : v)}
              aria-label={t("platform_skills.category_filter_aria")}
              style={{ width: 200 }}
              showSearch
              data-testid="ps-category-filter"
              options={[
                { label: t("platform_skills.category_filter_all"), value: "__all__" },
                ...categories.map((c) => ({ label: c, value: c })),
              ]}
            />
          </Space>
          {selectedRowKeys.length > 0 && (
            <Space
              style={{ marginBottom: 12 }}
              wrap
              data-testid="ps-batch-toolbar"
              aria-label={t("platform_skills.batch_toolbar_aria")}
            >
              <Text type="secondary" style={{ fontSize: 12 }}>
                {scopeIsAll
                  ? t("platform_skills.batch_scope_all", { count: total })
                  : t("platform_skills.batch_selected", { count: selectedRowKeys.length })}
              </Text>
              <Button
                size="small"
                loading={batchBusy}
                onClick={() => void runBatch({ set_pinned: true })}
                data-testid="ps-batch-lock"
              >
                {t("platform_skills.batch_lock")}
              </Button>
              <Button
                size="small"
                loading={batchBusy}
                onClick={() => void runBatch({ set_pinned: false })}
                data-testid="ps-batch-unlock"
              >
                {t("platform_skills.batch_unlock")}
              </Button>
              <Button
                size="small"
                loading={batchBusy}
                onClick={() => void runBatch({ set_status: "archived" })}
                data-testid="ps-batch-archive"
              >
                {t("platform_skills.batch_archive")}
              </Button>
              <Button
                size="small"
                loading={batchBusy}
                onClick={() => void runBatch({ set_status: "active" })}
                data-testid="ps-batch-activate"
              >
                {t("platform_skills.batch_activate")}
              </Button>
              <Dropdown
                trigger={["click"]}
                disabled={batchBusy}
                menu={{
                  items: [
                    ...categories.map((c) => ({ key: c, label: c })),
                    ...(categories.length > 0 ? [{ type: "divider" as const }] : []),
                    { key: CLEAR_CATEGORY_KEY, label: t("platform_skills.batch_clear_category") },
                  ],
                  onClick: ({ key }) =>
                    void runBatch({ set_category: key === CLEAR_CATEGORY_KEY ? "" : key }),
                }}
              >
                <Button size="small" loading={batchBusy} data-testid="ps-batch-set-category">
                  {t("platform_skills.batch_set_category")}
                </Button>
              </Dropdown>
              {canApplyToAll && (
                <Checkbox
                  checked={applyToAllMatching}
                  onChange={(e) => setApplyToAllMatching(e.target.checked)}
                  data-testid="ps-batch-all-matching"
                >
                  {t("platform_skills.batch_all_matching", { count: total })}
                </Checkbox>
              )}
              <Button
                size="small"
                type="link"
                onClick={() => {
                  setSelectedRowKeys([]);
                  setApplyToAllMatching(false);
                }}
                data-testid="ps-batch-clear"
              >
                {t("platform_skills.batch_clear")}
              </Button>
            </Space>
          )}
          <Table<PlatformSkill>
            columns={columns}
            dataSource={rows}
            rowKey={(r) => r.id}
            loading={loading}
            pagination={{
              current: page,
              pageSize,
              total,
              showSizeChanger: true,
              onChange: (p, ps) => {
                setPage(p);
                setPageSize(ps);
              },
            }}
            locale={{ emptyText }}
            rowSelection={{
              selectedRowKeys,
              onChange: (keys) => setSelectedRowKeys(keys),
              // aria-labels keep the bare checkboxes axe-clean (label rule).
              getCheckboxProps: (record) => ({
                disabled: batchBusy,
                "aria-label": t("platform_skills.batch_select_row", { name: record.name }),
              }),
              columnTitle: (originNode) => (
                <span aria-label={t("platform_skills.batch_select_all")}>{originNode}</span>
              ),
            }}
            data-testid="ps-table"
          />
        </>
      )}
    </div>
  );
}
