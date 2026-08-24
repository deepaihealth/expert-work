/**
 * Skills list page — Stream H.4 PR 5 + skill-authoring-ia Phase D.
 *
 * Cursor-paginated skill library. **Creation is import-only** (a skill is a
 * folder package — ``SKILL.md`` + scripts/references/assets — so a ``.skill``
 * ZIP is its natural authoring unit). The old "New skill" empty-shell drawer
 * was a dead end (no version → editor wouldn't render) and is removed; the
 * primary action is now Import ``.skill``. Iterate an imported skill's files
 * (and ``SKILL.md``) on its detail page.
 *
 * Cross-tenant scope inherits from ``TenantScopeContext`` (system_admin
 * can ``tenant_id=*``).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  App,
  Button,
  Empty,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { TableColumnsType } from "antd";
import {
  Boxes,
  Download,
  FileCode2,
  Globe2,
  Lock,
  Pin,
  RefreshCw,
  Upload,
} from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";

import {
  importSkillZip,
  listSkills,
  type SkillList,
  type SkillRecord,
  type SkillStatus,
  type SkillVisibility,
} from "../api/skills";
import { ApiError } from "../api/client";
import { tenantSkillApi } from "../api/skillApi";
import { useAuth } from "../auth/AuthContext";
import { concreteTenantScope, useTenantScope } from "../tenant/TenantScopeContext";
import { useIsTenantSwitched } from "../tenant/useIsTenantSwitched";
import { PageHeader } from "../components/PageHeader";
import { SkillEvolutionKillSwitch } from "../components/SkillEvolutionKillSwitch";
import { ReadonlyTooltip } from "../components/ReadonlyTooltip";

const { Text } = Typography;

const STATUS_OPTIONS: SkillStatus[] = ["draft", "active", "stale", "archived"];

const STATUS_COLOR: Record<SkillStatus, string> = {
  draft: "default",
  active: "success",
  // Sprint #4 — Curator auto-stale ≠ archived; render as default
  // (asleep) so the warm-orange archive semantic stays distinct.
  stale: "default",
  archived: "warning",
};

// Sprint #4 — Curator default stale threshold (mirrors backend
// _DEFAULT_STALE_DAYS in skill_curator.py). The list view doesn't have
// the per-tenant override, so this hint is best-effort under the
// platform default; pinned / non-active rows skip the hint entirely.
const STALE_DAYS_DEFAULT = 30;

function staleEtaLabel(
  lastUsedAt: string,
  t: (key: string, opts?: Record<string, unknown>) => string,
): string {
  const lastMs = new Date(lastUsedAt).getTime();
  if (Number.isNaN(lastMs)) return "";
  const ageDays = Math.floor((Date.now() - lastMs) / (1000 * 60 * 60 * 24));
  const remaining = STALE_DAYS_DEFAULT - ageDays;
  if (remaining <= 0) return t("skills.eta_due_soon");
  return t("skills.eta_days_to_stale", { days: remaining });
}

export function SkillsList() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const { scope, apiTenantScope } = useTenantScope();
  // Cross-tenant W3 — 切入态只读:导入 ZIP 是写操作,置灰。
  const isTenantSwitched = useIsTenantSwitched();
  // Cross-tenant W3 — home tenant for the aggregate row-jump: a foreign
  // row carries its owning tenant into the detail URL (see onRow below).
  const { identity } = useAuth();
  const navigate = useNavigate();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [data, setData] = useState<SkillList | null>(null);
  const [accumulated, setAccumulated] = useState<SkillRecord[]>([]);
  // Stream X-6 — platform skills returned by the merged view. They arrive
  // on the first page only (no pagination), so we capture them on refresh
  // and prepend them to the table. Server-side name-shadowing already
  // de-dupes against the tenant's own skills.
  const [platformItems, setPlatformItems] = useState<SkillRecord[]>([]);
  const [statusFilter, setStatusFilter] = useState<SkillStatus | undefined>(undefined);
  const [visibilityFilter, setVisibilityFilter] = useState<SkillVisibility | undefined>(undefined);
  const [categoryFilter, setCategoryFilter] = useState("");
  // BUG-3:平台技能全激活后列表 50+ 行,靠肉眼扫不现实 —— 名称/描述客户端
  // 过滤(status/category 走服务端,这个纯前端,零请求)。
  const [nameFilter, setNameFilter] = useState("");
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);


  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listSkills({
        tenantScope: apiTenantScope,
        status: statusFilter,
        visibility: visibilityFilter,
        category: categoryFilter.trim().length > 0 ? categoryFilter.trim() : undefined,
      });
      setData(result);
      setAccumulated(result.items);
      setPlatformItems(result.platform_items ?? []);
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
  }, [apiTenantScope, statusFilter, visibilityFilter, categoryFilter]);

  const loadMore = useCallback(async () => {
    if (data?.next_cursor === undefined || data?.next_cursor === null) return;
    setLoadingMore(true);
    try {
      const result = await listSkills({
        tenantScope: apiTenantScope,
        status: statusFilter,
        visibility: visibilityFilter,
        category: categoryFilter.trim().length > 0 ? categoryFilter.trim() : undefined,
        cursor: data.next_cursor,
      });
      setData(result);
      setAccumulated((prev) => [...prev, ...result.items]);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "failed");
    } finally {
      setLoadingMore(false);
    }
  }, [apiTenantScope, statusFilter, visibilityFilter, categoryFilter, data, message]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const onImportClick = useCallback(() => fileInputRef.current?.click(), []);

  const onImportFile = useCallback(
    async (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (!file) return;
      try {
        const result = await importSkillZip(file);
        message.success(
          t("skills.imported", {
            name: result.skill.name,
            version: result.version.version,
          }),
        );
        refresh();
      } catch (err) {
        message.error(err instanceof Error ? err.message : "import failed");
      } finally {
        if (fileInputRef.current) fileInputRef.current.value = "";
      }
    },
    [message, refresh, t],
  );

  // W4 D1 — row-level export of the latest version (read op: stays live in
  // the switched-in state). Scope mirrors the onRow row-jump: a foreign-tenant
  // row in the "*" aggregate carries its owning tenant; otherwise the ambient
  // scope collapses to a concrete UUID (or undefined for home).
  const onExportRow = useCallback(
    async (record: SkillRecord) => {
      if (record.latest_version === null) return;
      const tenantScope =
        record.tenant_id != null && record.tenant_id !== identity?.homeTenantId
          ? record.tenant_id
          : concreteTenantScope(apiTenantScope);
      try {
        const blob = await tenantSkillApi.exportVersion(
          record.id,
          record.latest_version,
          tenantScope,
        );
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = `${record.name}-v${record.latest_version}.skill`;
        document.body.appendChild(anchor);
        anchor.click();
        document.body.removeChild(anchor);
        URL.revokeObjectURL(url);
      } catch (err) {
        message.error(err instanceof Error ? err.message : "export failed");
      }
    },
    [apiTenantScope, identity, message],
  );

  const isCrossTenant = data?.cross_tenant ?? false;
  const hasMore = data?.next_cursor !== null && data?.next_cursor !== undefined;

  // Stream X-6 — platform skills render first, then the tenant's own
  // (paginated) skills. Platform rows are read-only in the tenant view.
  const dataSource = useMemo(() => {
    const merged = [...platformItems, ...accumulated];
    const q = nameFilter.trim().toLowerCase();
    if (q.length === 0) return merged;
    return merged.filter(
      (r) =>
        r.name.toLowerCase().includes(q) ||
        (r.description ?? "").toLowerCase().includes(q),
    );
  }, [platformItems, accumulated, nameFilter]);

  const columns: TableColumnsType<SkillRecord> = useMemo(() => [
    {
      title: t("skills.col_name"),
      dataIndex: "name",
      key: "name",
      render: (v: string, record) => {
        const isPlatform = record.source === "platform";
        return (
          <Space size={6}>
            {record.pinned && (
              <Tooltip title={t("skills.pin_tooltip_on")}>
                <Pin
                  size={12}
                  strokeWidth={2}
                  style={{ color: "var(--ew-color-brand-500)" }}
                  data-testid={`skill-pin-icon-${record.id}`}
                />
              </Tooltip>
            )}
            <Text strong>{v}</Text>
            {record.latest_version !== null && (
              <Tag bordered={false}>v{record.latest_version}</Tag>
            )}
            {/* Stream SE (SE-8) — agent_private visibility badge. Only the
                authoring agent sees these until promoted to tenant scope. */}
            {record.visibility === "agent_private" && (
              <Tooltip title={t("skill_evolution.visibility_agent_private")}>
                <Tag
                  icon={<Lock size={11} strokeWidth={1.75} />}
                  bordered={false}
                  data-testid={`skill-visibility-private-${record.id}`}
                >
                  {t("skill_evolution.visibility_agent_private")}
                </Tag>
              </Tooltip>
            )}
            {/* Stream X-6 — source badge: distinguish curated platform
                skills from the tenant's own. */}
            {isPlatform ? (
              <Tag
                icon={<Boxes size={11} strokeWidth={1.75} />}
                color="purple"
                data-testid={`skill-source-platform-${record.id}`}
              >
                {t("skills.source_platform")}
              </Tag>
            ) : (
              <Tag bordered={false} data-testid={`skill-source-tenant-${record.id}`}>
                {t("skills.source_tenant")}
              </Tag>
            )}
            {/* Stream X-6 — entitlement lock for platform rows the
                tenant's plan tier does not cover. */}
            {isPlatform && record.entitled === false && (
              <Tooltip
                title={t("skills.requires_tier_tooltip", {
                  tier: record.required_tier ?? "pro",
                })}
              >
                <Tag
                  icon={<Lock size={11} strokeWidth={1.75} />}
                  color="default"
                  data-testid={`skill-locked-${record.id}`}
                >
                  {t("skills.requires_tier", {
                    tier: record.required_tier ?? "pro",
                  })}
                </Tag>
              </Tooltip>
            )}
          </Space>
        );
      },
    },
    {
      title: t("skills.col_status"),
      dataIndex: "status",
      key: "status",
      width: 180,
      render: (s: SkillStatus, record) => (
        <Space size={6}>
          <Tag color={STATUS_COLOR[s]}>{t(`skills.status_${s}`)}</Tag>
          {/* Sprint #4 (Mini-ADR U-30) — distance-to-stale hint.
              Shows only for ``active`` rows so operators get an early
              signal before the Curator's nightly sweep flips them. */}
          {s === "active" && record.last_used_at !== null && !record.pinned && (
            <Text type="secondary" style={{ fontSize: 11 }}>
              {staleEtaLabel(record.last_used_at, t)}
            </Text>
          )}
          {/* SE-16 (SE-A47) — a draft at v>1 is a pending revision of an
              existing skill (dedup fold-in or a revise round), not a new
              entry: reviewers should diff against the prior version. */}
          {s === "draft" && (record.latest_version ?? 0) > 1 && (
            <Tooltip
              title={t("skills.revision_pending_tooltip", {
                version: record.latest_version,
              })}
            >
              <Tag
                color="geekblue"
                bordered={false}
                data-testid={`skill-revision-pending-${record.id}`}
              >
                {t("skills.revision_pending")}
              </Tag>
            </Tooltip>
          )}
        </Space>
      ),
    },
    {
      title: t("skills.col_category"),
      dataIndex: "category",
      key: "category",
      width: 160,
    },
    {
      title: t("skills.col_description"),
      dataIndex: "description",
      key: "description",
      ellipsis: true,
      render: (text: string) => (
        <Tooltip title={text} mouseEnterDelay={0.4}>
          <Text style={{ fontSize: 12 }}>{text}</Text>
        </Tooltip>
      ),
    },
    {
      title: t("skills.col_updated"),
      dataIndex: "updated_at",
      key: "updated_at",
      width: 180,
      render: (iso: string) => (
        <Text type="secondary" style={{ fontSize: 12 }}>{new Date(iso).toLocaleString()}</Text>
      ),
    },
    {
      title: t("skills.col_actions"),
      key: "actions",
      width: 120,
      // Platform rows are read-only in the tenant scope — the tenant
      // ``/v1/skills/{id}/…/export`` route would 404, so no export there.
      render: (_: unknown, record) =>
        record.source === "platform" ? null : (
          <Button
            size="small"
            icon={<Download size={13} strokeWidth={1.75} />}
            disabled={record.latest_version === null}
            onClick={(e) => {
              e.stopPropagation();
              void onExportRow(record);
            }}
            data-testid={`skill-row-export-${record.id}`}
          >
            {t("skills.export_zip")}
          </Button>
        ),
    },
  ], [t, onExportRow]);

  return (
    <div data-testid="skills-root">
      <PageHeader
        icon={<FileCode2 size={18} strokeWidth={1.5} />}
        title={t("skills.page_title")}
        subtitle={t("skills.subtitle")}
        actions={
          <>
            <SkillEvolutionKillSwitch />
            {isCrossTenant && (
              <Tag icon={<Globe2 size={12} strokeWidth={1.5} />} color="purple" data-testid="skills-cross-banner">
                {t("skills.cross_tenant_banner")}
              </Tag>
            )}
            <Input
              allowClear
              placeholder={t("skills.filter_name")}
              value={nameFilter}
              onChange={(e) => setNameFilter(e.target.value)}
              style={{ width: 180 }}
              aria-label={t("skills.filter_name")}
              data-testid="skills-name-filter"
            />
            <Select<SkillStatus | "all">
              value={statusFilter ?? "all"}
              onChange={(v) => setStatusFilter(v === "all" ? undefined : (v as SkillStatus))}
              style={{ width: 140 }}
              aria-label={t("skills.filter_status")}
              data-testid="skills-status-filter"
              options={[
                { value: "all", label: t("skills.filter_status_all") },
                ...STATUS_OPTIONS.map((s) => ({
                  value: s,
                  label: t(`skills.status_${s}`),
                })),
              ]}
            />
            <Select<SkillVisibility | "all">
              value={visibilityFilter ?? "all"}
              onChange={(v) =>
                setVisibilityFilter(v === "all" ? undefined : (v as SkillVisibility))
              }
              style={{ width: 150 }}
              aria-label={t("skill_evolution.filter_visibility")}
              data-testid="skills-visibility-filter"
              options={[
                { value: "all", label: t("skill_evolution.filter_visibility_all") },
                { value: "agent_private", label: t("skill_evolution.visibility_agent_private") },
                { value: "tenant", label: t("skill_evolution.visibility_tenant") },
              ]}
            />
            <Input
              placeholder={t("skills.filter_category")}
              value={categoryFilter}
              onChange={(e) => setCategoryFilter(e.target.value)}
              style={{ width: 160 }}
              allowClear
              data-testid="skills-category-filter"
            />
            <Button onClick={refresh} loading={loading} icon={<RefreshCw size={14} strokeWidth={1.5} />}>
              {t("common.refresh")}
            </Button>
            <ReadonlyTooltip on={isTenantSwitched}>
              <Button
                type="primary"
                onClick={onImportClick}
                disabled={isTenantSwitched}
                icon={<Upload size={14} strokeWidth={1.75} />}
                data-testid="skills-import-btn"
              >
                {t("skills.import_zip")}
              </Button>
            </ReadonlyTooltip>
          </>
        }
      />

      <input
        ref={fileInputRef}
        type="file"
        accept=".zip,.skill,application/zip"
        style={{ display: "none" }}
        onChange={onImportFile}
        data-testid="skills-import-input"
      />

      {error !== null && (
        <Alert type="error" showIcon message={t("skills.failed_to_load")} description={error} style={{ marginBottom: 12 }} data-testid="skills-error" />
      )}

      <Table<SkillRecord>
        columns={columns}
        dataSource={dataSource}
        rowKey={(r) => `${r.source ?? "tenant"}:${r.id}`}
        loading={loading}
        // BUG-3:此前 pagination={false} 让 50+ 行平铺成墙 —— 客户端分页
        // 20/页(单页时自动隐藏,小库无感);「加载更多」继续供服务端游标
        // 追加租户自有技能。
        pagination={{ pageSize: 20, showSizeChanger: false, hideOnSinglePage: true }}
        onRow={(record) =>
          // Platform rows are read-only in the tenant scope — a tenant
          // ``getSkill(id)`` would 404, so don't navigate. Bind via the
          // agent manifest instead.
          record.source === "platform"
            ? {}
            : {
                onClick: () => {
                  // Cross-tenant W3 — in the "*" aggregate a foreign-tenant
                  // row would 404 (getSkill falls back to the home tenant),
                  // so carry the row's owning tenant into the detail URL.
                  // Absent = pre-W3 backend; null = platform skill (NULL
                  // tenant) — both keep the old behavior.
                  const query =
                    record.tenant_id != null &&
                    record.tenant_id !== identity?.homeTenantId
                      ? `?tenant_id=${encodeURIComponent(record.tenant_id)}`
                      : "";
                  navigate(`/skills/${encodeURIComponent(record.id)}${query}`);
                },
                style: { cursor: "pointer" },
              }
        }
        locale={{
          emptyText: (
            <Empty
              description={scope === "*" ? t("skills.empty_cross") : t("skills.empty_home")}
            >
              {scope !== "*" && (
                <ReadonlyTooltip on={isTenantSwitched}>
                  <Button
                    type="primary"
                    icon={<Upload size={14} strokeWidth={1.75} />}
                    onClick={onImportClick}
                    disabled={isTenantSwitched}
                    data-testid="skills-empty-import"
                  >
                    {t("skills.import_zip")}
                  </Button>
                </ReadonlyTooltip>
              )}
            </Empty>
          ),
        }}
        data-testid="skills-table"
      />

      {hasMore && (
        <div style={{ display: "flex", justifyContent: "center", marginTop: 16 }}>
          <Button onClick={loadMore} loading={loadingMore} data-testid="skills-load-more">
            {t("skills.load_more")}
          </Button>
        </div>
      )}

    </div>
  );
}
