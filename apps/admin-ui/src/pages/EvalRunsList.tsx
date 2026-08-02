/**
 * Eval Runs list page — P1-S2.5-FE.
 *
 * Operator surface over ``GET /v1/eval-runs`` (home-tenant): a paged
 * table of eval runs + an "Enqueue baseline" button that POSTs a new
 * ``m0_baseline`` run for the resident EvalWorker to drain. Mirrors the
 * ``RunsList`` shell (PageHeader, status filter, error Alert, refresh) so
 * the visual language stays consistent.
 *
 * While any run is still ``queued`` / ``running`` the table polls so the
 * worker's progress shows up without a manual refresh — reusing
 * ``useStatusPolling`` (it keys off the active-status set, which the eval
 * ``queued`` / ``running`` values share with run statuses).
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { App, Alert, Empty, Select, Table, Tag, Tooltip, Typography } from "antd";
import type { TableColumnsType } from "antd";
import { FlaskConical, RefreshCw } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";

import {
  enqueueEvalRun,
  listEvalRuns,
  type EvalRunList,
  type EvalRunRecord,
  type EvalRunStatus,
} from "../api/eval_runs";
import { ApiError } from "../api/client";
import type { RunStatus } from "../api/runs";
import { useStatusPolling } from "../hooks/useStatusPolling";
import { useTenantScope } from "../tenant/TenantScopeContext";
import { useIsTenantSwitched } from "../tenant/useIsTenantSwitched";
import { ReadonlyTooltip } from "../components/ReadonlyTooltip";
import { PageHeader } from "../components/PageHeader";

const { Text } = Typography;

// Tag colour + literal status text so colour is never the only signal
// (axe / WCAG). ``failed`` and ``error`` share red but differ in text.
const STATUS_COLOR: Record<EvalRunStatus, string> = {
  queued: "default",
  running: "processing",
  passed: "success",
  failed: "error",
  error: "error",
};

const STATUS_OPTIONS: EvalRunStatus[] = ["queued", "running", "passed", "failed", "error"];

// Suites an operator may enqueue — mirrors the backend ``_ALLOWED_SUITES``.
// ``m0_baseline`` is the deterministic capability gate; ``adversarial`` /
// ``trace_eval`` drive a real eval agent (11.4/11.5).
const SUITE_OPTIONS = ["m0_baseline", "adversarial", "trace_eval"] as const;
type EvalSuite = (typeof SUITE_OPTIONS)[number];

function summaryLabel(summary: Record<string, unknown> | null): string {
  if (summary === null) return "—";
  const pass = summary.pass_count;
  const total = summary.total;
  if (typeof pass === "number" && typeof total === "number") {
    return `${pass}/${total}`;
  }
  return "—";
}

export function EvalRunsList() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const navigate = useNavigate();
  // Cross-tenant W3 — the list is scope-aware ("*" aggregates every tenant).
  const { apiTenantScope } = useTenantScope();
  // Cross-tenant W3 — 切入态只读:入队是写操作,置灰。
  const isTenantSwitched = useIsTenantSwitched();
  const [data, setData] = useState<EvalRunList | null>(null);
  const [loading, setLoading] = useState(false);
  const [enqueuing, setEnqueuing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<EvalRunStatus | undefined>(undefined);
  const [suiteToEnqueue, setSuiteToEnqueue] = useState<EvalSuite>("m0_baseline");

  const toMessage = (err: unknown): string =>
    err instanceof ApiError
      ? `${err.code}: ${err.message}`
      : err instanceof Error
        ? err.message
        : "unknown error";

  const load = useCallback(
    async (silent: boolean) => {
      if (!silent) {
        setLoading(true);
        setError(null);
      }
      try {
        setData(await listEvalRuns({ status: statusFilter, tenantScope: apiTenantScope }));
      } catch (err) {
        setError(toMessage(err));
      } finally {
        if (!silent) setLoading(false);
      }
    },
    [statusFilter, apiTenantScope],
  );

  useEffect(() => {
    void load(false);
  }, [load]);

  // Poll while any run is still draining so progress shows live. The
  // hook stops on terminal statuses; we feed it a synthesised active
  // marker (it only checks membership in its active-status set).
  const anyActive = (data?.items ?? []).some(
    (r) => r.status === "queued" || r.status === "running",
  );
  useStatusPolling({
    status: anyActive ? ("running" as RunStatus) : null,
    onTick: () => void load(true),
  });

  const onEnqueue = useCallback(async () => {
    setEnqueuing(true);
    try {
      await enqueueEvalRun(suiteToEnqueue);
      message.success(t("eval_runs_page.enqueue_success"));
      setStatusFilter(undefined);
      await load(true);
    } catch (err) {
      message.error(toMessage(err));
    } finally {
      setEnqueuing(false);
    }
  }, [load, message, suiteToEnqueue, t]);

  const columns: TableColumnsType<EvalRunRecord> = useMemo(
    () => [
      {
        title: t("eval_runs_page.column_run_id"),
        dataIndex: "id",
        key: "id",
        width: 200,
        render: (id: string) => (
          <Tooltip title={id}>
            <Text code style={{ fontSize: 12 }}>
              {id.slice(0, 8)}…
            </Text>
          </Tooltip>
        ),
      },
      {
        title: t("eval_runs_page.column_status"),
        dataIndex: "status",
        key: "status",
        width: 130,
        render: (status: EvalRunStatus) => (
          <Tag color={STATUS_COLOR[status] ?? "default"}>{status}</Tag>
        ),
      },
      {
        title: t("eval_runs_page.column_suite"),
        dataIndex: "suite",
        key: "suite",
        render: (suite: string) => <Text strong>{suite}</Text>,
      },
      {
        title: t("eval_runs_page.column_summary"),
        dataIndex: "summary",
        key: "summary",
        width: 120,
        render: (summary: Record<string, unknown> | null) => (
          <Text type="secondary">{summaryLabel(summary)}</Text>
        ),
      },
      {
        title: t("eval_runs_page.column_created"),
        dataIndex: "created_at",
        key: "created_at",
        width: 200,
        render: (iso: string) => (
          <Text type="secondary" style={{ fontSize: 12 }}>
            {new Date(iso).toLocaleString()}
          </Text>
        ),
      },
    ],
    [t],
  );

  return (
    <div>
      <PageHeader
        icon={<FlaskConical size={18} strokeWidth={1.5} />}
        title={t("eval_runs_page.page_title")}
        subtitle={t("eval_runs_page.subtitle")}
        actions={
          <>
            <Select<EvalRunStatus | "all">
              value={statusFilter ?? "all"}
              onChange={(v) => setStatusFilter(v === "all" ? undefined : (v as EvalRunStatus))}
              style={{ width: 160 }}
              aria-label={t("eval_runs_page.filter_status")}
              data-testid="eval-status-filter"
              options={[
                { value: "all", label: t("eval_runs_page.filter_status_all") },
                ...STATUS_OPTIONS.map((s) => ({ value: s, label: s })),
              ]}
            />
            <Select<EvalSuite>
              value={suiteToEnqueue}
              onChange={(v) => setSuiteToEnqueue(v)}
              style={{ width: 150 }}
              aria-label={t("eval_runs_page.suite_label")}
              data-testid="eval-suite-select"
              options={SUITE_OPTIONS.map((s) => ({ value: s, label: s }))}
            />
            <ReadonlyTooltip on={isTenantSwitched}>
              <button
                type="button"
                onClick={() => void onEnqueue()}
                disabled={enqueuing || isTenantSwitched}
                data-testid="eval-enqueue"
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                  padding: "4px 12px",
                  border: "1px solid var(--ew-color-brand-500)",
                  borderRadius: 6,
                  background: "var(--ew-color-brand-500)",
                  color: "#fff",
                  fontSize: 13,
                  cursor: enqueuing ? "wait" : isTenantSwitched ? "not-allowed" : "pointer",
                  opacity: isTenantSwitched ? 0.5 : 1,
                }}
              >
                <FlaskConical size={14} strokeWidth={1.5} />
                {t("eval_runs_page.enqueue")}
              </button>
            </ReadonlyTooltip>
            <button
              type="button"
              onClick={() => void load(false)}
              disabled={loading}
              aria-label={t("common.refresh")}
              data-testid="eval-refresh"
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

      {error !== null && (
        <Alert
          type="error"
          showIcon
          message={t("eval_runs_page.failed_to_load")}
          description={error}
          style={{ marginBottom: 16 }}
          data-testid="eval-error"
        />
      )}

      <Table<EvalRunRecord>
        columns={columns}
        dataSource={data?.items ?? []}
        rowKey={(record) => record.id}
        loading={loading}
        pagination={{ total: data?.total ?? 0, showSizeChanger: false, pageSize: 50 }}
        onRow={(record) => ({
          onClick: () => navigate(`/eval-runs/${encodeURIComponent(record.id)}`),
          style: { cursor: "pointer" },
        })}
        locale={{ emptyText: <Empty description={t("eval_runs_page.empty")} /> }}
        data-testid="eval-table"
      />
    </div>
  );
}
