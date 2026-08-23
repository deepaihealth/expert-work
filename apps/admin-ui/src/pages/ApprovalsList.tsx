/**
 * Approvals queue page — Stream HX-7 PR 3 (STREAM-HX-DESIGN § 8.2-⑤).
 *
 * The cross-run approval queue over ``GET /v1/approvals``: oldest
 * waiting first, inline approve / reject per row, multi-select batch.
 * Single and batch verdicts both go through ``POST /v1/approvals:decide``
 * (one decision path — Mini-ADR HX-G5); ``modify`` with edited args
 * stays on the RunDetail ApprovalCard, which this page links to.
 *
 * Mirrors the RunsList shell (PageHeader, status filter, error Alert,
 * cross-tenant banner). Batch actions are disabled outside the home
 * tenant scope — ``:decide`` operates on the caller's tenant only.
 */
import { useCallback, useEffect, useMemo, useState, type Key } from "react";
import {
  Alert,
  Badge,
  Button,
  Empty,
  Popconfirm,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import type { TableColumnsType } from "antd";
import { CheckCircle2, Globe2, ListChecks, RefreshCw, XCircle } from "lucide-react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";

import {
  decideApprovals,
  listApprovals,
  type ApprovalItem,
  type ApprovalKindClass,
  type ApprovalList,
  type ApprovalStatus,
} from "../api/approvals";
import { ApiError } from "../api/client";
import { useTenantScope } from "../tenant/TenantScopeContext";
import { PageHeader } from "../components/PageHeader";

const { Text } = Typography;

const STATUS_COLOR: Record<string, string> = {
  pending: "warning",
  approved: "success",
  modified: "success",
  rejected: "error",
  timeout: "default",
};

const STATUS_OPTIONS: ApprovalStatus[] = [
  "pending",
  "approved",
  "rejected",
  "modified",
  "timeout",
];

const REASON_COLOR: Record<string, string> = {
  policy_gate: "geekblue",
  missing_info: "gold",
  ambiguous_requirement: "gold",
  approach_choice: "cyan",
  risk_confirmation: "volcano",
};

function formatWaiting(t: (key: string, opts?: Record<string, unknown>) => string, iso: string): string {
  const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60_000));
  if (minutes < 60) return t("approvals_page.waiting_minutes", { count: minutes });
  return t("approvals_page.waiting_hours", { count: Math.round(minutes / 60) });
}

type Urgency = "danger" | "warning" | "secondary";

/**
 * Time remaining until an approval auto-rejects, with an urgency level so the
 * queue surfaces what's about to be swept: ``danger`` inside 2h (or already
 * past ``timeout_at`` — the sweep runs on a ~5m cadence), ``warning`` inside
 * 6h, otherwise ``secondary``.
 */
function formatTimeLeft(
  t: (key: string, opts?: Record<string, unknown>) => string,
  iso: string,
): { text: string; level: Urgency } {
  const minutes = Math.round((new Date(iso).getTime() - Date.now()) / 60_000);
  if (minutes <= 0) return { text: t("approvals_page.timed_out"), level: "danger" };
  if (minutes < 120)
    return { text: t("approvals_page.time_left_minutes", { count: minutes }), level: "danger" };
  return {
    text: t("approvals_page.time_left_hours", { count: Math.round(minutes / 60) }),
    level: minutes < 360 ? "warning" : "secondary",
  };
}

export function ApprovalsList() {
  const { t } = useTranslation();
  const { scope, apiTenantScope } = useTenantScope();
  const [data, setData] = useState<ApprovalList | null>(null);
  const [loading, setLoading] = useState(false);
  const [deciding, setDeciding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<ApprovalStatus>("pending");
  // B-20 approval triage — safety sign-offs and agent clarification
  // questions are different audiences; the queue splits into two tabs.
  const [kindClass, setKindClass] = useState<ApprovalKindClass>("safety");
  // Pending count of the *other* tab (same status filter), so a waiting
  // safety sign-off stays visible while triaging clarifications and vice
  // versa. ``null`` until its probe resolves (badge hidden).
  const [otherCount, setOtherCount] = useState<number | null>(null);
  const [selectedKeys, setSelectedKeys] = useState<Key[]>([]);

  const isCrossTenant = scope === "*";
  const canDecide = statusFilter === "pending" && !isCrossTenant;

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const other: ApprovalKindClass = kindClass === "safety" ? "clarification" : "safety";
      const [result, otherProbe] = await Promise.all([
        listApprovals({ tenantScope: apiTenantScope, status: statusFilter, kindClass }),
        // Badge probe for the inactive tab — total only, one row of payload.
        listApprovals({
          tenantScope: apiTenantScope,
          status: statusFilter,
          kindClass: other,
          limit: 1,
        }),
      ]);
      setData(result);
      setOtherCount(otherProbe.total);
      setSelectedKeys([]);
    } catch (err) {
      const text =
        err instanceof ApiError
          ? `${err.code}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "unknown error";
      setError(text);
    } finally {
      setLoading(false);
    }
  }, [apiTenantScope, statusFilter, kindClass]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const decide = useCallback(
    async (items: ApprovalItem[], decision: "approve" | "reject") => {
      setDeciding(true);
      try {
        const result = await decideApprovals(
          items.map((item) => ({
            thread_id: item.thread_id,
            run_id: item.run_id,
            decision,
          })),
        );
        const failed = result.results.filter((r) => !r.ok);
        if (failed.length === 0) {
          message.success(t("approvals_page.decide_ok", { count: result.succeeded }));
        } else {
          message.warning(
            t("approvals_page.decide_partial", {
              ok: result.succeeded,
              failed: failed.length,
            }),
          );
        }
      } catch (err) {
        const text =
          err instanceof ApiError
            ? `${err.code}: ${err.message}`
            : err instanceof Error
              ? err.message
              : "unknown error";
        message.error(t("approvals_page.decide_failed", { error: text }));
      } finally {
        setDeciding(false);
        await refresh();
      }
    },
    [refresh, t],
  );

  const selectedItems = useMemo(() => {
    const keys = new Set(selectedKeys.map(String));
    return (data?.items ?? []).filter((item) => keys.has(item.run_id));
  }, [data, selectedKeys]);

  // Every pending row on the page — one-click "approve all" target. Page size
  // (20) matches the ``:decide`` batch cap, so one page is one batch call.
  const pendingItems = useMemo(
    () => (data?.items ?? []).filter((item) => item.status === "pending"),
    [data],
  );

  const columns: TableColumnsType<ApprovalItem> = useMemo(
    () => [
      {
        title: t("approvals_page.column_reason"),
        dataIndex: "reason_kind",
        key: "reason_kind",
        width: 170,
        render: (kind: string) => (
          <Tag color={REASON_COLOR[kind] ?? "default"}>{kind}</Tag>
        ),
      },
      {
        title: t("approvals_page.column_action"),
        dataIndex: "action_summary",
        key: "action_summary",
        ellipsis: true,
        render: (summary: string, record) => (
          <Tooltip title={summary}>
            <Link
              to={`/runs/${encodeURIComponent(record.thread_id)}/${encodeURIComponent(record.run_id)}`}
              data-testid={`approval-link-${record.run_id}`}
            >
              {summary}
            </Link>
          </Tooltip>
        ),
      },
      {
        title: t("approvals_page.column_waiting"),
        dataIndex: "requested_at",
        key: "requested_at",
        width: 130,
        render: (iso: string) => (
          <Text type="secondary" style={{ fontSize: 12 }}>
            {formatWaiting(t, iso)}
          </Text>
        ),
      },
      {
        title: t("approvals_page.column_timeout"),
        dataIndex: "timeout_at",
        key: "timeout_at",
        width: 130,
        render: (iso: string) => {
          const { text, level } = formatTimeLeft(t, iso);
          return (
            <Tooltip title={new Date(iso).toLocaleString()}>
              <Text type={level} style={{ fontSize: 12 }}>
                {text}
              </Text>
            </Tooltip>
          );
        },
      },
      {
        title: t("approvals_page.column_status"),
        dataIndex: "status",
        key: "status",
        width: 110,
        render: (status: string) => (
          <Tag color={STATUS_COLOR[status] ?? "default"}>{status}</Tag>
        ),
      },
      {
        title: t("approvals_page.column_actions"),
        key: "actions",
        width: 190,
        render: (_: unknown, record) =>
          record.status === "pending" && canDecide ? (
            <Space size={6}>
              <Popconfirm
                title={t("approvals_page.confirm_approve")}
                onConfirm={() => void decide([record], "approve")}
                okText={t("approvals_page.approve")}
                cancelText={t("common.cancel")}
              >
                <Button
                  size="small"
                  type="primary"
                  icon={<CheckCircle2 size={13} strokeWidth={1.5} />}
                  disabled={deciding}
                  data-testid={`approval-approve-${record.run_id}`}
                >
                  {t("approvals_page.approve")}
                </Button>
              </Popconfirm>
              <Popconfirm
                title={t("approvals_page.confirm_reject")}
                onConfirm={() => void decide([record], "reject")}
                okText={t("approvals_page.reject")}
                okButtonProps={{ danger: true }}
                cancelText={t("common.cancel")}
              >
                <Button
                  size="small"
                  danger
                  icon={<XCircle size={13} strokeWidth={1.5} />}
                  disabled={deciding}
                  data-testid={`approval-reject-${record.run_id}`}
                >
                  {t("approvals_page.reject")}
                </Button>
              </Popconfirm>
            </Space>
          ) : (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {record.decided_by ?? "—"}
            </Text>
          ),
      },
    ],
    [t, canDecide, decide, deciding],
  );

  return (
    <div>
      <PageHeader
        icon={<ListChecks size={18} strokeWidth={1.5} />}
        title={t("approvals_page.page_title")}
        actions={
          <>
            {isCrossTenant && (
              <Tag
                icon={<Globe2 size={12} strokeWidth={1.5} />}
                color="purple"
                data-testid="cross-tenant-banner"
              >
                {t("approvals_page.cross_tenant_banner")}
              </Tag>
            )}
            {canDecide && pendingItems.length > 0 && (
              <Popconfirm
                title={t("approvals_page.confirm_approve_all", { count: pendingItems.length })}
                onConfirm={() => void decide(pendingItems, "approve")}
                okText={t("approvals_page.approve")}
                cancelText={t("common.cancel")}
              >
                <Button
                  size="small"
                  type="primary"
                  icon={<CheckCircle2 size={13} strokeWidth={1.5} />}
                  disabled={deciding}
                  data-testid="approvals-approve-all"
                >
                  {t("approvals_page.approve_all_page", { count: pendingItems.length })}
                </Button>
              </Popconfirm>
            )}
            <Select<ApprovalStatus>
              value={statusFilter}
              onChange={(v) => setStatusFilter(v)}
              style={{ width: 150 }}
              aria-label={t("approvals_page.filter_status")}
              data-testid="approvals-status-filter"
              options={STATUS_OPTIONS.map((s) => ({ value: s, label: s }))}
            />
            <button
              type="button"
              onClick={refresh}
              disabled={loading}
              aria-label={t("common.refresh")}
              data-testid="approvals-refresh"
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                padding: "4px 10px",
                border: "1px solid var(--ew-border-default)",
                borderRadius: 6,
                background: "var(--ew-surface-raised)",
                color: "var(--ew-text-primary)",
                fontSize: 13,
                cursor: loading ? "wait" : "pointer",
              }}
            >
              <RefreshCw size={14} strokeWidth={1.5} />
              {loading ? t("common.loading") : t("common.refresh")}
            </button>
          </>
        }
      />

      <Tabs
        activeKey={kindClass}
        onChange={(k) => setKindClass(k as ApprovalKindClass)}
        data-testid="approvals-kind-tabs"
        items={[
          {
            key: "safety",
            label: (
              <span data-testid="approvals-tab-safety">
                {t("approvals_page.tab_safety")}
                {kindClass !== "safety" && (otherCount ?? 0) > 0 && (
                  <Badge count={otherCount ?? 0} size="small" style={{ marginLeft: 6 }} />
                )}
              </span>
            ),
          },
          {
            key: "clarification",
            label: (
              <span data-testid="approvals-tab-clarification">
                {t("approvals_page.tab_clarification")}
                {kindClass !== "clarification" && (otherCount ?? 0) > 0 && (
                  <Badge count={otherCount ?? 0} size="small" style={{ marginLeft: 6 }} />
                )}
              </span>
            ),
          },
        ]}
      />

      {error !== null && (
        <Alert
          type="error"
          showIcon
          message={t("approvals_page.failed_to_load")}
          description={error}
          style={{ marginBottom: 16 }}
          data-testid="approvals-error"
        />
      )}

      {canDecide && selectedItems.length > 0 && (
        <Space style={{ marginBottom: 12 }} data-testid="approvals-batch-bar">
          <Text type="secondary">
            {t("approvals_page.selected_count", { count: selectedItems.length })}
          </Text>
          <Popconfirm
            title={t("approvals_page.confirm_batch_approve", { count: selectedItems.length })}
            onConfirm={() => void decide(selectedItems, "approve")}
            okText={t("approvals_page.approve")}
            cancelText={t("common.cancel")}
          >
            <Button
              size="small"
              type="primary"
              disabled={deciding}
              data-testid="approvals-batch-approve"
            >
              {t("approvals_page.batch_approve")}
            </Button>
          </Popconfirm>
          <Popconfirm
            title={t("approvals_page.confirm_batch_reject", { count: selectedItems.length })}
            onConfirm={() => void decide(selectedItems, "reject")}
            okText={t("approvals_page.reject")}
            okButtonProps={{ danger: true }}
            cancelText={t("common.cancel")}
          >
            <Button size="small" danger disabled={deciding} data-testid="approvals-batch-reject">
              {t("approvals_page.batch_reject")}
            </Button>
          </Popconfirm>
        </Space>
      )}

      <Table<ApprovalItem>
        columns={columns}
        dataSource={data?.items ?? []}
        rowKey={(record) => record.run_id}
        loading={loading}
        rowSelection={
          canDecide
            ? {
                selectedRowKeys: selectedKeys,
                onChange: setSelectedKeys,
                // aria-labels keep the bare checkboxes axe-clean (the
                // batch endpoint caps at 20 decisions per call anyway).
                getCheckboxProps: (record) => ({
                  disabled: deciding,
                  "aria-label": t("approvals_page.select_row", {
                    summary: record.action_summary,
                  }),
                }),
                columnTitle: (originNode) => (
                  <span aria-label={t("approvals_page.select_all")}>{originNode}</span>
                ),
              }
            : undefined
        }
        pagination={{
          total: data?.total ?? 0,
          showSizeChanger: false,
          pageSize: 20,
        }}
        locale={{
          emptyText: <Empty description={t("approvals_page.empty")} />,
        }}
        data-testid="approvals-table"
      />

      <p style={{ marginTop: 16, fontSize: 12, color: "var(--ew-text-tertiary)" }}>
        {t("approvals_page.modify_hint")}
      </p>
    </div>
  );
}
