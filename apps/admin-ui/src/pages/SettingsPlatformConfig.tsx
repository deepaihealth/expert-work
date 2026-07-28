/**
 * Settings — Platform Credentials page (Stream P, Mini-ADR P-11/P-12).
 *
 * Platform-level (NOT tenant-scoped) management of provider/tool credential
 * refs — the runtime DB overlay over the env seed. system_admin only (mirrors
 * the backend ``is_system_admin`` gate); non-admins see a notice. Shows the
 * full catalog with source (env/db/unset), effective ref, enabled, and the
 * cross-tenant used-by-agents count. Edits submit a ref (kms:// / secret://),
 * never a plaintext key; "disable" is the soft path and delete is guarded
 * server-side (409 when env-defined or in use).
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  Drawer,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { TableColumnsType } from "antd";
import { Building2, KeyRound, RefreshCw } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { PageHeader } from "../components/PageHeader";
import {
  deletePlatformProviderKey,
  deleteTenantProviderOverride,
  deleteTenantToolOverride,
  getPlatformCredentials,
  getTenantCredentials,
  upsertPlatformProvider,
  upsertPlatformProviderKey,
  upsertPlatformTool,
  upsertTenantProviderOverride,
  upsertTenantToolOverride,
  type PlatformCredentialsView,
  type PlatformProviderKey,
  type PlatformProviderRow,
  type PlatformSecretSource,
  type PlatformSecretUpsertBody,
  type TenantCredentialsView,
  type TenantEffectiveSource,
  type TenantProviderEntry,
  type TenantToolEntry,
} from "../api/platform_config";
import { listTenants, type TenantSummary } from "../api/tenants";
import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { PlatformBillingSection } from "./settings_platform/PlatformBillingSection";
import { PlatformDelegationSection } from "./settings_platform/PlatformDelegationSection";
import { PlatformDynamicWorkerSection } from "./settings_platform/PlatformDynamicWorkerSection";
import { PlatformEmbeddingSection } from "./settings_platform/PlatformEmbeddingSection";
import { PlatformJudgeSection } from "./settings_platform/PlatformJudgeSection";
import { PlatformQualitySection } from "./settings_platform/PlatformQualitySection";
import { PlatformToolBudgetSection } from "./settings_platform/PlatformToolBudgetSection";

const { Text } = Typography;

type Kind = "provider" | "tool";

interface EditTarget {
  kind: Kind;
  key: string;
  secret_ref: string;
  /** Set when editing a per-tenant override (Stream HX-8) rather than the platform row. */
  tenantId?: string;
  /** Stream Y-MK — set when editing/adding a specific provider key. */
  keyId?: string;
  /** Stream Y-MK — failover priority for the key being edited (default 100). */
  priority?: number;
  /** Stream Y-MK — true when adding a brand-new key (key_id is editable). */
  isNewKey?: boolean;
}

// Key-format placeholder per credential row — the old hardcoded "sk-ant-…"
// (Anthropic's format) misled every non-Anthropic entry.
const KEY_PLACEHOLDERS: Record<string, string> = {
  anthropic: "sk-ant-…",
  openai: "sk-…",
  deepseek: "sk-…",
  qwen: "sk-…",
};

function sourceTag(source: PlatformSecretSource, t: (k: string) => string) {
  const color =
    source === "db" ? "cyan" : source === "env" ? "default" : undefined;
  return <Tag color={color}>{t(`settings_platform.source_${source}`)}</Tag>;
}

export function SettingsPlatformConfig() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const auth = useAuth();
  const isSystemAdmin = auth.identity?.isSystemAdmin ?? false;

  // Active domain tab, kept in the URL (?tab=) so refresh / deep links land
  // on the same tab. Inactive panes render lazily (antd default), so the
  // section components only fetch when their tab is first opened.
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab = searchParams.get("tab") ?? "credentials";

  const [view, setView] = useState<PlatformCredentialsView | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [edit, setEdit] = useState<EditTarget | null>(null);
  const [editForm] = Form.useForm<{
    secret_ref?: string;
    value?: string;
    enabled: boolean;
    key_id?: string;
    priority?: number;
  }>();
  const [editMode, setEditMode] = useState<"value" | "ref">("value");
  const [saving, setSaving] = useState(false);

  // Per-tenant override drawer (Stream HX-8).
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [tenants, setTenants] = useState<TenantSummary[]>([]);
  const [tenantId, setTenantId] = useState<string | null>(null);
  const [tenantView, setTenantView] = useState<TenantCredentialsView | null>(
    null,
  );
  const [tenantLoading, setTenantLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setView(await getPlatformCredentials());
    } catch (err) {
      setError(err instanceof Error ? err.message : "unknown error");
    } finally {
      setLoading(false);
    }
  }, []);

  const refreshTenantView = useCallback(async (tid: string) => {
    setTenantLoading(true);
    try {
      setTenantView(await getTenantCredentials(tid));
    } catch {
      setTenantView(null);
    } finally {
      setTenantLoading(false);
    }
  }, []);

  const openDrawer = useCallback(async () => {
    setDrawerOpen(true);
    try {
      setTenants(await listTenants(200));
    } catch {
      setTenants([]);
    }
  }, []);

  const onPickTenant = useCallback(
    (tid: string) => {
      setTenantId(tid);
      refreshTenantView(tid);
    },
    [refreshTenantView],
  );

  useEffect(() => {
    if (isSystemAdmin) {
      refresh();
    }
  }, [isSystemAdmin, refresh]);

  const errText = useCallback(
    (err: unknown): string =>
      err instanceof ApiError
        ? `${err.code}: ${err.message}`
        : err instanceof Error
          ? err.message
          : "failed",
    [],
  );

  const doUpsert = useCallback(
    async (
      kind: Kind,
      key: string,
      body: PlatformSecretUpsertBody,
      tid?: string,
    ) => {
      if (tid !== undefined) {
        if (kind === "provider") {
          await upsertTenantProviderOverride(tid, key, body);
        } else {
          await upsertTenantToolOverride(tid, key, body);
        }
      } else if (kind === "provider") {
        await upsertPlatformProvider(key, body);
      } else {
        await upsertPlatformTool(key, body);
      }
    },
    [],
  );

  const onDeleteOverride = useCallback(
    async (kind: Kind, key: string, tid: string) => {
      try {
        if (kind === "provider") {
          await deleteTenantProviderOverride(tid, key);
        } else {
          await deleteTenantToolOverride(tid, key);
        }
        message.success(t("settings_platform.deleted"));
        refreshTenantView(tid);
        refresh();
      } catch (err) {
        message.error(errText(err));
      }
    },
    [errText, message, refresh, refreshTenantView, t],
  );

  // Stream Y-MK — toggle one provider key's enabled flag (re-upsert its ref).
  const onToggleKey = useCallback(
    async (provider: string, key: PlatformProviderKey, enabled: boolean) => {
      try {
        await upsertPlatformProviderKey(provider, key.key_id, {
          secret_ref: key.secret_ref,
          enabled,
          priority: key.priority,
        });
        message.success(t("settings_platform.saved"));
        refresh();
      } catch (err) {
        message.error(errText(err));
      }
    },
    [errText, message, refresh, t],
  );

  // Stream Y-MK — delete one provider key (sibling failover keys remain).
  const onDeleteKey = useCallback(
    async (provider: string, keyId: string) => {
      try {
        await deletePlatformProviderKey(provider, keyId);
        message.success(t("settings_platform.deleted"));
        refresh();
      } catch (err) {
        message.error(errText(err));
      }
    },
    [errText, message, refresh, t],
  );

  const onSaveEdit = useCallback(async () => {
    if (edit === null) {
      return;
    }
    const values = await editForm.validateFields();
    const body: PlatformSecretUpsertBody =
      editMode === "value"
        ? { value: values.value, enabled: values.enabled }
        : { secret_ref: values.secret_ref, enabled: values.enabled };
    // Stream Y-MK — provider key edits carry priority + a key_id route.
    if (edit.kind === "provider" && edit.tenantId === undefined) {
      body.priority = values.priority ?? 100;
    }
    setSaving(true);
    try {
      if (
        edit.kind === "provider" &&
        edit.keyId !== undefined &&
        edit.tenantId === undefined
      ) {
        const keyId = edit.isNewKey ? (values.key_id ?? "").trim() : edit.keyId;
        await upsertPlatformProviderKey(edit.key, keyId, body);
      } else {
        await doUpsert(edit.kind, edit.key, body, edit.tenantId);
      }
      message.success(t("settings_platform.saved"));
      const editedTenant = edit.tenantId;
      setEdit(null);
      refresh();
      if (editedTenant !== undefined) {
        refreshTenantView(editedTenant);
      }
    } catch (err) {
      message.error(errText(err));
    } finally {
      setSaving(false);
    }
  }, [
    edit,
    editMode,
    editForm,
    doUpsert,
    errText,
    message,
    refresh,
    refreshTenantView,
    t,
  ]);

  // Stream Y-MK — nested per-key table shown when a provider row is expanded.
  const keyColumns = useCallback(
    (provider: string): TableColumnsType<PlatformProviderKey> => [
      {
        title: t("settings_platform.col_key_id"),
        dataIndex: "key_id",
        key: "key_id",
        width: 140,
        render: (keyId: string) => (
          <Tag color={keyId === "default" ? "blue" : "geekblue"}>{keyId}</Tag>
        ),
      },
      {
        title: t("settings_platform.col_priority"),
        dataIndex: "priority",
        key: "priority",
        width: 90,
        render: (p: number) => <Text>{p}</Text>,
      },
      {
        title: t("settings_platform.col_secret_ref"),
        dataIndex: "secret_ref",
        key: "secret_ref",
        render: (ref: string) => (
          <Tooltip title={ref}>
            <Text code style={{ fontSize: 11 }}>
              {ref}
            </Text>
          </Tooltip>
        ),
      },
      {
        title: t("settings_platform.col_enabled"),
        dataIndex: "enabled",
        key: "enabled",
        width: 90,
        render: (enabled: boolean, k) => (
          <Switch
            size="small"
            checked={enabled}
            onChange={(checked) => onToggleKey(provider, k, checked)}
            aria-label={`${provider} ${k.key_id} ${t("settings_platform.col_enabled")}`}
            data-testid={`pc-key-toggle-${provider}-${k.key_id}`}
          />
        ),
      },
      {
        title: t("settings_platform.col_actions"),
        key: "actions",
        width: 170,
        render: (_v, k) => (
          <Space size={6}>
            <Button
              size="small"
              onClick={() =>
                setEdit({
                  kind: "provider",
                  key: provider,
                  secret_ref: k.secret_ref,
                  keyId: k.key_id,
                  priority: k.priority,
                })
              }
              data-testid={`pc-key-edit-${provider}-${k.key_id}`}
            >
              {t("settings_platform.edit_btn")}
            </Button>
            <Popconfirm
              title={t("settings_platform.delete_key_confirm")}
              okType="danger"
              okText={t("common.delete")}
              cancelText={t("common.cancel")}
              onConfirm={() => onDeleteKey(provider, k.key_id)}
            >
              <Button
                size="small"
                danger
                data-testid={`pc-key-delete-${provider}-${k.key_id}`}
              >
                {t("common.delete")}
              </Button>
            </Popconfirm>
          </Space>
        ),
      },
    ],
    [t, onToggleKey, onDeleteKey],
  );

  const providerColumns = useMemo<TableColumnsType<PlatformProviderRow>>(
    () => [
      {
        title: t("settings_platform.col_name"),
        key: "name",
        render: (_v, row) => <Text strong>{row.provider}</Text>,
      },
      {
        title: t("settings_platform.col_source"),
        dataIndex: "source",
        key: "source",
        width: 110,
        render: (s: PlatformSecretSource) => sourceTag(s, t),
      },
      {
        title: t("settings_platform.col_secret_ref"),
        dataIndex: "secret_ref",
        key: "secret_ref",
        render: (ref: string | null) =>
          ref ? (
            <Tooltip title={ref}>
              <Text code style={{ fontSize: 11 }}>
                {ref}
              </Text>
            </Tooltip>
          ) : (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t("settings_platform.unset_ref")}
            </Text>
          ),
      },
      {
        title: t("settings_platform.col_keys"),
        key: "keys",
        width: 110,
        render: (_v, row) => {
          const total = row.keys.length;
          if (total === 0) {
            return <Text type="secondary">—</Text>;
          }
          const on = row.keys.filter((k) => k.enabled).length;
          return (
            <Tag color={on > 0 ? "cyan" : "red"}>
              {t("settings_platform.keys_summary", { on, total })}
            </Tag>
          );
        },
      },
      {
        title: t("settings_platform.col_used_by"),
        dataIndex: "used_by_agents",
        key: "used_by_agents",
        width: 100,
        render: (n: number) => <Text>{n}</Text>,
      },
      {
        title: t("settings_platform.col_tenant_overrides"),
        dataIndex: "tenant_override_count",
        key: "tenant_override_count",
        width: 120,
        render: (n: number | undefined) =>
          n ? <Tag color="purple">{n}</Tag> : <Text type="secondary">0</Text>,
      },
      {
        title: t("settings_platform.col_actions"),
        key: "actions",
        width: 140,
        render: (_v, row) => (
          <Button
            size="small"
            type="primary"
            ghost
            onClick={() =>
              setEdit({
                kind: "provider",
                key: row.provider,
                secret_ref: "",
                keyId: "",
                priority: 100,
                isNewKey: true,
              })
            }
            data-testid={`pc-add-key-${row.provider}`}
          >
            {t("settings_platform.add_key_btn")}
          </Button>
        ),
      },
    ],
    [t],
  );

  const effectiveSourceTag = useCallback(
    (source: TenantEffectiveSource) => {
      const color =
        source === "tenant"
          ? "purple"
          : source === "suppressed"
            ? "red"
            : source === "db"
              ? "cyan"
              : undefined;
      return (
        <Tag color={color}>
          {t(`settings_platform.tenant_source_${source}`)}
        </Tag>
      );
    },
    [t],
  );

  const makeOverrideColumns = useCallback(
    (kind: Kind): TableColumnsType<TenantProviderEntry | TenantToolEntry> => [
      {
        title: t("settings_platform.col_name"),
        key: "name",
        render: (_v, row) => (
          <Text strong>
            {"provider" in row ? row.provider : (row as TenantToolEntry).tool}
          </Text>
        ),
      },
      {
        title: t("settings_platform.col_effective"),
        key: "effective",
        width: 120,
        render: (_v, row) => effectiveSourceTag(row.effective_source),
      },
      {
        title: t("settings_platform.col_secret_ref"),
        key: "ref",
        render: (_v, row) =>
          row.effective_ref ? (
            <Tooltip title={row.effective_ref}>
              <Text code style={{ fontSize: 11 }}>
                {row.effective_ref}
              </Text>
            </Tooltip>
          ) : (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t("settings_platform.unset_ref")}
            </Text>
          ),
      },
      {
        title: t("settings_platform.col_actions"),
        key: "actions",
        width: 180,
        render: (_v, row) => {
          const key =
            "provider" in row ? row.provider : (row as TenantToolEntry).tool;
          return (
            <Space size={6}>
              <Button
                size="small"
                onClick={() =>
                  setEdit({
                    kind,
                    key,
                    secret_ref: row.override?.secret_ref ?? "",
                    tenantId: tenantId ?? undefined,
                  })
                }
                data-testid={`pc-tenant-edit-${key}`}
              >
                {row.override !== null
                  ? t("settings_platform.edit_btn")
                  : t("settings_platform.override_btn")}
              </Button>
              {row.override !== null && tenantId !== null && (
                <Popconfirm
                  title={t("settings_platform.delete_override_confirm")}
                  okType="danger"
                  okText={t("common.delete")}
                  cancelText={t("common.cancel")}
                  onConfirm={() => onDeleteOverride(kind, key, tenantId)}
                >
                  <Button
                    size="small"
                    danger
                    data-testid={`pc-tenant-delete-${key}`}
                  >
                    {t("common.delete")}
                  </Button>
                </Popconfirm>
              )}
            </Space>
          );
        },
      },
    ],
    [t, effectiveSourceTag, onDeleteOverride, tenantId],
  );

  const overrideProviderColumns = useMemo(
    () => makeOverrideColumns("provider"),
    [makeOverrideColumns],
  );

  useEffect(() => {
    if (edit !== null) {
      // Default to the paste-a-key flow; clear any prior pasted value.
      setEditMode("value");
      editForm.setFieldsValue({
        secret_ref: edit.secret_ref,
        value: "",
        enabled: true,
        key_id: edit.isNewKey ? "" : edit.keyId,
        priority: edit.priority ?? 100,
      });
    }
  }, [edit, editForm]);

  return (
    <div data-testid="pc-root">
      <PageHeader
        icon={<KeyRound size={18} strokeWidth={1.5} />}
        title={t("settings_platform.page_title")}
        subtitle={t("settings_platform.subtitle")}
        actions={
          isSystemAdmin && (
            <Space>
              <Button
                onClick={openDrawer}
                icon={<Building2 size={14} strokeWidth={1.5} />}
                data-testid="pc-tenant-overrides-btn"
              >
                {t("settings_platform.tenant_overrides_btn")}
              </Button>
              <Button
                onClick={refresh}
                loading={loading}
                icon={<RefreshCw size={14} strokeWidth={1.5} />}
              >
                {t("common.refresh")}
              </Button>
            </Space>
          )
        }
      />

      {!isSystemAdmin ? (
        <Alert
          type="warning"
          showIcon
          message={t("settings_platform.not_admin_title")}
          description={t("settings_platform.not_admin_body")}
          data-testid="pc-not-admin"
        />
      ) : (
        <>
          {error !== null && (
            <Alert
              type="error"
              showIcon
              message={t("settings_platform.failed_to_load")}
              description={error}
              style={{ marginBottom: 12 }}
              data-testid="pc-error"
            />
          )}
          <Tabs
            activeKey={activeTab}
            onChange={(k) => setSearchParams({ tab: k }, { replace: true })}
            items={[
              {
                key: "credentials",
                label: t("settings_platform.tab_credentials"),
                children: (
                  <Space direction="vertical" size={16} style={{ display: "flex" }}>
                    <Card
                      size="small"
                      title={t("settings_platform.providers_heading")}
                    >
                      <Table<PlatformProviderRow>
                        columns={providerColumns}
                        dataSource={view?.providers ?? []}
                        rowKey={(r) => r.provider}
                        loading={loading}
                        pagination={false}
                        size="small"
                        expandable={{
                          rowExpandable: (r) => r.keys.length > 0,
                          expandedRowRender: (r) => (
                            <Table<PlatformProviderKey>
                              columns={keyColumns(r.provider)}
                              dataSource={r.keys}
                              rowKey={(k) => k.key_id}
                              pagination={false}
                              size="small"
                              data-testid={`pc-keys-table-${r.provider}`}
                            />
                          ),
                        }}
                        data-testid="pc-providers-table"
                      />
                    </Card>
                    {/* NOTE: the per-tool credential table was removed from the
                        UI — the only catalog tool (web_search) runs on keyless
                        SearXNG and nothing consumes tool keys at runtime. The
                        backend slot stays; re-add a Card here if a keyed tool
                        ever lands. */}
                  </Space>
                ),
              },
              {
                key: "models",
                label: t("settings_platform.tab_models"),
                children: (
                  <Space direction="vertical" size={16} style={{ display: "flex" }}>
                    <Card
                      size="small"
                      title={t("settings_platform.embedding_heading")}
                    >
                      <PlatformEmbeddingSection />
                    </Card>
                    <Card
                      size="small"
                      title={t("settings_platform.judge_heading")}
                    >
                      <PlatformJudgeSection />
                    </Card>
                  </Space>
                ),
              },
              {
                key: "cost",
                label: t("settings_platform.tab_cost"),
                children: (
                  <Space direction="vertical" size={16} style={{ display: "flex" }}>
                    <Card
                      size="small"
                      title={t("settings_platform.billing_heading")}
                    >
                      <PlatformBillingSection />
                    </Card>
                    <Card
                      size="small"
                      title={t("settings_platform.tool_budget_heading")}
                    >
                      <PlatformToolBudgetSection />
                    </Card>
                    <Card
                      size="small"
                      title={t("settings_platform.dynamic_worker_heading")}
                    >
                      <PlatformDynamicWorkerSection />
                    </Card>
                    <Card
                      size="small"
                      title={t("settings_platform.delegation_heading")}
                    >
                      <PlatformDelegationSection />
                    </Card>
                  </Space>
                ),
              },
              {
                key: "quality",
                label: t("settings_platform.tab_quality"),
                children: (
                  <Card
                    size="small"
                    title={t("settings_platform.quality_heading")}
                  >
                    <PlatformQualitySection />
                  </Card>
                ),
              },
            ]}
          />
        </>
      )}

      <Drawer
        title={t("settings_platform.tenant_drawer_title")}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        width={720}
        data-testid="pc-tenant-drawer"
      >
        <Form.Item label={t("settings_platform.tenant_select_label")}>
          <Select
            showSearch
            optionFilterProp="label"
            placeholder={t("settings_platform.tenant_select_placeholder")}
            value={tenantId}
            onChange={onPickTenant}
            options={tenants.map((tn) => ({
              value: tn.tenant_id,
              label: `${tn.display_name} (${tn.tenant_id.slice(0, 8)}…)`,
            }))}
            style={{ width: "100%" }}
            data-testid="pc-tenant-select"
          />
        </Form.Item>
        {tenantId !== null && (
          <>
            <Alert
              type="info"
              showIcon
              message={t("settings_platform.tenant_drawer_hint")}
              style={{ marginBottom: 12 }}
            />
            <h3 style={{ fontSize: 14, margin: "8px 0" }}>
              {t("settings_platform.providers_heading")}
            </h3>
            <Table<TenantProviderEntry>
              columns={
                overrideProviderColumns as TableColumnsType<TenantProviderEntry>
              }
              dataSource={tenantView?.providers ?? []}
              rowKey={(r) => r.provider}
              loading={tenantLoading}
              pagination={false}
              size="small"
              data-testid="pc-tenant-providers-table"
            />
            {/* Tool overrides hidden with the platform tool table (see the
                credentials tab note) — the backend override API remains. */}
          </>
        )}
      </Drawer>

      <Modal
        title={
          edit?.tenantId !== undefined
            ? t("settings_platform.edit_override_modal_title", {
                key: edit?.key ?? "",
              })
            : edit?.isNewKey
              ? t("settings_platform.add_key_modal_title", {
                  key: edit?.key ?? "",
                })
              : t("settings_platform.edit_modal_title", {
                  key: edit?.key ?? "",
                })
        }
        open={edit !== null}
        onCancel={() => setEdit(null)}
        onOk={onSaveEdit}
        confirmLoading={saving}
        okText={t("common.save")}
        cancelText={t("common.cancel")}
        data-testid="pc-edit-modal"
      >
        <Form form={editForm} layout="vertical">
          <Form.Item label={t("settings_platform.mode_label")}>
            <Segmented
              value={editMode}
              onChange={(v) => setEditMode(v as "value" | "ref")}
              options={[
                { label: t("settings_platform.mode_value"), value: "value" },
                { label: t("settings_platform.mode_ref"), value: "ref" },
              ]}
              aria-label={t("settings_platform.mode_label")}
              data-testid="pc-edit-mode"
            />
          </Form.Item>
          {editMode === "value" ? (
            <Form.Item
              name="value"
              label={t("settings_platform.value_label")}
              extra={t("settings_platform.value_hint")}
              rules={[
                {
                  validator: (_r, value: string) =>
                    typeof value === "string" && value.length > 0
                      ? Promise.resolve()
                      : Promise.reject(
                          new Error(t("settings_platform.value_required")),
                        ),
                },
              ]}
            >
              <Input.Password
                placeholder={KEY_PLACEHOLDERS[edit?.key ?? ""] ?? "API key"}
                autoComplete="off"
                data-testid="pc-edit-value"
              />
            </Form.Item>
          ) : (
            <Form.Item
              name="secret_ref"
              label={t("settings_platform.secret_ref_label")}
              extra={t("settings_platform.secret_ref_hint")}
              rules={[
                {
                  validator: (_r, value: string) =>
                    typeof value === "string" &&
                    /^(secret|kms):\/\//.test(value)
                      ? Promise.resolve()
                      : Promise.reject(
                          new Error(t("settings_platform.secret_ref_hint")),
                        ),
                },
              ]}
            >
              <Input
                placeholder="kms://platform/anthropic-key"
                data-testid="pc-edit-ref"
              />
            </Form.Item>
          )}
          {edit?.kind === "provider" &&
            edit?.tenantId === undefined &&
            edit?.keyId !== undefined && (
              <>
                {edit?.isNewKey && (
                  <Form.Item
                    name="key_id"
                    label={t("settings_platform.key_id_label")}
                    extra={t("settings_platform.key_id_hint")}
                    rules={[
                      {
                        validator: (_r, value: string) =>
                          typeof value === "string" &&
                          /^[a-zA-Z0-9._-]+$/.test(value)
                            ? Promise.resolve()
                            : Promise.reject(
                                new Error(
                                  t("settings_platform.key_id_invalid"),
                                ),
                              ),
                      },
                    ]}
                  >
                    <Input placeholder="acct-b" data-testid="pc-edit-key-id" />
                  </Form.Item>
                )}
                <Form.Item
                  name="priority"
                  label={t("settings_platform.priority_label")}
                  extra={t("settings_platform.priority_hint")}
                >
                  <InputNumber
                    min={0}
                    max={9999}
                    style={{ width: 160 }}
                    data-testid="pc-edit-priority"
                  />
                </Form.Item>
              </>
            )}
          <Form.Item
            name="enabled"
            label={t("settings_platform.enabled_label")}
            valuePropName="checked"
          >
            <Switch
              aria-label={t("settings_platform.enabled_label")}
              data-testid="pc-edit-enabled"
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
