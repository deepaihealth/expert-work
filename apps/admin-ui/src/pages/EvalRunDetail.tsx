/**
 * Eval Run detail page — P1-S2.5-FE.
 *
 * Fetches ``GET /v1/eval-runs/{id}`` (status + summary) and
 * ``GET /v1/eval-runs/{id}/cases`` (per-capability results). Polls while
 * the run is still ``queued`` / ``running`` so the worker's progress
 * lands without a manual refresh. Mirrors ``RunDetail``'s metadata-card +
 * panel layout.
 */
import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Card, Empty, Skeleton, Table, Tag, Typography } from "antd";
import type { TableColumnsType } from "antd";
import { useParams, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import type { RunStatus } from "../api/runs";
import { useStatusPolling } from "../hooks/useStatusPolling";
import { concreteTenantScope, useTenantScope } from "../tenant/TenantScopeContext";
import { PageHeader } from "../components/PageHeader";
import {
  getEvalRun,
  getEvalRunCases,
  type EvalCaseResult,
  type EvalRunRecord,
  type EvalRunStatus,
} from "../api/eval_runs";

const { Text } = Typography;

const STATUS_COLOR: Record<EvalRunStatus, string> = {
  queued: "default",
  running: "processing",
  passed: "success",
  failed: "error",
  error: "error",
};

function summaryLabel(summary: Record<string, unknown> | null): string | null {
  if (summary === null) return null;
  const pass = summary.pass_count;
  const total = summary.total;
  if (typeof pass === "number" && typeof total === "number") {
    return `${pass}/${total}`;
  }
  return null;
}

function scoresLabel(scores: Record<string, number> | null): string {
  if (scores === null) return "—";
  const parts = Object.entries(scores).map(([k, v]) => `${k}=${v}`);
  return parts.length > 0 ? parts.join(" · ") : "—";
}

export function EvalRunDetail() {
  const { t } = useTranslation();
  const { runId } = useParams<{ runId: string }>();
  // Cross-tenant W3/W4 — detail reads take a concrete UUID only ("*" 400s).
  // Priority: the ``?tenant_id=`` query param (set by the EvalRunsList "*"
  // aggregate row-jump — the ambient scope is "*" there, which collapses to
  // undefined) wins over the ambient switched-in scope.
  const { apiTenantScope } = useTenantScope();
  const [searchParams] = useSearchParams();
  const readScope =
    searchParams.get("tenant_id") ?? concreteTenantScope(apiTenantScope);

  const [run, setRun] = useState<EvalRunRecord | null>(null);
  const [cases, setCases] = useState<EvalCaseResult[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const toMessage = (err: unknown): string =>
    err instanceof ApiError
      ? `${err.code}: ${err.message}`
      : err instanceof Error
        ? err.message
        : "unknown error";

  const load = useCallback(
    async (silent: boolean) => {
      if (!runId) return;
      if (!silent) {
        setLoading(true);
        setError(null);
      }
      try {
        const [runData, casesData] = await Promise.all([
          getEvalRun(runId, readScope),
          getEvalRunCases(runId, readScope),
        ]);
        setRun(runData);
        setCases(casesData.cases);
      } catch (err) {
        setError(toMessage(err));
      } finally {
        if (!silent) setLoading(false);
      }
    },
    [runId, readScope],
  );

  useEffect(() => {
    void load(false);
  }, [load]);

  // queued / running share the run-status active set → polls; passed /
  // failed / error are terminal → the hook drops the timer.
  useStatusPolling({
    status: (run?.status ?? null) as RunStatus | null,
    onTick: () => void load(true),
  });

  if (!runId) {
    return <Empty description="Missing :runId" style={{ marginTop: 80 }} />;
  }

  if (loading) {
    return <Skeleton active paragraph={{ rows: 8 }} />;
  }

  if (error !== null || run === null) {
    return (
      <Alert
        type="error"
        showIcon
        message={t("eval_run_detail.failed_to_load")}
        description={error ?? t("eval_run_detail.not_found")}
        data-testid="eval-detail-error"
      />
    );
  }

  const summary = summaryLabel(run.summary);

  const columns: TableColumnsType<EvalCaseResult> = [
    {
      title: t("eval_run_detail.col_case_id"),
      dataIndex: "case_id",
      key: "case_id",
      width: 200,
    },
    {
      title: t("eval_run_detail.col_capability"),
      dataIndex: "capability",
      key: "capability",
    },
    {
      title: t("eval_run_detail.col_result"),
      dataIndex: "passed",
      key: "passed",
      width: 110,
      render: (passed: boolean) => (
        <Tag color={passed ? "success" : "error"}>
          {passed ? t("eval_run_detail.result_passed") : t("eval_run_detail.result_failed")}
        </Tag>
      ),
    },
    {
      title: t("eval_run_detail.col_scores"),
      dataIndex: "scores",
      key: "scores",
      render: (scores: Record<string, number> | null) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {scoresLabel(scores)}
        </Text>
      ),
    },
  ];

  return (
    <div data-testid="eval-detail-root">
      <PageHeader
        title={`${run.id.slice(0, 12)}…`}
        backTo={{ label: t("nav.eval"), to: "/eval-runs" }}
        subtitle={
          <Tag color={STATUS_COLOR[run.status] ?? "default"} bordered={false}>
            {run.status}
          </Tag>
        }
        actions={
          <Button onClick={() => void load(false)} loading={loading}>
            {t("common.refresh")}
          </Button>
        }
      />

      <Card title={t("eval_run_detail.run_metadata")} size="small">
        <dl
          style={{
            display: "grid",
            gridTemplateColumns: "160px 1fr",
            rowGap: 8,
            columnGap: 16,
            margin: 0,
            fontSize: 13,
          }}
        >
          <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("eval_run_detail.run_id")}</dt>
          <dd className="mono" style={{ margin: 0 }}>
            {run.id}
          </dd>
          <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("eval_run_detail.suite")}</dt>
          <dd style={{ margin: 0 }}>{run.suite}</dd>
          <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("eval_run_detail.status")}</dt>
          <dd style={{ margin: 0 }}>{run.status}</dd>
          <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("eval_run_detail.triggered_by")}</dt>
          <dd style={{ margin: 0 }}>{run.triggered_by}</dd>
          <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("eval_run_detail.created_at")}</dt>
          <dd style={{ margin: 0 }}>{new Date(run.created_at).toLocaleString()}</dd>
          {summary !== null && (
            <>
              <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("eval_run_detail.summary")}</dt>
              <dd style={{ margin: 0 }}>{summary}</dd>
            </>
          )}
        </dl>
      </Card>

      <Card
        title={t("eval_run_detail.cases_title")}
        size="small"
        style={{ marginTop: 16 }}
      >
        <Table<EvalCaseResult>
          columns={columns}
          dataSource={cases}
          rowKey={(record) => record.id}
          pagination={false}
          locale={{ emptyText: <Empty description={t("eval_run_detail.cases_empty")} /> }}
          data-testid="eval-cases-table"
        />
      </Card>
    </div>
  );
}
