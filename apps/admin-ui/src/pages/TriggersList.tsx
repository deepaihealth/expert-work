/**
 * Triggers list page — Stream H.4 PR 6.
 *
 * Two sub-categories (cron / webhook) split via Antd Tabs. Each tab
 * is a Table of triggers with an inline Enabled switch + Delete
 * action. Create Drawer switches form between cron (cron_expr input)
 * and webhook (no extra fields — secret is generated server-side and
 * surfaced *once* via the ``WebhookSecretShowOnce`` card).
 *
 * Webhook secret rotation is **not** an in-place operation in M0
 * (Mini-ADR / § 6.6.5): the UI flow is delete + re-create. The
 * show-once card warns explicitly that the secret will never appear
 * again.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  App,
  Button,
  Drawer,
  Empty,
  Form,
  Input,
  Popconfirm,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { TableColumnsType } from "antd";
import { Clock, Copy, Globe2, Plus, RefreshCw, Trash2, Webhook } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  createTrigger,
  deleteTrigger,
  listTriggers,
  patchTrigger,
  type CreateTriggerBody,
  type TriggerCreateResponse,
  type TriggerKind,
  type TriggerList,
  type TriggerRecord,
} from "../api/triggers";
import { ApiError } from "../api/client";
import { useTenantScope } from "../tenant/TenantScopeContext";
import { useIsTenantSwitched } from "../tenant/useIsTenantSwitched";
import { PageHeader } from "../components/PageHeader";

const { Text } = Typography;

interface CreateForm {
  name: string;
  agent_name: string;
  agent_version: string;
  cron_expr?: string;
}

export function TriggersList() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const { scope, apiTenantScope } = useTenantScope();
  // Cross-tenant W3 — 切入态只读:启停/删除/创建是写操作,置灰。
  const isTenantSwitched = useIsTenantSwitched();

  const [activeTab, setActiveTab] = useState<TriggerKind>("cron");
  const [data, setData] = useState<TriggerList | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [createOpen, setCreateOpen] = useState(false);
  const [createKind, setCreateKind] = useState<TriggerKind>("cron");
  const [createSubmitting, setCreateSubmitting] = useState(false);
  const [createForm] = Form.useForm<CreateForm>();

  /** Server-issued webhook secret — show *once* after create + then
   *  wipe from state when the user dismisses the card. */
  const [shownSecret, setShownSecret] = useState<{
    triggerName: string;
    secret: string;
  } | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listTriggers({ tenantScope: apiTenantScope });
      setData(result);
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
  }, [apiTenantScope]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const filteredItems = useMemo(() => {
    return (data?.items ?? []).filter((t) => t.kind === activeTab);
  }, [data, activeTab]);

  const counts = useMemo(() => {
    const all = data?.items ?? [];
    return {
      cron: all.filter((t) => t.kind === "cron").length,
      webhook: all.filter((t) => t.kind === "webhook").length,
    };
  }, [data]);

  const onToggleEnabled = useCallback(
    async (record: TriggerRecord, next: boolean) => {
      try {
        await patchTrigger(record.id, { enabled: next });
        message.success(t("triggers.toggled"));
        refresh();
      } catch (err) {
        message.error(err instanceof Error ? err.message : "failed");
      }
    },
    [message, refresh, t],
  );

  const onDelete = useCallback(
    async (id: string) => {
      try {
        await deleteTrigger(id);
        message.success(t("triggers.deleted"));
        refresh();
      } catch (err) {
        message.error(err instanceof Error ? err.message : "failed");
      }
    },
    [message, refresh, t],
  );

  const openCreate = useCallback(
    (kind: TriggerKind) => {
      setCreateKind(kind);
      createForm.resetFields();
      setCreateOpen(true);
    },
    [createForm],
  );

  const onCreate = useCallback(async () => {
    const values = await createForm.validateFields();
    const config: Record<string, unknown> =
      createKind === "cron" ? { expr: values.cron_expr } : {};
    const body: CreateTriggerBody = {
      name: values.name,
      agent_name: values.agent_name,
      agent_version: values.agent_version,
      kind: createKind,
      config,
    };
    setCreateSubmitting(true);
    try {
      const result: TriggerCreateResponse = await createTrigger(body);
      message.success(t("triggers.created"));
      setCreateOpen(false);
      if (createKind === "webhook" && typeof result.webhook_secret === "string") {
        setShownSecret({ triggerName: result.name, secret: result.webhook_secret });
      }
      refresh();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "failed");
    } finally {
      setCreateSubmitting(false);
    }
  }, [createForm, createKind, message, refresh, t]);

  const onCopySecret = useCallback(async () => {
    if (shownSecret === null) return;
    try {
      await navigator.clipboard.writeText(shownSecret.secret);
      message.success(t("triggers.secret_copied"));
    } catch {
      message.error(t("triggers.secret_copy_failed"));
    }
  }, [shownSecret, message, t]);

  const isCrossTenant = data?.cross_tenant ?? false;

  const columns: TableColumnsType<TriggerRecord> = useMemo(() => [
    {
      title: t("triggers.col_name"),
      dataIndex: "name",
      key: "name",
      render: (v: string) => <Text strong>{v}</Text>,
    },
    {
      title: t("triggers.col_agent"),
      dataIndex: "agent_name",
      key: "agent",
      render: (name: string, record) => (
        <Space size={6}>
          <Text>{name}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>v{record.agent_version}</Text>
        </Space>
      ),
    },
    activeTab === "cron"
      ? {
          title: t("triggers.col_cron_expr"),
          dataIndex: "config",
          key: "config",
          width: 180,
          render: (config: Record<string, unknown>) => (
            <Text code style={{ fontSize: 12 }}>{String(config.expr ?? "—")}</Text>
          ),
        }
      : {
          title: t("triggers.col_webhook_path"),
          dataIndex: "id",
          key: "webhook_path",
          width: 260,
          render: (id: string) => (
            <Text code style={{ fontSize: 11 }}>POST /v1/webhooks/{id}</Text>
          ),
        },
    {
      title: t("triggers.col_enabled"),
      dataIndex: "enabled",
      key: "enabled",
      width: 100,
      render: (v: boolean, record) => (
        <Tooltip title={isTenantSwitched ? t("common.tenant_switched_readonly") : undefined}>
          <Switch
            checked={v}
            disabled={isTenantSwitched}
            onChange={(next) => onToggleEnabled(record, next)}
            data-testid={`trigger-enabled-${record.id}`}
          />
        </Tooltip>
      ),
    },
    {
      title: t("triggers.col_updated"),
      dataIndex: "updated_at",
      key: "updated_at",
      width: 180,
      render: (iso: string) => (
        <Tooltip title={iso}>
          <Text type="secondary" style={{ fontSize: 12 }}>{new Date(iso).toLocaleString()}</Text>
        </Tooltip>
      ),
    },
    {
      title: t("triggers.col_actions"),
      key: "actions",
      width: 100,
      render: (_, record) => (
        <Popconfirm
          title={t("triggers.delete_confirm_title")}
          description={t("triggers.delete_confirm_body")}
          okType="danger"
          okText={t("common.delete")}
          cancelText={t("common.cancel")}
          onConfirm={() => onDelete(record.id)}
          disabled={isTenantSwitched}
        >
          <Tooltip title={isTenantSwitched ? t("common.tenant_switched_readonly") : undefined}>
            <Button
              size="small"
              danger
              disabled={isTenantSwitched}
              icon={<Trash2 size={12} strokeWidth={1.75} />}
              data-testid={`trigger-delete-${record.id}`}
            >
              {t("common.delete")}
            </Button>
          </Tooltip>
        </Popconfirm>
      ),
    },
  ], [t, activeTab, onToggleEnabled, onDelete, isTenantSwitched]);

  return (
    <div data-testid="triggers-root">
      <PageHeader
        icon={
          activeTab === "cron" ? (
            <Clock size={18} strokeWidth={1.5} />
          ) : (
            <Webhook size={18} strokeWidth={1.5} />
          )
        }
        title={t("triggers.page_title")}
        subtitle={t("triggers.subtitle")}
        actions={
          <>
            {isCrossTenant && (
              <Tag icon={<Globe2 size={12} strokeWidth={1.5} />} color="purple" data-testid="triggers-cross-banner">
                {t("triggers.cross_tenant_banner")}
              </Tag>
            )}
            <Button onClick={refresh} loading={loading} icon={<RefreshCw size={14} strokeWidth={1.5} />}>
              {t("common.refresh")}
            </Button>
            <Tooltip title={isTenantSwitched ? t("common.tenant_switched_readonly") : undefined}>
              <Button
                type="primary"
                icon={<Plus size={14} strokeWidth={1.75} />}
                onClick={() => openCreate(activeTab)}
                disabled={isTenantSwitched}
                data-testid="triggers-create-btn"
              >
                {t("triggers.create")}
              </Button>
            </Tooltip>
          </>
        }
      />

      <Tabs
        activeKey={activeTab}
        onChange={(k) => setActiveTab(k as TriggerKind)}
        items={[
          { key: "cron", label: `${t("triggers.tab_cron")} (${counts.cron})` },
          { key: "webhook", label: `${t("triggers.tab_webhook")} (${counts.webhook})` },
        ]}
        data-testid="triggers-tabs"
      />

      {error !== null && (
        <Alert type="error" showIcon message={t("triggers.failed_to_load")} description={error} style={{ marginBottom: 12 }} data-testid="triggers-error" />
      )}

      <Table<TriggerRecord>
        columns={columns}
        dataSource={filteredItems}
        rowKey={(r) => r.id}
        loading={loading}
        pagination={{ pageSize: 50, showSizeChanger: false, total: filteredItems.length }}
        locale={{
          emptyText: (
            <Empty
              description={
                scope === "*"
                  ? t("triggers.empty_cross")
                  : activeTab === "cron"
                    ? t("triggers.empty_cron")
                    : t("triggers.empty_webhook")
              }
            />
          ),
        }}
        data-testid="triggers-table"
      />

      <Drawer
        title={createKind === "cron" ? t("triggers.create_cron_title") : t("triggers.create_webhook_title")}
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        width={480}
        data-testid="triggers-create-drawer"
        extra={
          <Space>
            <Button onClick={() => setCreateOpen(false)}>{t("common.cancel")}</Button>
            <Button type="primary" loading={createSubmitting} onClick={onCreate}>
              {t("triggers.create_submit")}
            </Button>
          </Space>
        }
      >
        <Form form={createForm} layout="vertical">
          <Form.Item
            name="name"
            label={t("triggers.field_name")}
            rules={[{ required: true, message: t("triggers.name_required") }]}
          >
            <Input data-testid="trigger-name-input" maxLength={64} placeholder="daily_summary" />
          </Form.Item>
          <Form.Item
            name="agent_name"
            label={t("triggers.field_agent_name")}
            rules={[{ required: true, message: t("triggers.agent_required") }]}
          >
            <Input data-testid="trigger-agent-name-input" placeholder="research_agent" />
          </Form.Item>
          <Form.Item
            name="agent_version"
            label={t("triggers.field_agent_version")}
            rules={[{ required: true, message: t("triggers.agent_version_required") }]}
          >
            <Input data-testid="trigger-agent-version-input" placeholder="2.1.0" />
          </Form.Item>
          {createKind === "cron" && (
            <Form.Item
              name="cron_expr"
              label={t("triggers.field_cron_expr")}
              rules={[{ required: true, message: t("triggers.cron_required") }]}
              extra={t("triggers.cron_hint")}
            >
              <Input
                data-testid="trigger-cron-expr-input"
                placeholder="0 9 * * *"
                style={{ fontFamily: "var(--ew-font-mono)" }}
              />
            </Form.Item>
          )}
          {createKind === "webhook" && (
            <Alert
              type="info"
              showIcon
              message={t("triggers.webhook_secret_info_title")}
              description={t("triggers.webhook_secret_info_body")}
            />
          )}
        </Form>
      </Drawer>

      {shownSecret !== null && (
        <Drawer
          title={t("triggers.secret_drawer_title")}
          open={true}
          onClose={() => setShownSecret(null)}
          width={520}
          data-testid="trigger-secret-drawer"
          closable={false}
          extra={
            <Button onClick={() => setShownSecret(null)} data-testid="trigger-secret-close">
              {t("triggers.secret_acknowledged")}
            </Button>
          }
        >
          <Alert
            type="warning"
            showIcon
            message={t("triggers.secret_warn_title")}
            description={t("triggers.secret_warn_body")}
            style={{ marginBottom: 12 }}
          />
          <Text type="secondary" style={{ fontSize: 12 }}>{t("triggers.secret_trigger_name")}</Text>
          <div style={{ marginBottom: 12 }}><Text code>{shownSecret.triggerName}</Text></div>
          <Text type="secondary" style={{ fontSize: 12 }}>{t("triggers.secret_label")}</Text>
          <div
            style={{
              display: "flex",
              gap: 8,
              alignItems: "center",
              background: "var(--ew-surface-raised)",
              padding: 12,
              borderRadius: 4,
              border: "1px solid var(--ew-border-strong)",
              marginTop: 4,
            }}
          >
            <Text
              code
              style={{ flex: 1, wordBreak: "break-all", fontSize: 12 }}
              data-testid="trigger-secret-value"
            >
              {shownSecret.secret}
            </Text>
            <Button
              size="small"
              icon={<Copy size={12} strokeWidth={1.75} />}
              onClick={onCopySecret}
              data-testid="trigger-secret-copy"
            >
              {t("triggers.secret_copy")}
            </Button>
          </div>
        </Drawer>
      )}
    </div>
  );
}
