/**
 * Eval Datasets panel — Stream H.4 PR 1.
 *
 * Curated golden-dataset CRUD backed by ``/v1/eval-datasets``. Each row
 * has an ``input`` + ``expected`` JSON pair — edited via Monaco (the
 * same component H.3 PR 5 used for ``ApprovalCard``, with the same
 * pristine vs dirty buffer detection).
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
  Modal,
  Popconfirm,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { TableColumnsType } from "antd";
import Editor from "@monaco-editor/react";
import { Globe2, Plus, RefreshCw, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  createEvalDataset,
  deleteEvalDataset,
  listEvalDatasets,
  patchEvalDataset,
  type EvalDataset,
  type EvalDatasetList,
} from "../../api/curation";
import { ApiError } from "../../api/client";
import { useTenantScope } from "../../tenant/TenantScopeContext";
import { useIsTenantSwitched } from "../../tenant/useIsTenantSwitched";
import { ReadonlyTooltip } from "../../components/ReadonlyTooltip";

const { Text } = Typography;

type ParseResult =
  | { ok: true; value: Record<string, unknown> }
  | { ok: false; error: string };

function parseJsonObject(buffer: string, fallbackEmpty: boolean): ParseResult {
  if (buffer.trim().length === 0) {
    return fallbackEmpty
      ? { ok: true, value: {} }
      : { ok: false, error: "empty" };
  }
  try {
    const parsed: unknown = JSON.parse(buffer);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return { ok: false, error: "top-level must be a JSON object" };
    }
    return { ok: true, value: parsed as Record<string, unknown> };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "parse error" };
  }
}

export function EvalDatasetsPanel() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const { scope, apiTenantScope } = useTenantScope();
  // Cross-tenant W3 — 切入态只读:创建/编辑/删除是写操作,置灰。
  const isTenantSwitched = useIsTenantSwitched();

  const [data, setData] = useState<EvalDatasetList | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [createOpen, setCreateOpen] = useState(false);
  const [createSubmitting, setCreateSubmitting] = useState(false);
  const [createForm] = Form.useForm<{ agent_name: string; name: string }>();

  const [editing, setEditing] = useState<EvalDataset | null>(null);
  const [editSubmitting, setEditSubmitting] = useState(false);
  const [inputBuf, setInputBuf] = useState("");
  const [expectedBuf, setExpectedBuf] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listEvalDatasets({ tenantScope: apiTenantScope });
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

  const onCreate = useCallback(async () => {
    const values = await createForm.validateFields();
    setCreateSubmitting(true);
    try {
      await createEvalDataset({
        agent_name: values.agent_name,
        name: values.name,
        source: "golden",
      });
      message.success(t("eval_datasets.created"));
      setCreateOpen(false);
      createForm.resetFields();
      refresh();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "failed");
    } finally {
      setCreateSubmitting(false);
    }
  }, [createForm, message, refresh, t]);

  const openEdit = useCallback((record: EvalDataset) => {
    setEditing(record);
    setInputBuf(JSON.stringify(record.input ?? {}, null, 2));
    setExpectedBuf(record.expected !== null ? JSON.stringify(record.expected, null, 2) : "");
  }, []);

  const inputParse = useMemo(() => parseJsonObject(inputBuf, true), [inputBuf]);
  const expectedParse = useMemo(
    () => (expectedBuf.trim().length === 0 ? { ok: true as const, value: null } : parseJsonObject(expectedBuf, false)),
    [expectedBuf],
  );
  const editValid = inputParse.ok && expectedParse.ok;

  const onSaveEdit = useCallback(async () => {
    if (editing === null || !editValid) return;
    if (!inputParse.ok) return;
    if (!expectedParse.ok) return;
    setEditSubmitting(true);
    try {
      await patchEvalDataset(editing.id, {
        input: inputParse.value,
        expected: expectedParse.value === null ? null : (expectedParse.value as Record<string, unknown>),
      });
      message.success(t("eval_datasets.updated"));
      setEditing(null);
      refresh();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "failed");
    } finally {
      setEditSubmitting(false);
    }
  }, [editing, editValid, inputParse, expectedParse, message, refresh, t]);

  const onDelete = useCallback(async (id: string) => {
    try {
      await deleteEvalDataset(id);
      message.success(t("eval_datasets.deleted"));
      refresh();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "failed");
    }
  }, [message, refresh, t]);

  const columns: TableColumnsType<EvalDataset> = useMemo(() => [
    {
      title: t("eval_datasets.col_name"),
      dataIndex: "name",
      key: "name",
      render: (v: string) => <Text strong>{v}</Text>,
    },
    {
      title: t("eval_datasets.col_agent"),
      dataIndex: "agent_name",
      key: "agent_name",
    },
    {
      title: t("eval_datasets.col_source"),
      dataIndex: "source",
      key: "source",
      width: 180,
      render: (s: string) => <Tag>{s}</Tag>,
    },
    {
      title: t("eval_datasets.col_updated"),
      dataIndex: "updated_at",
      key: "updated_at",
      width: 200,
      render: (iso: string) => (
        <Tooltip title={iso}>
          <Text type="secondary" style={{ fontSize: 12 }}>{new Date(iso).toLocaleString()}</Text>
        </Tooltip>
      ),
    },
    {
      title: t("eval_datasets.col_actions"),
      key: "actions",
      width: 180,
      render: (_, record) => (
        <Space size={4}>
          <ReadonlyTooltip on={isTenantSwitched}>
            <Button
              size="small"
              disabled={isTenantSwitched}
              onClick={() => openEdit(record)}
              data-testid={`eval-edit-${record.id}`}
            >
              {t("common.edit") /* fallback to "Edit" via i18n */}
            </Button>
          </ReadonlyTooltip>
          <ReadonlyTooltip on={isTenantSwitched}>
            <Popconfirm
              title={t("eval_datasets.delete_confirm_title")}
              description={t("eval_datasets.delete_confirm_body")}
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
                data-testid={`eval-delete-${record.id}`}
              >
                {t("common.delete")}
              </Button>
            </Popconfirm>
          </ReadonlyTooltip>
        </Space>
      ),
    },
  ], [t, openEdit, onDelete, isTenantSwitched]);

  const isCrossTenant = data?.cross_tenant ?? false;

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12 }}>
        {isCrossTenant && (
          <Tag icon={<Globe2 size={12} strokeWidth={1.5} />} color="purple" data-testid="evald-cross-banner">
            {t("curation.cross_tenant_banner")}
          </Tag>
        )}
        <span style={{ flex: 1 }} />
        <Button onClick={refresh} loading={loading} icon={<RefreshCw size={14} strokeWidth={1.5} />}>
          {t("common.refresh")}
        </Button>
        <ReadonlyTooltip on={isTenantSwitched}>
          <Button
            type="primary"
            icon={<Plus size={14} strokeWidth={1.75} />}
            onClick={() => setCreateOpen(true)}
            disabled={isTenantSwitched}
            data-testid="evald-create-btn"
          >
            {t("eval_datasets.create")}
          </Button>
        </ReadonlyTooltip>
      </div>

      {error !== null && (
        <Alert type="error" showIcon message={t("eval_datasets.failed_to_load")} description={error} style={{ marginBottom: 12 }} data-testid="evald-error" />
      )}

      <Table<EvalDataset>
        columns={columns}
        dataSource={data?.items ?? []}
        rowKey={(r) => r.id}
        loading={loading}
        pagination={{ pageSize: 50, showSizeChanger: false, total: data?.total ?? 0 }}
        locale={{
          emptyText: (
            <Empty description={scope === "*" ? t("eval_datasets.empty_cross") : t("eval_datasets.empty_home")} />
          ),
        }}
        data-testid="eval-datasets-table"
      />

      <Modal
        title={t("eval_datasets.create_modal_title")}
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={onCreate}
        confirmLoading={createSubmitting}
        data-testid="evald-create-modal"
      >
        <Form form={createForm} layout="vertical">
          <Form.Item
            name="agent_name"
            label={t("eval_datasets.field_agent_name")}
            rules={[{ required: true, message: t("eval_datasets.agent_required") }]}
          >
            <Input data-testid="evald-agent-input" />
          </Form.Item>
          <Form.Item
            name="name"
            label={t("eval_datasets.field_name")}
            rules={[{ required: true, message: t("eval_datasets.name_required") }]}
          >
            <Input data-testid="evald-name-input" maxLength={128} />
          </Form.Item>
        </Form>
      </Modal>

      <Drawer
        title={editing !== null ? `${t("eval_datasets.edit_title")} — ${editing.name}` : ""}
        open={editing !== null}
        onClose={() => setEditing(null)}
        width={720}
        data-testid="evald-edit-drawer"
        extra={
          <Space>
            <Button onClick={() => setEditing(null)}>{t("common.cancel")}</Button>
            <Button
              type="primary"
              onClick={onSaveEdit}
              loading={editSubmitting}
              disabled={!editValid}
              data-testid="evald-save-btn"
            >
              {t("common.save")}
            </Button>
          </Space>
        }
      >
        {editing !== null && (
          <Space direction="vertical" style={{ width: "100%" }} size={16}>
            <div>
              <Text type="secondary">{t("eval_datasets.edit_input_label")}</Text>
              <div style={{ border: "1px solid var(--ew-border-default)", borderRadius: 4, marginTop: 4 }}>
                <Editor
                  height="220px"
                  defaultLanguage="json"
                  value={inputBuf}
                  onChange={(v) => setInputBuf(v ?? "")}
                  options={{ minimap: { enabled: false }, fontSize: 12 }}
                  data-testid="evald-input-editor"
                />
              </div>
              {!inputParse.ok && (
                <Alert type="error" message={t("eval_datasets.json_parse_error")} description={inputParse.error} style={{ marginTop: 6 }} data-testid="evald-input-error" />
              )}
            </div>
            <div>
              <Text type="secondary">{t("eval_datasets.edit_expected_label")}</Text>
              <div style={{ border: "1px solid var(--ew-border-default)", borderRadius: 4, marginTop: 4 }}>
                <Editor
                  height="220px"
                  defaultLanguage="json"
                  value={expectedBuf}
                  onChange={(v) => setExpectedBuf(v ?? "")}
                  options={{ minimap: { enabled: false }, fontSize: 12 }}
                  data-testid="evald-expected-editor"
                />
              </div>
              {!expectedParse.ok && (
                <Alert type="error" message={t("eval_datasets.json_parse_error")} description={expectedParse.error} style={{ marginTop: 6 }} data-testid="evald-expected-error" />
              )}
            </div>
          </Space>
        )}
      </Drawer>
    </div>
  );
}
