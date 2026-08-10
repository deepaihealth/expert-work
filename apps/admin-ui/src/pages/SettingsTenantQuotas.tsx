/**
 * Settings — Tenant Quotas page (Stream H.4 PR 8).
 *
 * Backend is tenant-scoped only (per-tenant ``/v1/tenants/{tid}/quotas``)
 * — for system_admin to manage another tenant's quotas they switch via
 * the TenantSwitcher. The effective ``tenant_id`` for the path comes
 * from ``apiTenantScope ?? identity.homeTenantId``; cross-tenant view
 * (``scope === "*"``) shows a banner telling the operator to pick a
 * concrete tenant.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  App,
  Button,
  Empty,
  Form,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { TableColumnsType } from "antd";
import { Gauge, Plus, RefreshCw, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { PageHeader } from "../components/PageHeader";
import {
  deleteTenantQuota,
  listTenantQuotas,
  upsertTenantQuota,
  type QuotaDimension,
  type TenantQuotaRecord,
} from "../api/tenant_quotas";
import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { useTenantScope } from "../tenant/TenantScopeContext";

const { Text } = Typography;

const DIMENSION_OPTIONS: QuotaDimension[] = [
  "qps",
  "tokens_per_day",
  "sandboxes",
  "monthly_token_budget",
  "image_upload_count_30d",
  "image_storage_bytes",
  "artifact_download_count_30d",
  "workspace_bytes_per_user",
];

// 字节维度:limit_value 列附带人类可读换算,建行表单下方提示同一换算。
const BYTES_DIMENSIONS: ReadonlySet<QuotaDimension> = new Set([
  "image_storage_bytes",
  "workspace_bytes_per_user",
]);

function formatBytesHint(v: number): string {
  if (!Number.isFinite(v) || v <= 0) return "";
  const gib = v / 1024 ** 3;
  return gib >= 1 ? `${gib % 1 === 0 ? gib : gib.toFixed(1)} GiB` : `${(v / 1024 ** 2).toFixed(0)} MiB`;
}

interface CreateForm {
  dimension: QuotaDimension;
  limit_value: number;
  burst: number | null;
}

export function SettingsTenantQuotas() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const { scope, apiTenantScope } = useTenantScope();
  const auth = useAuth();
  const homeTenantId = auth.identity?.homeTenantId ?? null;

  /** Effective tenant for the path: scope is "*" (cross-tenant) → block
   *  edit, otherwise use scope's UUID, falling back to home tenant. */
  const effectiveTenantId =
    scope === "*"
      ? null
      : typeof apiTenantScope === "string" && apiTenantScope !== "*"
        ? apiTenantScope
        : homeTenantId;

  const [data, setData] = useState<TenantQuotaRecord[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [createOpen, setCreateOpen] = useState(false);
  const [createSubmitting, setCreateSubmitting] = useState(false);
  const [createForm] = Form.useForm<CreateForm>();

  const refresh = useCallback(async () => {
    if (effectiveTenantId === null) {
      setData(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const result = await listTenantQuotas(effectiveTenantId);
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
  }, [effectiveTenantId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const onCreate = useCallback(async () => {
    if (effectiveTenantId === null) return;
    const values = await createForm.validateFields();
    setCreateSubmitting(true);
    try {
      await upsertTenantQuota(effectiveTenantId, {
        dimension: values.dimension,
        limit_value: values.limit_value,
        burst: values.burst,
        scope: {},
      });
      message.success(t("settings_ops.quota_created"));
      setCreateOpen(false);
      createForm.resetFields();
      refresh();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "failed");
    } finally {
      setCreateSubmitting(false);
    }
  }, [createForm, effectiveTenantId, message, refresh, t]);

  const onDelete = useCallback(
    async (quotaId: string) => {
      if (effectiveTenantId === null) return;
      try {
        await deleteTenantQuota(effectiveTenantId, quotaId);
        message.success(t("settings_ops.quota_deleted"));
        refresh();
      } catch (err) {
        message.error(err instanceof Error ? err.message : "failed");
      }
    },
    [effectiveTenantId, message, refresh, t],
  );

  const columns: TableColumnsType<TenantQuotaRecord> = useMemo(() => [
    {
      title: t("settings_ops.col_dimension"),
      dataIndex: "dimension",
      key: "dimension",
      width: 240,
      render: (v: QuotaDimension) => <Tag color="cyan">{v}</Tag>,
    },
    {
      title: t("settings_ops.col_limit_value"),
      dataIndex: "limit_value",
      key: "limit_value",
      width: 160,
      render: (v: number, record: TenantQuotaRecord) =>
        BYTES_DIMENSIONS.has(record.dimension) ? (
          <Text strong>
            {v.toLocaleString()} ({formatBytesHint(v)})
          </Text>
        ) : (
          <Text strong>{v.toLocaleString()}</Text>
        ),
    },
    {
      title: t("settings_ops.col_burst"),
      dataIndex: "burst",
      key: "burst",
      width: 120,
      render: (v: number | null) =>
        v === null ? (
          <Text type="secondary" style={{ fontSize: 12 }}>—</Text>
        ) : (
          <Text>{v.toLocaleString()}</Text>
        ),
    },
    {
      title: t("settings_ops.col_scope"),
      dataIndex: "scope",
      key: "scope",
      render: (s: Record<string, string>) =>
        Object.keys(s).length === 0 ? (
          <Text type="secondary" style={{ fontSize: 12 }}>—</Text>
        ) : (
          <Text code style={{ fontSize: 11 }}>{JSON.stringify(s)}</Text>
        ),
    },
    {
      title: t("settings_ops.col_updated"),
      dataIndex: "updated_at",
      key: "updated_at",
      width: 180,
      render: (iso: string) => (
        <Tooltip title={iso}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {new Date(iso).toLocaleString()}
          </Text>
        </Tooltip>
      ),
    },
    {
      title: t("settings_ops.col_actions"),
      key: "actions",
      width: 120,
      render: (_, record) => (
        <Popconfirm
          title={t("settings_ops.quota_delete_confirm_title")}
          description={t("settings_ops.quota_delete_confirm_body")}
          okType="danger"
          okText={t("common.delete")}
          cancelText={t("common.cancel")}
          onConfirm={() => onDelete(record.id)}
        >
          <Button
            size="small"
            danger
            icon={<Trash2 size={12} strokeWidth={1.75} />}
            data-testid={`quota-delete-${record.id}`}
          >
            {t("common.delete")}
          </Button>
        </Popconfirm>
      ),
    },
  ], [t, onDelete]);

  return (
    <div data-testid="quotas-root">
      <PageHeader
        icon={<Gauge size={18} strokeWidth={1.5} />}
        title={t("settings_ops.quotas_page_title")}
        subtitle={t("settings_ops.quotas_subtitle")}
        actions={
          <>
            {effectiveTenantId !== null && (
              <Tag color="default" data-testid="quota-tenant-tag">
                tenant:{" "}
                <Text code style={{ fontSize: 11 }}>{effectiveTenantId.slice(0, 8)}…</Text>
              </Tag>
            )}
            <Button
              onClick={refresh}
              loading={loading}
              icon={<RefreshCw size={14} strokeWidth={1.5} />}
              disabled={effectiveTenantId === null}
            >
              {t("common.refresh")}
            </Button>
            <Button
              type="primary"
              icon={<Plus size={14} strokeWidth={1.75} />}
              onClick={() => setCreateOpen(true)}
              disabled={effectiveTenantId === null}
              data-testid="quota-create-btn"
            >
              {t("settings_ops.quota_create")}
            </Button>
          </>
        }
      />

      {effectiveTenantId === null && (
        <Alert
          type="info"
          showIcon
          message={t("settings_ops.cross_tenant_blocked_title")}
          description={t("settings_ops.cross_tenant_blocked_body")}
          data-testid="quotas-cross-tenant-block"
        />
      )}

      {error !== null && (
        <Alert
          type="error"
          showIcon
          message={t("settings_ops.failed_to_load")}
          description={error}
          style={{ marginBottom: 12 }}
          data-testid="quotas-error"
        />
      )}

      {effectiveTenantId !== null && (
        <Table<TenantQuotaRecord>
          columns={columns}
          dataSource={data ?? []}
          rowKey={(r) => r.id}
          loading={loading}
          pagination={false}
          locale={{ emptyText: <Empty description={t("settings_ops.quota_empty")} /> }}
          data-testid="quotas-table"
        />
      )}

      <Modal
        title={t("settings_ops.quota_create_modal_title")}
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={onCreate}
        confirmLoading={createSubmitting}
        data-testid="quota-create-modal"
      >
        <Form form={createForm} layout="vertical" initialValues={{ burst: null }}>
          <Form.Item
            name="dimension"
            label={t("settings_ops.field_dimension")}
            rules={[{ required: true, message: t("settings_ops.dimension_required") }]}
          >
            <Select<QuotaDimension>
              data-testid="quota-dimension-select"
              options={DIMENSION_OPTIONS.map((d) => ({ value: d, label: d }))}
            />
          </Form.Item>
          <Form.Item
            name="limit_value"
            label={t("settings_ops.field_limit_value")}
            rules={[{ required: true, message: t("settings_ops.limit_required") }]}
          >
            <InputNumber min={0} style={{ width: "100%" }} data-testid="quota-limit-input" />
          </Form.Item>
          <Form.Item
            name="burst"
            label={t("settings_ops.field_burst")}
            extra={t("settings_ops.burst_hint")}
          >
            <InputNumber min={0} style={{ width: "100%" }} data-testid="quota-burst-input" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
