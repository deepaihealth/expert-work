/**
 * Run detail page — Stream H.1b PR 3.
 *
 * Real fetch of ``GET /v1/sessions/{thread_id}/runs/{run_id}`` —
 * Mini-ADR J-41's durable run row, augmented with any pending
 * approval. ``POST .../resume`` lets a reviewer approve or reject a
 * pending approval inline.
 *
 * Route shape changed from the demo's ``/runs/:runId`` to the real
 * ``/runs/:threadId/:runId`` because the backend identity is the
 * tuple ``(thread_id, run_id)`` — there is no flat ``GET /v1/runs``
 * endpoint yet (Mini-ADR J-41 keeps the per-thread shape).
 *
 * Trace / span visualisation is intentionally out of scope here —
 * spans live in OTLP traces, not in the control-plane API surface.
 * H.4 wires the Tempo / Grafana embed.
 */
import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Card, Empty, Skeleton, Space, Tag, Typography } from "antd";
import { useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { PageHeader } from "../components/PageHeader";
import {
  getRun,
  type RunDetail as RunDetailModel,
  type RunStatus,
} from "../api/runs";
import { useStatusPolling } from "../hooks/useStatusPolling";
import { concreteTenantScope, useTenantScope } from "../tenant/TenantScopeContext";
import { ApprovalCard } from "./run_detail/ApprovalCard";
import { EventStreamPanel } from "./run_detail/EventStreamPanel";
import { PlanPanel } from "./run_detail/PlanPanel";
import { RunSummaryPanel } from "./run_detail/RunSummaryPanel";
import { TraceToolbar } from "./run_detail/TraceToolbar";

const { Text } = Typography;

const STATUS_COLOR: Record<RunStatus, string> = {
  pending: "default",
  queued: "default",
  running: "processing",
  paused: "warning",
  awaiting_approval: "warning",
  success: "success",
  completed: "success",
  error: "error",
  failed: "error",
  timeout: "error",
  interrupted: "default",
  cancelled: "default",
  unknown: "default",
};

export function RunDetail() {
  const { t } = useTranslation();
  const { threadId, runId } = useParams<{ threadId: string; runId: string }>();
  // Track C W2 — 切入态读透传:getRun 带 ?tenant_id=(组件内直取)。
  const { apiTenantScope } = useTenantScope();

  const [run, setRun] = useState<RunDetailModel | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // CM-8 — bumped on every poll tick so the PlanPanel re-fetches on the
  // same cadence without owning its own timer.
  const [pollTick, setPollTick] = useState(0);

  /** Silent refresh — polled by ``useStatusPolling`` so the Skeleton
   *  flicker only happens on the initial fetch and explicit user
   *  refreshes, not every 3 seconds. */
  const refreshSilent = useCallback(async () => {
    if (!threadId || !runId) return;
    try {
      setRun(await getRun(threadId, runId, concreteTenantScope(apiTenantScope)));
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `${err.code}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "unknown error";
      setError(msg);
    }
  }, [threadId, runId, apiTenantScope]);

  const refresh = useCallback(async () => {
    if (!threadId || !runId) return;
    setLoading(true);
    setError(null);
    try {
      setRun(await getRun(threadId, runId, concreteTenantScope(apiTenantScope)));
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
  }, [threadId, runId, apiTenantScope]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Mini-ADR H-7 — 3s poll while the run is active and the tab is
  // visible. Terminal status stops the timer; the page-visibility gate
  // is inside the hook.
  useStatusPolling({
    status: run?.status ?? null,
    onTick: () => {
      setPollTick((n) => n + 1);
      void refreshSilent();
    },
  });

  if (!threadId || !runId) {
    return <Empty description="Missing :threadId or :runId" style={{ marginTop: 80 }} />;
  }

  if (loading) {
    return <Skeleton active paragraph={{ rows: 8 }} />;
  }

  if (error !== null || run === null) {
    return (
      <Alert
        type="error"
        showIcon
        message={t("run_detail.failed_to_load")}
        description={error ?? "run not found"}
        data-testid="run-detail-error"
      />
    );
  }

  const approval = run.pending_approval;

  return (
    <div data-testid="run-detail-root">
      <PageHeader
        title={`${run.run_id.slice(0, 12)}…`}
        backTo={{
          // Up one level in the drill-down: run → its conversation.
          label: t("run_detail.back_to_conversation"),
          to: `/conversations/${encodeURIComponent(run.thread_id)}`,
        }}
        subtitle={
          <Space size={8} align="center" wrap>
            <Tag color={STATUS_COLOR[run.status] ?? "default"} bordered={false}>
              {run.status}
            </Tag>
            <span>
              {t("run_detail.thread_label")}:{" "}
              <Text code style={{ fontSize: 12 }}>
                {run.thread_id.slice(0, 12)}…
              </Text>
            </span>
          </Space>
        }
        actions={
          <Button onClick={() => void refresh()} loading={loading}>
            {t("common.refresh")}
          </Button>
        }
      />

      {approval !== null && (
        <ApprovalCard
          threadId={threadId}
          runId={runId}
          approval={approval}
          onResolved={() => void refresh()}
        />
      )}

      <Card title={t("run_detail.run_metadata")} size="small">
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
          <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("run_detail.run_id")}</dt>
          <dd className="mono" style={{ margin: 0 }}>{run.run_id}</dd>
          <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("run_detail.thread_id")}</dt>
          <dd className="mono" style={{ margin: 0 }}>{run.thread_id}</dd>
          <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("run_detail.status")}</dt>
          <dd style={{ margin: 0 }}>{run.status}</dd>
        </dl>
      </Card>

      <RunSummaryPanel run={run} />

      <PlanPanel threadId={threadId} runStatus={run.status} pollTick={pollTick} />

      <div style={{ marginTop: 16 }}>
        <TraceToolbar traceId={run.trace_id ?? null} />
      </div>

      <EventStreamPanel threadId={threadId} runId={runId} />
    </div>
  );
}
