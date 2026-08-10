/**
 * Settings · API Keys — Stream H.1b PR 3.
 *
 * Real CRUD against ``/v1/api_keys`` + ``/v1/service_accounts``:
 *
 *   - list across the caller's tenant (system_admin → all tenants via
 *     :ref:`TenantSwitcher`)
 *   - create under a chosen service account; surface the plaintext
 *     bearer **once** in a confirmation modal that cannot be re-opened
 *   - rotate (double-active grace window per Mini-ADR K-1) — again
 *     show-once for the new plaintext
 *   - revoke (DELETE) with an explicit confirm dialog
 *
 * Scopes are READ / WRITE / ADMIN (expert_work's API-key scope alphabet —
 * narrower than the per-resource RBAC matrix; see
 * ``ApiKeyScope`` in ``expert_work.protocol.service_account``).
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Alert,
  App,
  Button,
  Checkbox,
  DatePicker,
  Empty,
  Form,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { TableColumnsType } from "antd";
import {
  AlertTriangle,
  Copy,
  Eye,
  Key,
  RotateCcw,
  ShieldAlert,
  Trash2,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import type { Dayjs } from "dayjs";

import { ApiError } from "../api/client";
import { PageHeader } from "../components/PageHeader";
import {
  API_KEY_SCOPES,
  createApiKey,
  listApiKeys,
  listServiceAccounts,
  revealApiKey,
  revokeApiKey,
  rotateApiKey,
  type ApiKeyCreated,
  type ApiKeyRecord,
  type ApiKeyScope,
  type ServiceAccountRecord,
} from "../api/api_keys";
import { useTenantScope } from "../tenant/TenantScopeContext";

const { Text } = Typography;

type RowStatus = "active" | "grace" | "revoked" | "expired";

function classifyKey(k: ApiKeyRecord): RowStatus {
  if (k.revoked_at !== null) return "revoked";
  if (k.expires_at !== null && new Date(k.expires_at) < new Date()) return "expired";
  if (k.rotated_at !== null && k.grace_period_s !== null) {
    const graceEnd = new Date(k.rotated_at).getTime() + k.grace_period_s * 1000;
    if (graceEnd > Date.now()) return "grace";
  }
  return "active";
}

export function SettingsApiKeys() {
  const { t } = useTranslation();
  const { apiTenantScope } = useTenantScope();
  const [searchParams, setSearchParams] = useSearchParams();
  const { message } = App.useApp();

  const [keys, setKeys] = useState<ApiKeyRecord[]>([]);
  const [accounts, setAccounts] = useState<ServiceAccountRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [showOnce, setShowOnce] = useState<ApiKeyCreated | null>(null);
  // Plaintext lives only in component state — cleared the instant the
  // modal closes, never persisted or logged.
  const [viewingKey, setViewingKey] = useState<string | null>(null);
  const [form] = Form.useForm<{
    serviceAccountId: string;
    scopes: ApiKeyScope[];
    expiresAt: Dayjs | null;
  }>();

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [keyList, saList] = await Promise.all([
        listApiKeys({ tenantScope: apiTenantScope }),
        listServiceAccounts({ tenantScope: apiTenantScope }),
      ]);
      setKeys(keyList.items);
      setAccounts(saList.items);
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
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (searchParams.get("action") === "create") {
      setCreateOpen(true);
      setSearchParams({});
    }
  }, [searchParams, setSearchParams]);

  const inRotation = useMemo(
    () => keys.filter((k) => classifyKey(k) === "grace").length,
    [keys],
  );

  const saName = useCallback(
    (id: string): string => accounts.find((a) => a.id === id)?.name ?? id.slice(0, 8) + "…",
    [accounts],
  );

  const handleCreate = async () => {
    try {
      const values = await form.validateFields();
      const created = await createApiKey(values.serviceAccountId, {
        scopes: values.scopes,
        expires_at: values.expiresAt ? values.expiresAt.toISOString() : null,
      });
      setCreateOpen(false);
      form.resetFields();
      setShowOnce(created);
      await refresh();
    } catch (err) {
      if (err instanceof ApiError) {
        message.error(`${err.code}: ${err.message}`);
      } else if (err instanceof Error) {
        message.error(err.message);
      }
    }
  };

  const handleRotate = async (apiKeyId: string) => {
    try {
      const rotated = await rotateApiKey(apiKeyId, { grace_period_s: 300 });
      setShowOnce(rotated.new);
      message.success(t("api_keys.rotated"));
      await refresh();
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : String(err);
      message.error(msg);
    }
  };

  const handleView = async (apiKeyId: string) => {
    try {
      const { plaintext } = await revealApiKey(apiKeyId);
      setViewingKey(plaintext);
    } catch (err) {
      if (err instanceof ApiError && err.code === "API_KEY_PLAINTEXT_UNAVAILABLE") {
        message.info(t("api_keys.view_unavailable"));
      } else if (err instanceof ApiError) {
        message.error(err.message);
      } else if (err instanceof Error) {
        message.error(err.message);
      }
    }
  };

  const handleRevoke = async (apiKeyId: string) => {
    try {
      await revokeApiKey(apiKeyId);
      message.success(t("api_keys.revoked"));
      await refresh();
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : String(err);
      message.error(msg);
    }
  };

  const statusTagColor: Record<RowStatus, string> = {
    active: "success",
    grace: "warning",
    revoked: "error",
    expired: "default",
  };

  const columns: TableColumnsType<ApiKeyRecord> = [
    {
      title: t("api_keys.col_prefix"),
      dataIndex: "prefix",
      key: "prefix",
      render: (p: string) => (
        <Text code style={{ fontSize: 11 }}>
          {p}
        </Text>
      ),
    },
    {
      title: t("api_keys.col_scopes"),
      dataIndex: "scopes",
      key: "scopes",
      render: (s: ApiKeyScope[]) => (
        <Space size={4} wrap>
          {s.map((scope) => (
            <Tag key={scope} bordered={false}>
              {scope}
            </Tag>
          ))}
        </Space>
      ),
    },
    {
      title: t("api_keys.col_service_account"),
      dataIndex: "service_account_id",
      key: "service_account_id",
      render: (id: string) => <span>{saName(id)}</span>,
    },
    {
      title: t("api_keys.col_status"),
      key: "status",
      width: 120,
      render: (_v, r) => {
        const cls = classifyKey(r);
        return (
          <Tag color={statusTagColor[cls]} bordered={false}>
            {cls}
          </Tag>
        );
      },
    },
    {
      title: t("api_keys.col_last_used"),
      dataIndex: "last_used_at",
      key: "last_used_at",
      width: 160,
      render: (v: string | null) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {v ? new Date(v).toLocaleString() : "—"}
        </Text>
      ),
    },
    {
      title: t("api_keys.col_expires"),
      dataIndex: "expires_at",
      key: "expires_at",
      width: 160,
      render: (v: string | null) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {v ? new Date(v).toLocaleDateString() : t("api_keys.never")}
        </Text>
      ),
    },
    {
      title: "",
      key: "actions",
      width: 100,
      render: (_v, r) => {
        const cls = classifyKey(r);
        if (cls === "revoked") return null;
        return (
          <Space size={4}>
            {cls !== "expired" && (
              <Tooltip title={t("api_keys.view")}>
                <Button
                  type="text"
                  size="small"
                  icon={<Eye size={14} strokeWidth={1.5} />}
                  onClick={() => void handleView(r.id)}
                  data-testid={`api-key-view-${r.id}`}
                />
              </Tooltip>
            )}
            <Tooltip title={t("api_keys.rotate")}>
              <Button
                type="text"
                size="small"
                icon={<RotateCcw size={14} strokeWidth={1.5} />}
                onClick={() => void handleRotate(r.id)}
                data-testid={`api-key-rotate-${r.id}`}
              />
            </Tooltip>
            <Popconfirm
              title={t("api_keys.revoke_confirm")}
              okType="danger"
              onConfirm={() => void handleRevoke(r.id)}
            >
              <Tooltip title={t("api_keys.revoke")}>
                <Button
                  type="text"
                  size="small"
                  danger
                  icon={<Trash2 size={14} strokeWidth={1.5} />}
                  data-testid={`api-key-revoke-${r.id}`}
                />
              </Tooltip>
            </Popconfirm>
          </Space>
        );
      },
    },
  ];

  return (
    <div>
      <PageHeader
        title={t("api_keys.page_title")}
        subtitle={t("api_keys.subtitle")}
        actions={
          <Button
            type="primary"
            icon={<Key size={14} strokeWidth={1.5} />}
            disabled={accounts.length === 0}
            onClick={() => setCreateOpen(true)}
            data-testid="api-key-create-open"
          >
            {t("api_keys.create")}
          </Button>
        }
      />

      {error !== null && (
        <Alert
          type="error"
          showIcon
          message={t("api_keys.failed_to_load")}
          description={error}
          style={{ marginBottom: 16 }}
          data-testid="api-keys-error"
        />
      )}

      {inRotation > 0 && (
        <Alert
          showIcon
          icon={<AlertTriangle size={16} strokeWidth={1.5} />}
          type="warning"
          message={<strong>{t("api_keys.rotation_banner", { count: inRotation })}</strong>}
          description={t("api_keys.rotation_help")}
          style={{ marginBottom: 16 }}
        />
      )}

      <Table<ApiKeyRecord>
        className="ew-table"
        rowKey="id"
        columns={columns}
        dataSource={keys}
        loading={loading}
        pagination={false}
        locale={{
          emptyText: (
            <Empty description={t("api_keys.empty")} />
          ),
        }}
        data-testid="api-keys-table"
      />

      <Modal
        title={t("api_keys.create")}
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        okText={t("api_keys.create")}
        cancelText={t("common.cancel")}
        onOk={() => void handleCreate()}
        data-testid="api-key-create-modal"
      >
        <Form
          form={form}
          layout="vertical"
          style={{ marginTop: 16 }}
          initialValues={{ scopes: ["read"], expiresAt: null }}
        >
          <Form.Item
            name="serviceAccountId"
            label={t("api_keys.service_account_label")}
            rules={[{ required: true, message: t("api_keys.service_account_required") }]}
          >
            <Select
              data-testid="api-key-sa-select"
              options={accounts.map((a) => ({ value: a.id, label: a.name }))}
            />
          </Form.Item>
          <Form.Item
            name="scopes"
            label={t("api_keys.scopes_label")}
            rules={[{ required: true, message: t("api_keys.scopes_required") }]}
          >
            <Checkbox.Group style={{ display: "flex", flexDirection: "column", gap: 10, width: "100%" }}>
              {API_KEY_SCOPES.map((s) => (
                <Checkbox key={s} value={s} style={{ alignItems: "flex-start" }}>
                  <div>
                    <Text code style={{ fontSize: 12 }}>
                      {s}
                    </Text>
                    {s === "admin" && (
                      <Tag color="error" bordered={false} style={{ marginLeft: 6, fontSize: 10 }}>
                        <ShieldAlert size={10} strokeWidth={1.5} style={{ verticalAlign: "middle", marginRight: 2 }} />
                        {t("api_keys.dangerous")}
                      </Tag>
                    )}
                    <div>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {t(`api_keys.scope_help_${s}`)}
                      </Text>
                    </div>
                  </div>
                </Checkbox>
              ))}
            </Checkbox.Group>
          </Form.Item>
          <Form.Item
            name="expiresAt"
            label={t("api_keys.expires_label")}
            extra={t("api_keys.expires_extra")}
          >
            <DatePicker style={{ width: "100%" }} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        open={showOnce !== null}
        onCancel={() => setShowOnce(null)}
        title={
          <span style={{ color: "var(--ew-color-brand-400)" }}>
            <Key
              size={16}
              strokeWidth={1.5}
              style={{ verticalAlign: "middle", marginRight: 6 }}
            />
            {t("api_keys.show_once_title")}
          </span>
        }
        okText={t("api_keys.show_once_ack")}
        onOk={() => setShowOnce(null)}
        cancelButtonProps={{ style: { display: "none" } }}
        data-testid="api-key-show-once"
      >
        {showOnce !== null && (
          <div style={{ marginTop: 12 }}>
            <p>
              {t("api_keys.show_once_help_prefix")}
              <strong>{t("api_keys.show_once_help_emphasis")}</strong>。
            </p>
            <div
              style={{
                fontFamily: "var(--ew-font-mono)",
                fontSize: 14,
                padding: 12,
                background: "var(--ew-color-neutral-950, #0a0a0a)",
                border: "1px solid var(--ew-border-default)",
                borderRadius: 6,
                display: "flex",
                gap: 12,
                margin: "12px 0",
              }}
            >
              <code style={{ flex: 1, wordBreak: "break-all" }}>{showOnce.plaintext}</code>
              <Button
                size="small"
                icon={<Copy size={12} strokeWidth={1.5} />}
                onClick={() => {
                  void navigator.clipboard.writeText(showOnce.plaintext);
                  message.success(t("api_keys.copied"));
                }}
              >
                {t("api_keys.copy")}
              </Button>
            </div>
            <p style={{ color: "var(--ew-text-secondary)", fontSize: 13, margin: 0 }}>
              {t("api_keys.show_once_prefix_note")}{" "}
              <Text code>{showOnce.api_key.prefix}</Text>
            </p>
          </div>
        )}
      </Modal>

      <Modal
        open={viewingKey !== null}
        onCancel={() => setViewingKey(null)}
        onOk={() => setViewingKey(null)}
        okText={t("api_keys.view_close")}
        cancelButtonProps={{ style: { display: "none" } }}
        title={t("api_keys.view_title")}
        data-testid="api-key-view-modal"
      >
        {viewingKey !== null && (
          <Text code copyable style={{ wordBreak: "break-all" }}>
            {viewingKey}
          </Text>
        )}
      </Modal>
    </div>
  );
}
