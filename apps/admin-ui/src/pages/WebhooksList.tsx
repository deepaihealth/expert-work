/**
 * Webhook endpoints list page — HX-9 (STREAM-HX § 13).
 *
 * A single Table of the tenant's registered outbound webhook endpoints
 * with an inline Enabled switch + Delete action. The Create Drawer takes
 * a name, delivery URL, a multi-select of subscribed event types, and an
 * optional agent scope. The HMAC signing secret is generated server-side
 * and surfaced *once* via the show-once drawer — rotation is delete +
 * re-create (M0), so the card warns it will never appear again.
 */
import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  App,
  Button,
  Drawer,
  Empty,
  Form,
  Input,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
} from "antd";
import type { TableColumnsType } from "antd";
import { Copy, Globe2, Plus, RefreshCw, Trash2, Webhook } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  WEBHOOK_EVENT_TYPES,
  WEBHOOK_PAYLOAD_FORMATS,
  createWebhookEndpoint,
  deleteWebhookEndpoint,
  listWebhookEndpoints,
  patchWebhookEndpoint,
  type CreateWebhookEndpointBody,
  type WebhookEndpoint,
  type WebhookEndpointCreateResponse,
  type WebhookEndpointList,
  type WebhookEventType,
  type WebhookPayloadFormat,
} from "../api/webhooks";
import { ApiError } from "../api/client";
import { useTenantScope } from "../tenant/TenantScopeContext";
import { useIsTenantSwitched } from "../tenant/useIsTenantSwitched";
import { PageHeader } from "../components/PageHeader";
import { ReadonlyTooltip } from "../components/ReadonlyTooltip";

const { Text } = Typography;

interface CreateForm {
  name: string;
  url: string;
  event_types: WebhookEventType[];
  agent_name?: string;
  payload_format?: WebhookPayloadFormat;
}

export function WebhooksList() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const { scope, apiTenantScope } = useTenantScope();
  // Cross-tenant W3 — 切入态只读:启停/删除/创建是写操作,置灰。
  const isTenantSwitched = useIsTenantSwitched();

  const [data, setData] = useState<WebhookEndpointList | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [createOpen, setCreateOpen] = useState(false);
  const [createSubmitting, setCreateSubmitting] = useState(false);
  const [createForm] = Form.useForm<CreateForm>();

  const [shownSecret, setShownSecret] = useState<{ name: string; secret: string } | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await listWebhookEndpoints({ tenantScope: apiTenantScope }));
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

  const onToggleEnabled = useCallback(
    async (record: WebhookEndpoint, next: boolean) => {
      try {
        await patchWebhookEndpoint(record.id, { enabled: next });
        message.success(t("webhooks.toggled"));
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
        await deleteWebhookEndpoint(id);
        message.success(t("webhooks.deleted"));
        refresh();
      } catch (err) {
        message.error(err instanceof Error ? err.message : "failed");
      }
    },
    [message, refresh, t],
  );

  const openCreate = useCallback(() => {
    createForm.resetFields();
    setCreateOpen(true);
  }, [createForm]);

  const onCreate = useCallback(async () => {
    const values = await createForm.validateFields();
    const body: CreateWebhookEndpointBody = {
      name: values.name,
      url: values.url,
      event_types: values.event_types,
      agent_name: values.agent_name?.trim() ? values.agent_name.trim() : null,
      payload_format: values.payload_format ?? "generic",
    };
    setCreateSubmitting(true);
    try {
      const result: WebhookEndpointCreateResponse = await createWebhookEndpoint(body);
      message.success(t("webhooks.created"));
      setCreateOpen(false);
      if (typeof result.secret === "string") {
        setShownSecret({ name: result.name, secret: result.secret });
      }
      refresh();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "failed");
    } finally {
      setCreateSubmitting(false);
    }
  }, [createForm, message, refresh, t]);

  const onCopySecret = useCallback(async () => {
    if (shownSecret === null) return;
    try {
      await navigator.clipboard.writeText(shownSecret.secret);
      message.success(t("webhooks.secret_copied"));
    } catch {
      message.error(t("webhooks.secret_copy_failed"));
    }
  }, [shownSecret, message, t]);

  const isCrossTenant = data?.cross_tenant ?? false;
  const items = data?.items ?? [];

  const columns: TableColumnsType<WebhookEndpoint> = [
    {
      title: t("webhooks.col_name"),
      dataIndex: "name",
      key: "name",
      render: (v: string) => <Text strong>{v}</Text>,
    },
    {
      title: t("webhooks.col_url"),
      dataIndex: "url",
      key: "url",
      render: (v: string) => (
        <Text code style={{ fontSize: 11, wordBreak: "break-all" }}>
          {v}
        </Text>
      ),
    },
    {
      title: t("webhooks.col_events"),
      dataIndex: "event_types",
      key: "event_types",
      width: 240,
      render: (events: WebhookEventType[]) => (
        <Space size={4} wrap>
          {events.map((e) => (
            <Tag key={e} style={{ fontSize: 11 }}>
              {e}
            </Tag>
          ))}
        </Space>
      ),
    },
    {
      title: t("webhooks.col_agent"),
      dataIndex: "agent_name",
      key: "agent_name",
      width: 140,
      render: (name: string | null) =>
        name ? <Text>{name}</Text> : <Text type="secondary">{t("webhooks.all_agents")}</Text>,
    },
    {
      title: t("webhooks.col_format"),
      dataIndex: "payload_format",
      key: "payload_format",
      width: 110,
      render: (fmt: WebhookPayloadFormat) =>
        fmt === "generic" ? (
          <Text type="secondary">{t("webhooks.format_generic")}</Text>
        ) : (
          <Tag color="cyan" style={{ fontSize: 11 }}>
            {t(`webhooks.format_${fmt}`)}
          </Tag>
        ),
    },
    {
      title: t("webhooks.col_enabled"),
      dataIndex: "enabled",
      key: "enabled",
      width: 90,
      render: (v: boolean, record) => (
        <ReadonlyTooltip on={isTenantSwitched}>
          <Switch
            checked={v}
            disabled={isTenantSwitched}
            onChange={(next) => onToggleEnabled(record, next)}
            data-testid={`webhook-enabled-${record.id}`}
          />
        </ReadonlyTooltip>
      ),
    },
    {
      title: t("webhooks.col_actions"),
      key: "actions",
      width: 100,
      render: (_, record) => (
        <ReadonlyTooltip on={isTenantSwitched}>
          <Popconfirm
            title={t("webhooks.delete_confirm_title")}
            description={t("webhooks.delete_confirm_body")}
            okType="danger"
            okText={t("common.delete")}
            cancelText={t("common.cancel")}
            onConfirm={() => onDelete(record.id)}
            disabled={isTenantSwitched}
          >
            <Button
              size="small"
              danger
              disabled={isTenantSwitched}
              icon={<Trash2 size={12} strokeWidth={1.75} />}
              data-testid={`webhook-delete-${record.id}`}
            >
              {t("common.delete")}
            </Button>
          </Popconfirm>
        </ReadonlyTooltip>
      ),
    },
  ];

  return (
    <div data-testid="webhooks-root">
      <PageHeader
        icon={<Webhook size={18} strokeWidth={1.5} />}
        title={t("webhooks.page_title")}
        subtitle={t("webhooks.subtitle")}
        actions={
          <>
            {isCrossTenant && (
              <Tag
                icon={<Globe2 size={12} strokeWidth={1.5} />}
                color="purple"
                data-testid="webhooks-cross-banner"
              >
                {t("webhooks.cross_tenant_banner")}
              </Tag>
            )}
            <Button onClick={refresh} loading={loading} icon={<RefreshCw size={14} strokeWidth={1.5} />}>
              {t("common.refresh")}
            </Button>
            <ReadonlyTooltip on={isTenantSwitched}>
              <Button
                type="primary"
                icon={<Plus size={14} strokeWidth={1.75} />}
                onClick={openCreate}
                disabled={isTenantSwitched}
                data-testid="webhooks-create-btn"
              >
                {t("webhooks.create")}
              </Button>
            </ReadonlyTooltip>
          </>
        }
      />

      {error !== null && (
        <Alert
          type="error"
          showIcon
          message={t("webhooks.failed_to_load")}
          description={error}
          style={{ marginBottom: 12 }}
          data-testid="webhooks-error"
        />
      )}

      <Table<WebhookEndpoint>
        columns={columns}
        dataSource={items}
        rowKey={(r) => r.id}
        loading={loading}
        pagination={{ pageSize: 50, showSizeChanger: false, total: items.length }}
        locale={{
          emptyText: (
            <Empty description={scope === "*" ? t("webhooks.empty_cross") : t("webhooks.empty")} />
          ),
        }}
        data-testid="webhooks-table"
      />

      <Drawer
        title={t("webhooks.create_title")}
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        width={480}
        data-testid="webhooks-create-drawer"
        extra={
          <Space>
            <Button onClick={() => setCreateOpen(false)}>{t("common.cancel")}</Button>
            <Button type="primary" loading={createSubmitting} onClick={onCreate}>
              {t("webhooks.create_submit")}
            </Button>
          </Space>
        }
      >
        <Form form={createForm} layout="vertical">
          <Form.Item
            name="name"
            label={t("webhooks.field_name")}
            rules={[{ required: true, message: t("webhooks.name_required") }]}
          >
            <Input data-testid="webhook-name-input" maxLength={64} placeholder="ops-notify" />
          </Form.Item>
          <Form.Item
            name="url"
            label={t("webhooks.field_url")}
            rules={[{ required: true, message: t("webhooks.url_required") }]}
            extra={t("webhooks.url_hint")}
          >
            <Input data-testid="webhook-url-input" placeholder="https://hooks.example.com/ingest" />
          </Form.Item>
          <Form.Item
            name="event_types"
            label={t("webhooks.field_events")}
            rules={[{ required: true, message: t("webhooks.events_required") }]}
          >
            <Select
              mode="multiple"
              data-testid="webhook-events-select"
              placeholder={t("webhooks.events_placeholder")}
              options={WEBHOOK_EVENT_TYPES.map((e) => ({ label: e, value: e }))}
            />
          </Form.Item>
          <Form.Item
            name="agent_name"
            label={t("webhooks.field_agent_name")}
            extra={t("webhooks.agent_name_hint")}
          >
            <Input data-testid="webhook-agent-name-input" placeholder={t("webhooks.all_agents")} />
          </Form.Item>
          <Form.Item
            name="payload_format"
            label={t("webhooks.field_payload_format")}
            extra={t("webhooks.payload_format_hint")}
            initialValue="generic"
          >
            <Select
              data-testid="webhook-payload-format-select"
              options={WEBHOOK_PAYLOAD_FORMATS.map((f) => ({
                label: t(`webhooks.format_${f}`),
                value: f,
              }))}
            />
          </Form.Item>
        </Form>
      </Drawer>

      {shownSecret !== null && (
        <Drawer
          title={t("webhooks.secret_drawer_title")}
          open={true}
          onClose={() => setShownSecret(null)}
          width={520}
          data-testid="webhook-secret-drawer"
          closable={false}
          extra={
            <Button onClick={() => setShownSecret(null)} data-testid="webhook-secret-close">
              {t("webhooks.secret_acknowledged")}
            </Button>
          }
        >
          <Alert
            type="warning"
            showIcon
            message={t("webhooks.secret_warn_title")}
            description={t("webhooks.secret_warn_body")}
            style={{ marginBottom: 12 }}
          />
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t("webhooks.secret_endpoint_name")}
          </Text>
          <div style={{ marginBottom: 12 }}>
            <Text code>{shownSecret.name}</Text>
          </div>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t("webhooks.secret_label")}
          </Text>
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
              data-testid="webhook-secret-value"
            >
              {shownSecret.secret}
            </Text>
            <Button
              size="small"
              icon={<Copy size={12} strokeWidth={1.75} />}
              onClick={onCopySecret}
              data-testid="webhook-secret-copy"
            >
              {t("webhooks.secret_copy")}
            </Button>
          </div>
        </Drawer>
      )}
    </div>
  );
}
