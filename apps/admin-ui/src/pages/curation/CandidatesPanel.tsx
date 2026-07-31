/**
 * Curation Candidates panel — Stream H.4 PR 1.
 *
 * Trajectory review surface backed by ``GET /v1/curation/candidates`` +
 * detail + promote + dismiss. Cross-tenant filter inherits the outer
 * Tenant scope; the panel writes ``cross_tenant`` flag back to the page
 * via banner so reviewers always see whose runs they're judging.
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
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { TableColumnsType } from "antd";
import { Globe2, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  dismissCandidate,
  getCandidate,
  listCandidates,
  promoteCandidate,
  type CandidateStatus,
  type CurationCandidate,
  type CurationCandidateDetail,
  type CurationCandidateList,
  type CurationSignal,
} from "../../api/curation";
import { ApiError } from "../../api/client";
import { concreteTenantScope, useTenantScope } from "../../tenant/TenantScopeContext";

const { Text } = Typography;

const STATUS_COLOR: Record<CandidateStatus, string> = {
  pending: "processing",
  promoted: "success",
  dismissed: "default",
};

const STATUS_OPTIONS: CandidateStatus[] = ["pending", "promoted", "dismissed"];
const SIGNAL_OPTIONS: CurationSignal[] = [
  "manual",
  "negative_feedback",
  "tool_failure",
  "timeout",
  "policy_block",
];

export function CandidatesPanel() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const { scope, apiTenantScope } = useTenantScope();
  const [data, setData] = useState<CurationCandidateList | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<CandidateStatus | undefined>("pending");
  const [signalFilter, setSignalFilter] = useState<CurationSignal | undefined>(undefined);

  const [selected, setSelected] = useState<CurationCandidateDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const [promoteOpen, setPromoteOpen] = useState(false);
  const [promoteSubmitting, setPromoteSubmitting] = useState(false);
  const [promoteForm] = Form.useForm<{ name: string }>();

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listCandidates({
        tenantScope: apiTenantScope,
        status: statusFilter,
        signal: signalFilter,
      });
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
  }, [apiTenantScope, statusFilter, signalFilter]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const openDetail = useCallback(async (record: CurationCandidate) => {
    setDetailLoading(true);
    setSelected(null);
    try {
      // Detail read takes a concrete UUID only ("*" 400s).
      const detail = await getCandidate(record.id, concreteTenantScope(apiTenantScope));
      setSelected(detail);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "failed to load");
    } finally {
      setDetailLoading(false);
    }
  }, [message, apiTenantScope]);

  const onDismiss = useCallback(async (id: string) => {
    try {
      await dismissCandidate(id);
      message.success(t("curation.dismissed"));
      setSelected(null);
      refresh();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "failed");
    }
  }, [message, refresh, t]);

  const onPromote = useCallback(async () => {
    if (selected === null) return;
    const values = await promoteForm.validateFields();
    setPromoteSubmitting(true);
    try {
      await promoteCandidate(selected.id, {
        name: values.name,
        source: "promoted_candidate",
      });
      message.success(t("curation.promoted"));
      setPromoteOpen(false);
      promoteForm.resetFields();
      setSelected(null);
      refresh();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "failed");
    } finally {
      setPromoteSubmitting(false);
    }
  }, [selected, promoteForm, message, refresh, t]);

  const columns: TableColumnsType<CurationCandidate> = useMemo(() => [
    {
      title: t("curation.col_signal"),
      dataIndex: "signal",
      key: "signal",
      width: 160,
      render: (s: string) => <Tag>{s}</Tag>,
    },
    {
      title: t("curation.col_agent"),
      dataIndex: "agent_name",
      key: "agent",
      render: (name: string, record) => (
        <Space size={6}>
          <Text strong>{name}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>v{record.agent_version}</Text>
        </Space>
      ),
    },
    {
      title: t("curation.col_status"),
      dataIndex: "status",
      key: "status",
      width: 120,
      render: (s: CandidateStatus) => <Tag color={STATUS_COLOR[s]}>{s}</Tag>,
    },
    {
      title: t("curation.col_detected"),
      dataIndex: "detected_at",
      key: "detected_at",
      width: 200,
      render: (iso: string) => (
        <Text type="secondary" style={{ fontSize: 12 }}>{new Date(iso).toLocaleString()}</Text>
      ),
    },
    {
      title: t("curation.col_outcome"),
      dataIndex: "outcome",
      key: "outcome",
      ellipsis: true,
      render: (text: string) => (
        <Tooltip title={text}>
          <Text style={{ fontSize: 12 }}>{text}</Text>
        </Tooltip>
      ),
    },
  ], [t]);

  const isCrossTenant = data?.cross_tenant ?? false;

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12 }}>
        {isCrossTenant && (
          <Tag icon={<Globe2 size={12} strokeWidth={1.5} />} color="purple" data-testid="curation-cross-banner">
            {t("curation.cross_tenant_banner")}
          </Tag>
        )}
        <span style={{ flex: 1 }} />
        <Select<CandidateStatus | "all">
          value={statusFilter ?? "all"}
          onChange={(v) => setStatusFilter(v === "all" ? undefined : v as CandidateStatus)}
          style={{ width: 160 }}
          aria-label={t("curation.filter_status")}
          data-testid="curation-status-filter"
          options={[
            { value: "all", label: t("curation.filter_status_all") },
            ...STATUS_OPTIONS.map((s) => ({ value: s, label: s })),
          ]}
        />
        <Select<CurationSignal | "all">
          value={signalFilter ?? "all"}
          onChange={(v) => setSignalFilter(v === "all" ? undefined : v as CurationSignal)}
          style={{ width: 180 }}
          aria-label={t("curation.filter_signal")}
          data-testid="curation-signal-filter"
          options={[
            { value: "all", label: t("curation.filter_signal_all") },
            ...SIGNAL_OPTIONS.map((s) => ({ value: s, label: s })),
          ]}
        />
        <Button onClick={refresh} loading={loading} icon={<RefreshCw size={14} strokeWidth={1.5} />}>
          {t("common.refresh")}
        </Button>
      </div>

      {error !== null && (
        <Alert type="error" showIcon message={t("curation.failed_to_load")} description={error} style={{ marginBottom: 12 }} data-testid="curation-error" />
      )}

      <Table<CurationCandidate>
        columns={columns}
        dataSource={data?.items ?? []}
        rowKey={(r) => r.id}
        loading={loading}
        pagination={{ pageSize: 50, showSizeChanger: false, total: data?.total ?? 0 }}
        onRow={(record) => ({
          onClick: () => openDetail(record),
          style: { cursor: "pointer" },
        })}
        locale={{
          emptyText: (
            <Empty description={scope === "*" ? t("curation.empty_cross") : t("curation.empty_home")} />
          ),
        }}
        data-testid="curation-candidates-table"
      />

      <Drawer
        title={selected ? t("curation.detail_title") : ""}
        open={selected !== null || detailLoading}
        onClose={() => setSelected(null)}
        width={640}
        data-testid="curation-detail-drawer"
        loading={detailLoading}
        extra={
          selected?.status === "pending" && (
            <Space>
              <Button danger onClick={() => onDismiss(selected.id)} data-testid="curation-dismiss-btn">
                {t("curation.dismiss")}
              </Button>
              <Button type="primary" onClick={() => setPromoteOpen(true)} data-testid="curation-promote-btn">
                {t("curation.promote")}
              </Button>
            </Space>
          )
        }
      >
        {selected !== null && (
          <Space direction="vertical" style={{ width: "100%" }} size={12}>
            <div>
              <Text type="secondary" style={{ fontSize: 12 }}>{t("curation.detail_signal")}</Text>
              <div><Tag>{selected.signal}</Tag></div>
            </div>
            <div>
              <Text type="secondary" style={{ fontSize: 12 }}>{t("curation.detail_outcome")}</Text>
              <div>{selected.outcome}</div>
            </div>
            <div>
              <Text type="secondary" style={{ fontSize: 12 }}>{t("curation.detail_trajectory")}</Text>
              {selected.trajectory === null ? (
                <Alert type="warning" message={t("curation.trajectory_missing")} />
              ) : (
                <pre
                  data-testid="curation-trajectory-body"
                  style={{
                    background: "var(--ew-surface-raised)",
                    padding: 12,
                    borderRadius: 4,
                    fontSize: 11,
                    maxHeight: 480,
                    overflow: "auto",
                    margin: 0,
                  }}
                >
                  {JSON.stringify(selected.trajectory, null, 2)}
                </pre>
              )}
            </div>
          </Space>
        )}
      </Drawer>

      <Modal
        title={t("curation.promote_modal_title")}
        open={promoteOpen}
        onCancel={() => setPromoteOpen(false)}
        onOk={onPromote}
        confirmLoading={promoteSubmitting}
        data-testid="curation-promote-modal"
        okText={t("curation.promote")}
      >
        <Form form={promoteForm} layout="vertical">
          <Form.Item
            name="name"
            label={t("curation.promote_dataset_name")}
            rules={[{ required: true, message: t("curation.promote_name_required") }]}
          >
            <Input data-testid="curation-promote-name-input" maxLength={128} placeholder="e.g. golden_v2_negative_cases" />
          </Form.Item>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t("curation.promote_hint")}
          </Text>
        </Form>
      </Modal>
    </div>
  );
}
