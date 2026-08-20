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
// it, and this page needs the same boolean for ``TrajectoryView``'s
// ``running`` prop, not just the polling timer). NOT used for the plan
// card's lock any more — see ``PLAN_LOCKED_STATUSES`` below (I-1).
const ACTIVE_RUN_STATUSES: ReadonlySet<RunStatus> = new Set<RunStatus>([
  "pending",
  "running",
  "paused",
  "queued",
  "awaiting_approval",
]);

// I-1 — plan-edit lock, narrower than ``ACTIVE_RUN_STATUSES`` above: mirrors
// the backend's write guard (``control_plane/api/plan.py``'s
// ``_WRITE_BLOCKED_STATUSES = frozenset({RunStatus.PENDING,
// RunStatus.RUNNING})``) — a plan write 409s only while the agent is about
// to race it with its own ``update_plan``/projection, i.e. while the
// thread's latest run is queued or actively live. ``queued`` is this
// frontend's pre-H.3 alias for ``pending`` (see ``RunStatus`` above), so it
// joins the lock too. ``paused`` / ``awaiting_approval`` are deliberately
// excluded — a run parked at a gate isn't about to overwrite an edit, so
// editing then is safe; this restores the pre-PR-B ``PlanPanel``'s lock
// scope, which this page's PR-B rewrite had widened by mistake to the
// broader ``ACTIVE_RUN_STATUSES`` set (a functional regression, I-1).
const PLAN_LOCKED_STATUSES: ReadonlySet<RunStatus> = new Set<RunStatus>([
  "pending",
  "running",
  "queued",
]);

// Stable module-level empties — this page never has live (this-session)
// turns or per-step token timings, only lazily-replayed history; passing
// fresh literals here would defeat every memo/effect keyed on them.
const NO_LIVE_TURNS: readonly Turn[] = [];
const NO_TIMINGS: Readonly<Record<string, TurnTiming>> = {};
const NO_EVENTS: readonly SseEvent[] = [];
const NO_LIVE_BY_STEP: ReadonlyMap<number, LiveStep> = new Map();
// Ruling 3 / I-2 — fed to ``usePlanCard`` instead of a terminal run's own
// (possibly stale) replayed events; see the ``liveEvents`` derivation below.
const NO_PLAN_EVENTS: readonly SseEvent[] = [];

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
  // M-8 — the conversation's own ``tenant_id``, authoritative for a
  // system_admin's cross-tenant drill-in the same way ConversationDetail
  // .tsx's ``viewedConvo.tenant_id`` is (the ambient scope resolves to the
  // caller's home tenant, not necessarily the thread's). ``null`` = the
  // lookup below hasn't settled yet; once settled, ``tenantId`` is the
  // thread's real tenant, or ``undefined`` when the lookup failed (history
  // then falls back to the ambient scope, same as before this fix — this
  // lookup stays best-effort for the page as a whole).
  //
  // Ruling 4 (PR-B follow-up) — the tenant is tagged with the ``threadId``
  // it was resolved for. On a same-page thread switch the params flip a
  // render before this state resets, and an untagged value would let the
  // history effect below fire ``loadHistory(newThread, oldThreadsTenant)``
  // — a cross-tenant read under the wrong scope. Today every entry point
  // remounts the page (new Route key), so the race is latent; the tag makes
  // it impossible rather than merely unexercised.
  const [convoTenant, setConvoTenant] = useState<{
    threadId: string;
    tenantId: string | undefined;
  } | null>(null);

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

  // Best-effort — the conversation lookup backs the Schema tab's
  // agent/version key and (M-8) the history-load effect's tenant scope
  // below; a failure here must not block the page (Schema tab degrades,
  // history falls back to the ambient scope).
  useEffect(() => {
    if (!threadId) return;
    let cancelled = false;
    setConvoTenant(null);
    // Same-page thread switch: don't let the Schema tab keep fetching the
    // previous thread's agent catalog while the new lookup is pending.
    setAgentName("");
    setAgentVersion("");
    getConversation(threadId, concreteTenantScope(apiTenantScope))
      .then((convo) => {
        if (cancelled) return;
        setAgentName(convo.agent_name ?? "");
        setAgentVersion(convo.agent_version ?? "");
        setConvoTenant({ threadId, tenantId: convo.tenant_id });
      })
      .catch(() => {
        if (cancelled) return;
        // Schema tab just renders its own "not in the tool catalog" empty
        // state with agentName/agentVersion === ""; history (M-8) falls
        // back to the ambient scope.
        setConvoTenant({ threadId, tenantId: undefined });
      });
    return () => {
      cancelled = true;
    };
  }, [threadId, apiTenantScope]);

  // Single-run trajectory — pair the thread's /messages with its /runs the
  // same way ConversationDetail does, then replay only this page's runId
  // (not the whole thread). M-8 — waits for the conversation lookup above
  // to settle so it can thread the thread's own tenant_id into
  // ``loadHistory`` (ConversationDetail.tsx:172's precedent — a system_admin
  // cross-tenant drill-in must read the thread's own tenant, not whatever
  // the ambient scope resolves to), instead of firing before it's known.
  const { turns: historyTurns, loads: historyLoads, load: loadHistory, loadRuns } =
    useHistoryTurns();
  const [historyLoaded, setHistoryLoaded] = useState(false);

  useEffect(() => {
    // Reset BEFORE the guard: on a same-page thread switch the guard
    // rejects the stale tenant below, and leaving ``historyLoaded`` true
    // would flash the previous thread's (filtered-empty) trajectory as a
    // misleading "no trajectory" state while the new lookup is pending.
    setHistoryLoaded(false);
    // Ruling 4 — ``convoTenant.threadId !== threadId`` is the race guard:
    // a tenant resolved for a previous thread never scopes this thread's
    // history load (see the state's own comment above).
    if (!threadId || convoTenant === null || convoTenant.threadId !== threadId) return;
    let cancelled = false;
    void loadHistory(threadId, convoTenant.tenantId).then(() => {
      if (!cancelled) setHistoryLoaded(true);
    });
    return () => {
      cancelled = true;
    };
  }, [threadId, convoTenant, loadHistory]);

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
  // I-1 — the plan card's own (narrower) lock; see ``PLAN_LOCKED_STATUSES``.
  const planLocked = run !== null && PLAN_LOCKED_STATUSES.has(run.status);

  // usePlanCard's three-source precedence (GET baseline → live plan frames
  // → PUT echo) reads this run's own already-loaded events for the "live"
  // overlay — there is no this-session live turn on this page. Ruling 3 /
  // I-2 — only while the run is still active: an active run's plan frames
  // are its latest state (spec §二.4's cold-start replay resends the
  // current plan early in the stream), but a terminal run's replayed
  // frames are a historical snapshot of what the plan looked like AT THAT
  // RUN — the thread's plan may have moved on since. The GET baseline
  // (the thread's *current* plan) is the one editable/savable object, so a
  // terminal run must not let its own old plan frames override it (or a
  // Save silently round-trip that stale snapshot instead of the real
  // plan).
  const liveEvents = running ? (historyLoads[runId ?? ""]?.events ?? NO_EVENTS) : NO_PLAN_EVENTS;
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
        <PlanCard plan={plan} loaded={planLoaded} running={planLocked} onSave={savePlan} />
      </div>

      <div style={{ marginTop: 16 }}>
        {!historyLoaded ? null : historyTurns === null ? (
          <Card size="small" data-testid="run-detail-pairing-failed">
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
