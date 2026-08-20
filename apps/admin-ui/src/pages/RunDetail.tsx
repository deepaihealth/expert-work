/**
 * Run detail page — Stream H.1b PR 3, redone by 调试台重设计 PR-B Task 4.
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
 * PR-B Task 4 — the old ``PlanPanel`` / ``TraceToolbar`` /
 * ``EventStreamPanel`` trio is retired here in favour of the console
 * shell's own ``PlanCard`` + ``TrajectoryView`` (single-run trajectory):
 * ``useHistoryTurns`` pairs the thread's ``/messages`` with its
 * ``/runs`` the same way ``ConversationDetail`` does, then ``loadRuns``
 * replays only this page's ``runId`` (not the whole thread), and
 * ``buildConsoleTurns`` + a filter narrow the timeline to that one run.
 * A count-mismatch pairing failure (``turns`` stays ``null``) degrades
 * the trajectory area to an explicit empty state instead of crashing.
 *
 * ``PlanPanel``'s 3s poll refresh is intentionally NOT carried over —
 * ``usePlanCard`` already covers a live run via the replayed run's own
 * plan-frame stream, with the PUT echo as the last writer after a save
 * (拍板 R4).
 *
 * ``onFireResult`` is deliberately not wired up: the trajectory's
 * "立即触发" button already shows its own inline delivered/pending/
 * failed status (``ToolTimeline.tsx``'s local ``delivery`` state) —
 * ``onFireResult`` only exists to additionally mirror that result into
 * a page-level list (PlaygroundTab's ``taskResults``), and this page has
 * no such list to mirror it into.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Button, Card, Empty, Skeleton, Space, Tag, Typography } from "antd";
import { useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { getConversation } from "../api/conversations";
import {
  getRun,
  type RunDetail as RunDetailModel,
  type RunStatus,
} from "../api/runs";
import { useAuth } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { buildConsoleTurns } from "../components/console/console_turns";
import { PlanCard } from "../components/console/PlanCard";
import { TrajectoryView } from "../components/console/TrajectoryView";
import type { ConsoleTurn, TurnTiming } from "../components/console/types";
import { usePlanCard } from "../components/console/usePlanCard";
import type { SseEvent } from "../api/sessions";
import type { Turn } from "../components/turn/types";
import { useHistoryTurns } from "../components/turn/useHistoryTurns";
import type { LiveStep } from "./agent_detail/playground/useTokenStream";
import { useStatusPolling } from "../hooks/useStatusPolling";
import { concreteTenantScope, useTenantScope } from "../tenant/TenantScopeContext";
import { ApprovalCard } from "./run_detail/ApprovalCard";
import { RunSummaryPanel } from "./run_detail/RunSummaryPanel";

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

// Same terminal/non-terminal split as ``useStatusPolling``'s own
// ``ACTIVE_STATUSES`` (copied, not imported — that module doesn't export
// it, and this page needs the same boolean for ``PlanCard``/
// ``TrajectoryView``'s ``running`` prop, not just the polling timer).
const ACTIVE_RUN_STATUSES: ReadonlySet<RunStatus> = new Set<RunStatus>([
  "pending",
  "running",
  "paused",
  "queued",
  "awaiting_approval",
]);

// Stable module-level empties — this page never has live (this-session)
// turns or per-step token timings, only lazily-replayed history; passing
// fresh literals here would defeat every memo/effect keyed on them.
const NO_LIVE_TURNS: readonly Turn[] = [];
const NO_TIMINGS: Readonly<Record<string, TurnTiming>> = {};
const NO_EVENTS: readonly SseEvent[] = [];
const NO_LIVE_BY_STEP: ReadonlyMap<number, LiveStep> = new Map();

export function RunDetail() {
  const { t } = useTranslation();
  const { threadId, runId } = useParams<{ threadId: string; runId: string }>();
  // Track C W2 — 切入态读透传:getRun 带 ?tenant_id=(组件内直取)。
  const { apiTenantScope } = useTenantScope();
  // Langfuse deep link + the Schema tab's tool-catalog gate — same source
  // as the playground/conversation pages.
  const { identity } = useAuth();
  const isSystemAdmin = identity?.isSystemAdmin ?? false;

  const [run, setRun] = useState<RunDetailModel | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // TrajectoryView's Schema tab keys its ``useAgentTools`` fetch on these —
  // best-effort (conversation lookup failing just means an empty Schema
  // tab, not a broken page), so they default to "".
  const [agentName, setAgentName] = useState("");
  const [agentVersion, setAgentVersion] = useState("");

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
    onTick: () => void refreshSilent(),
  });

  // Best-effort — the conversation lookup only backs the Schema tab's
  // agent/version key, so a failure here must not block the page.
  useEffect(() => {
    if (!threadId) return;
    let cancelled = false;
    getConversation(threadId, concreteTenantScope(apiTenantScope))
      .then((convo) => {
        if (cancelled) return;
        setAgentName(convo.agent_name ?? "");
        setAgentVersion(convo.agent_version ?? "");
      })
      .catch(() => {
        // Schema tab just renders its own "not in the tool catalog" empty
        // state with agentName/agentVersion === "".
      });
    return () => {
      cancelled = true;
    };
  }, [threadId, apiTenantScope]);

  // Single-run trajectory — pair the thread's /messages with its /runs the
  // same way ConversationDetail does, then replay only this page's runId
  // (not the whole thread).
  const { turns: historyTurns, loads: historyLoads, load: loadHistory, loadRuns } =
    useHistoryTurns();
  const [historyLoaded, setHistoryLoaded] = useState(false);

  useEffect(() => {
    if (!threadId) return;
    setHistoryLoaded(false);
    void loadHistory(threadId).then(() => setHistoryLoaded(true));
  }, [threadId, loadHistory]);

  useEffect(() => {
    if (!threadId || !runId || historyTurns === null) return;
    if (!historyTurns.some((h) => h.runId === runId)) return;
    void loadRuns([runId], threadId);
  }, [threadId, runId, historyTurns, loadRuns]);

  const trajectoryTurns: ConsoleTurn[] = useMemo(
    () =>
      buildConsoleTurns({
        historyTurns,
        historyLoads,
        liveTurns: NO_LIVE_TURNS,
        timings: NO_TIMINGS,
      }).filter((ct) => ct.runId === runId),
    [historyTurns, historyLoads, runId],
  );

  const handleEnsureLoaded = useCallback(
    (runIds: readonly string[]): Promise<void> =>
      threadId ? loadRuns(runIds, threadId) : Promise.resolve(),
    [threadId, loadRuns],
  );

  const running = run !== null && ACTIVE_RUN_STATUSES.has(run.status);

  // usePlanCard's three-source precedence (GET baseline → live plan frames
  // → PUT echo) reads this run's own already-loaded events for the "live"
  // overlay — there is no this-session live turn on this page.
  const liveEvents = historyLoads[runId ?? ""]?.events ?? NO_EVENTS;
  const {
    plan,
    loaded: planLoaded,
    save: savePlan,
  } = usePlanCard({ threadId: threadId ?? null, liveEvents });

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

      <div style={{ marginTop: 16 }}>
        <PlanCard plan={plan} loaded={planLoaded} running={running} onSave={savePlan} />
      </div>

      <div style={{ marginTop: 16 }}>
        {!historyLoaded ? null : historyTurns === null ? (
          <Card size="small">
            <Empty description={t("run_detail.trajectory_pairing_failed")} />
          </Card>
        ) : (
          <TrajectoryView
            turns={trajectoryTurns}
            threadId={threadId}
            agentName={agentName}
            agentVersion={agentVersion}
            streamTurnKey={null}
            liveByStep={NO_LIVE_BY_STEP}
            running={running}
            isSystemAdmin={isSystemAdmin}
            focusRequest={null}
            onEnsureLoaded={handleEnsureLoaded}
          />
        )}
      </div>
    </div>
  );
}
