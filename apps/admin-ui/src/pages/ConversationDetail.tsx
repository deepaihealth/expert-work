/**
 * Conversation detail — one ``(agent, user, session=thread)`` conversation
 * (``docs/design/conversation-centric-ia.md``).
 *
 * Shows the conversation summary (agent / user / status / token rollup /
 * last active) and the transcript — the debug console's own read-only turn
 * timeline (``components/console/*``, PR-B Task 3): ``useHistoryTurns``
 * pairs the checkpoint's user/assistant text (``GET /v1/sessions/{id}/
 * messages``) 1:1 with the thread's runs and replays each run's persisted
 * event stream when its row scrolls into view, so every LLM step, tool
 * call, timing and token rollup is visible here — not just the two
 * endpoints of a turn. A per-turn footer link ("查看运行") drills into the
 * existing per-run detail (``/runs/{thread}/{run}``) with its raw event
 * stream, approval history, and Langfuse deep link; there is no separate
 * runs table any more. Whenever the pairing is unavailable (count
 * mismatch, failed lookup, cross-tenant drill-in) the page degrades to the
 * flat M1.5 message block it has always rendered.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Card, Empty, Segmented, Skeleton, Space, Tag, Typography } from "antd";
import { useLocation, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import type { ApprovalItem } from "../api/approvals";
import { downloadArtifact } from "../api/artifacts";
import { ApiError } from "../api/client";
import { getConversation, type ConversationDetail as ConversationDetailModel } from "../api/conversations";
import { streamRunEvents } from "../api/runs";
import { computeSessionStats } from "../api/session_stats";
import { getSessionMessages, type HistoryMessage, type SseEvent } from "../api/sessions";
import type { FireNowResult } from "../api/triggers";
import { useAuth } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { buildConsoleTurns, runIdOf, statsInputOf } from "../components/console/console_turns";
import { PlanCard } from "../components/console/PlanCard";
import { StatsBar } from "../components/console/StatsBar";
import { Transcript } from "../components/console/Transcript";
import { TrajectoryView } from "../components/console/TrajectoryView";
import type { ConsoleTurn, TurnTiming } from "../components/console/types";
import { usePlanCard } from "../components/console/usePlanCard";
import type { FocusRequest } from "../components/console/use_trajectory_state";
import { ViewPane, type ConsoleView } from "../components/console/ViewPane";
import { CommentarySegmentLine } from "../components/turn/CommentarySegmentLine";
import { downloadJson } from "../components/turn/download_json";
import type { Turn } from "../components/turn/types";
import { useHistoryTurns } from "../components/turn/useHistoryTurns";
import type { LiveStep } from "./agent_detail/playground/useTokenStream";
import { concreteTenantScope, useTenantScope } from "../tenant/TenantScopeContext";
import { formatCompact } from "../utils/runFormat";

const { Text } = Typography;

const STATUS_COLOR: Record<string, string> = {
  active: "processing",
  paused: "warning",
  completed: "success",
  failed: "error",
  cancelled: "default",
  archived: "default",
  pending: "default",
};

// PR-B Task 3 — this page never streams a live turn: stable module-level
// constants so ``Transcript``/``TrajectoryView`` never see a fresh
// reference every render (PR-A.2 teaching — a fresh ``Map``/array each
// render is exactly "a live frame landed" to their internal effects).
const EMPTY_LIVE_BY_STEP: ReadonlyMap<number, LiveStep> = new Map();
const EMPTY_TASK_RESULTS: readonly FireNowResult[] = [];
const EMPTY_FLAT_HISTORY: readonly HistoryMessage[] = [];
const EMPTY_LIVE_TURNS: readonly Turn[] = [];
const EMPTY_TIMINGS: Readonly<Record<string, TurnTiming>> = {};
/** The read-only page never dispatches an approval decision — ``Transcript``
 *  requires the callback regardless (no turn ever carries a pending
 *  ``approval`` here, since history turns are always synthesised with
 *  ``approval: null``). */
function noopOnDecide(_turnId: string, _approval: ApprovalItem, _decision: "approve" | "reject"): void {}

export function ConversationDetail() {
  const { t } = useTranslation();
  const location = useLocation();
  const { threadId } = useParams<{ threadId: string }>();
  // Langfuse has no per-tenant isolation, so the per-turn deep link is
  // platform-ops only — same gate as the playground.
  const { identity } = useAuth();
  const isSystemAdmin = identity?.isSystemAdmin ?? false;
  // A system_admin drilling in from the cross-tenant browser carries the
  // thread's tenant here — without it the scope-aware detail endpoint
  // resolves to the caller's home tenant and 404s the foreign thread. The
  // "*" aggregate maps to undefined (the endpoint types tenant_id as
  // ``UUID | None`` and 422s a literal "*").
  const { apiTenantScope } = useTenantScope();

  const [convo, setConvo] = useState<ConversationDetailModel | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // M1.5 transcript — ``null`` means unavailable (e.g. a cross-tenant
  // thread the messages endpoint can't scope to): hide the panel rather
  // than erroring the page. ``[]`` renders an explicit empty state.
  const [messages, setMessages] = useState<HistoryMessage[] | null>(null);

  // 只读调试台 — count-paired lazy console turns + each one's replay state.
  const {
    turns: historyTurns,
    loads: historyLoads,
    registerRow: registerHistoryRow,
    loadRuns: loadHistoryRuns,
    load: loadHistory,
    reset: resetHistory,
  } = useHistoryTurns();
  const [exportingId, setExportingId] = useState<string | null>(null);
  // R9 — 中栏轮高亮:``null`` = 跟随最新一轮;点轮块 / 脚注「查看轨迹」置为该轮。
  const [selectedTurnKey, setSelectedTurnKey] = useState<string | null>(null);
  const [view, setView] = useState<ConsoleView>("chat");
  // §九「联动」—— 脚注 / 过程条发起的一次性跨视图定位请求(见 PlaygroundTab)。
  const [focusRequest, setFocusRequest] = useState<FocusRequest | null>(null);
  const focusNonceRef = useRef(0);

  const refresh = useCallback(async () => {
    if (!threadId) return;
    setLoading(true);
    setError(null);
    let loaded: ConversationDetailModel | null = null;
    try {
      loaded = await getConversation(threadId, concreteTenantScope(apiTenantScope));
      setConvo(loaded);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? `${err.code}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "unknown error",
      );
    } finally {
      setLoading(false);
    }
    // Best-effort, independent of the summary fetch — a transcript
    // failure must never take down the operational view. The thread's
    // tenant_id rides along so a system_admin's cross-tenant drill-in
    // reads the right tenant's checkpoint.
    try {
      const msgs = await getSessionMessages(threadId, loaded?.tenant_id);
      setMessages(Array.isArray(msgs) ? msgs : null);
    } catch {
      setMessages(null);
    }
  }, [threadId, apiTenantScope]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // D3 (lifted, W2) — the replay/runs endpoints are scope-aware now, so the
  // rich transcript rebuilds for any tenant's thread; the thread's own
  // ``tenant_id`` rides along (authoritative even from the "*" aggregate).
  // M-3 — ``convo`` can still be the *previous* threadId's row for one
  // render after the params change (this effect's own dependency), so pin
  // the rebuild to the thread actually being viewed rather than whatever
  // the last fetch resolved.
  const viewedConvo = convo !== null && convo.thread_id === threadId ? convo : null;

  useEffect(() => {
    if (!threadId || viewedConvo === null) {
      // H-1 — the route's param can change without remounting this
      // component (react-router keys by route id, not ``:threadId``), so a
      // thread switch must explicitly clear whatever turn cards the
      // previous thread built here; otherwise they linger over the new
      // thread's own content indefinitely.
      resetHistory();
      return;
    }
    void loadHistory(threadId, viewedConvo.tenant_id);
  }, [threadId, viewedConvo, loadHistory, resetHistory]);

  // One ordered timeline over the lazily-rebuilt history turns (Task 5 view
  // model, shared with the playground) — this page never has live turns.
  const convoRuns = convo?.runs;
  const consoleTurns = useMemo(() => {
    const built = buildConsoleTurns({
      historyTurns,
      historyLoads,
      liveTurns: EMPTY_LIVE_TURNS,
      timings: EMPTY_TIMINGS,
    });
    // C1 — ``buildConsoleTurns`` falls back to the raw terminal status
    // string for a failed history turn's error text; this page has a
    // richer source (the run list's own ``error`` field) that PlaygroundTab
    // has no equivalent of, so prefer it here the same way the pre-Task-3
    // page did.
    return built.map((turn) =>
      turn.turn.status === "error" && turn.runId
        ? {
            ...turn,
            turn: {
              ...turn.turn,
              error: convoRuns?.find((r) => r.run_id === turn.runId)?.error ?? turn.turn.error,
            },
          }
        : turn,
    );
  }, [historyTurns, historyLoads, convoRuns]);
  const stats = useMemo(
    () => computeSessionStats(consoleTurns.map(statsInputOf), null),
    [consoleTurns],
  );
  // usePlanCard's live overlay expects this-session streamed events; this
  // page has none, so feed it the events of turns that have actually
  // replayed (loadState "done") instead — the GET baseline already covers
  // the persisted plan, this only catches a plan frame inside a just-loaded
  // run's own stream.
  const loadedEvents = useMemo(
    () =>
      consoleTurns
        .filter((turn) => turn.loadState === "done")
        .flatMap((turn) => turn.turn.events),
    [consoleTurns],
  );
  const { plan, loaded: planLoaded } = usePlanCard({
    threadId: threadId ?? null,
    liveEvents: loadedEvents,
  });

  // Export a turn's full event stream as JSON — same contract as the
  // playground's toolbar button (prefer the authoritative persisted replay,
  // fall back to the frames already in the card, always download something).
  const handleExport = useCallback(
    async (turn: Turn) => {
      const runId = runIdOf(turn.events);
      setExportingId(turn.id);
      let events: SseEvent[] = turn.events;
      let source: "backend" | "client" = "client";
      try {
        if (threadId && runId) {
          const collected: SseEvent[] = [];
          for await (const frame of streamRunEvents(threadId, runId)) {
            collected.push(frame);
            if (frame.event === "end") break;
          }
          if (collected.length > 0) {
            events = collected;
            source = "backend";
          }
        }
      } catch {
        // Best-effort — fall back to the frames already assigned.
      } finally {
        setExportingId(null);
      }
      downloadJson(`expert-work-events-${runId ?? turn.id}.json`, {
        run_id: runId,
        thread_id: threadId ?? null,
        input: turn.input,
        source,
        exported_at: new Date().toISOString(),
        events,
      });
    },
    [threadId],
  );

  // Artifacts the agent registered during a replayed turn. ``user_id`` is the
  // tenant-admin governance target (H.8-F1) — the conversation's own user, not
  // the operator reading it.
  const conversationUserId = convo?.user_id ?? null;
  const handleDownloadArtifact = useCallback(
    async (name: string) => {
      try {
        await downloadArtifact(name, conversationUserId ?? undefined, concreteTenantScope(apiTenantScope));
      } catch {
        // Swallow — same rationale as the playground: the artifact may have
        // been deleted, and a toast here would need the App message API.
      }
    },
    [conversationUserId, apiTenantScope],
  );

  // §九「联动」—— 脚注「查看轨迹」:切「轨迹」tab + 选中该轮最后一条 ASSISTANT
  // 记录(``rowId: null``,由 ``use_trajectory_state`` 落到具体记录上)。
  const handleInspectTurn = useCallback((key: string) => {
    focusNonceRef.current += 1;
    setSelectedTurnKey(key);
    setView("trajectory");
    setFocusRequest({ turnKey: key, rowId: null, nonce: focusNonceRef.current });
  }, []);

  // §九「联动」—— 过程条每行「检查」:同上,但带上该行的 id。
  const handleInspectRow = useCallback((turnKey: string, rowId: string) => {
    focusNonceRef.current += 1;
    setSelectedTurnKey(turnKey);
    setView("trajectory");
    setFocusRequest({ turnKey, rowId, nonce: focusNonceRef.current });
  }, []);

  // 轨迹账本翻页 / 扩窗时主动回放这批历史 run。
  const handleEnsureLoaded = useCallback(
    (runIds: readonly string[]): Promise<void> =>
      threadId ? loadHistoryRuns(runIds, threadId) : Promise.resolve(),
    [threadId, loadHistoryRuns],
  );

  // 每轮脚注「查看运行」深链 —— 钻入既有的单 run 详情页(原始事件流 / 审批
  // 历史 / Langfuse 深链),该页仍是可写的(拍板 R2:仅本页全链只读)。
  const runHrefOf = useCallback(
    (turn: ConsoleTurn): string | null =>
      threadId && turn.runId
        ? `/runs/${encodeURIComponent(threadId)}/${encodeURIComponent(turn.runId)}`
        : null,
    [threadId],
  );

  if (!threadId) {
    return <Empty description="Missing :threadId" style={{ marginTop: 80 }} />;
  }
  if (loading) {
    return <Skeleton active paragraph={{ rows: 6 }} />;
  }
  if (error !== null || convo === null) {
    return (
      <Alert
        type="error"
        showIcon
        message={t("conversations_detail.failed_to_load")}
        description={error ?? "conversation not found"}
        data-testid="conversation-detail-error"
      />
    );
  }

  const tk = convo.tokens;
  // Back link: every entry point passes its exact URL (filters + page) via
  // ``location.state.from`` plus a ``fromLabel`` — going back restores that
  // view verbatim. Any in-app path is accepted (a single leading slash rules
  // out protocol-relative and absolute URLs); the label falls back to the
  // section the path belongs to, so a caller that predates ``fromLabel``
  // still reads right. Without state at all, fall back to the agent's
  // conversations tab (agent-bound thread) or the agents index.
  const navState = location.state as { from?: unknown; fromLabel?: unknown } | null;
  const stateFrom = navState?.from;
  const fromPath =
    typeof stateFrom === "string" && stateFrom.startsWith("/") && !stateFrom.startsWith("//")
      ? stateFrom
      : undefined;
  const stateLabel = navState?.fromLabel;
  const fromLabel =
    typeof stateLabel === "string" && stateLabel
      ? stateLabel
      : fromPath?.startsWith("/users")
        ? t("nav.users")
        : fromPath?.startsWith("/agents")
          ? t("nav.agents")
          : t("nav.conversations");
  const backTo = fromPath
    ? { label: fromLabel, to: fromPath }
    : convo.agent_name && convo.agent_version
      ? {
          label: convo.agent_name,
          to: `/agents/${encodeURIComponent(convo.agent_name)}/${encodeURIComponent(
            convo.agent_version,
          )}/conversations`,
        }
      : { label: t("nav.agents"), to: "/agents" };

  return (
    <div data-testid="conversation-detail-root">
      <PageHeader
        title={convo.title ?? t("conversations_page.untitled")}
        backTo={backTo}
        subtitle={
          <Space size={8} align="center" wrap>
            <Tag color={STATUS_COLOR[convo.status] ?? "default"} bordered={false}>
              {convo.status}
            </Tag>
            {convo.agent_name && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                {convo.agent_name}
                {convo.agent_version ? ` v${convo.agent_version}` : ""}
              </Text>
            )}
            <span>
              {t("conversations_detail.thread_label")}:{" "}
              <Text code style={{ fontSize: 12 }}>
                {convo.thread_id.slice(0, 12)}…
              </Text>
            </span>
          </Space>
        }
      />

      <Card size="small" title={t("conversations_detail.summary_title")}>
        <dl
          style={{
            display: "grid",
            gridTemplateColumns: "140px 1fr",
            rowGap: 8,
            columnGap: 16,
            margin: 0,
            fontSize: 13,
          }}
        >
          <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("conversations_detail.user")}</dt>
          <dd className="mono" style={{ margin: 0 }}>
            {convo.user_id ?? "—"}
          </dd>
          <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("conversations_page.column_runs")}</dt>
          <dd style={{ margin: 0 }}>
            {convo.run_count}
            {convo.error_count > 0 &&
              ` · ${t("conversations_page.error_count", { count: convo.error_count })}`}
          </dd>
          <dt style={{ color: "var(--ew-text-tertiary)" }}>
            {t("conversations_detail.tokens")}
          </dt>
          <dd style={{ margin: 0 }}>
            {tk && tk.total_tokens > 0 ? (
              <span data-testid="conversation-tokens">
                {t("conversations_detail.tokens_value", {
                  total: formatCompact(tk.total_tokens),
                  input: tk.input_tokens,
                  output: tk.output_tokens,
                  calls: tk.llm_calls,
                })}
              </span>
            ) : (
              "—"
            )}
          </dd>
          <dt style={{ color: "var(--ew-text-tertiary)" }}>
            {t("conversations_detail.models")}
          </dt>
          <dd style={{ margin: 0 }}>
            {tk && tk.models.length > 0 ? tk.models.join(", ") : "—"}
          </dd>
          <dt style={{ color: "var(--ew-text-tertiary)" }}>
            {t("conversations_page.column_last_active")}
          </dt>
          <dd style={{ margin: 0 }}>
            {convo.last_run_at ? new Date(convo.last_run_at).toLocaleString() : "—"}
          </dd>
        </dl>
      </Card>

      {(messages !== null || historyTurns !== null) && (
        <Card
          size="small"
          title={t("conversations_detail.messages_title")}
          style={{ marginTop: 16 }}
          data-testid="conversation-messages"
        >
          {historyTurns !== null ? (
            // 只读调试台 — the same Transcript/TrajectoryView/StatsBar/PlanCard
            // family the playground uses (PR-B Task 3), all wired ``readOnly``.
            <>
              {stats.turns > 0 && (
                <div style={{ marginBottom: 8 }} data-testid="console-stats-row">
                  <StatsBar stats={stats} isSystemAdmin={isSystemAdmin} />
                </div>
              )}
              <Segmented
                value={view}
                onChange={(value) => setView(value as ConsoleView)}
                size="small"
                aria-label={t("console.view_aria")}
                data-testid="console-view-tabs"
                options={[
                  {
                    value: "chat",
                    label: <span data-testid="console-view-tab-chat">{t("console.view_chat")}</span>,
                  },
                  {
                    value: "trajectory",
                    label: (
                      <span data-testid="console-view-tab-trajectory">
                        {t("console.view_trajectory")}
                      </span>
                    ),
                  },
                ]}
              />
              <div style={{ marginTop: 8 }}>
                <ViewPane view="chat" active={view === "chat"}>
                  <Transcript
                    turns={consoleTurns}
                    flatHistory={EMPTY_FLAT_HISTORY}
                    taskResults={EMPTY_TASK_RESULTS}
                    threadId={threadId}
                    selectedKey={selectedTurnKey}
                    onSelectTurn={setSelectedTurnKey}
                    onInspectTurn={handleInspectTurn}
                    onInspectRow={handleInspectRow}
                    streamTurnKey={null}
                    liveByStep={EMPTY_LIVE_BY_STEP}
                    registerHistoryRow={registerHistoryRow}
                    rate={null}
                    isSystemAdmin={isSystemAdmin}
                    readOnly
                    isTenantSwitched={false}
                    onDecide={noopOnDecide}
                    deciding={false}
                    onExport={handleExport}
                    exportingKey={exportingId}
                    onDownloadArtifact={handleDownloadArtifact}
                    runHrefOf={runHrefOf}
                  />
                </ViewPane>
                <ViewPane view="trajectory" active={view === "trajectory"}>
                  <TrajectoryView
                    turns={consoleTurns}
                    threadId={threadId}
                    agentName={convo.agent_name ?? ""}
                    agentVersion={convo.agent_version ?? ""}
                    readOnly
                    streamTurnKey={null}
                    liveByStep={EMPTY_LIVE_BY_STEP}
                    running={false}
                    visible={view === "trajectory"}
                    isSystemAdmin={isSystemAdmin}
                    focusRequest={focusRequest}
                    onEnsureLoaded={handleEnsureLoaded}
                  />
                </ViewPane>
              </div>
              <div style={{ marginTop: 12 }}>
                <PlanCard plan={plan} loaded={planLoaded} running={false} readOnly />
              </div>
            </>
          ) : messages === null || messages.length === 0 ? (
            <Empty description={t("conversations_detail.messages_empty")} />
          ) : (
            <div style={{ maxHeight: 480, overflowY: "auto" }}>
              {messages.map((m, i) => (
                <div
                  key={i}
                  data-testid={`conversation-message-${i}`}
                  style={{
                    display: "grid",
                    gridTemplateColumns: "72px 1fr",
                    columnGap: 12,
                    padding: "8px 0",
                    borderTop: i === 0 ? "none" : "1px solid var(--ew-border-default)",
                    fontSize: 13,
                  }}
                >
                  <Tag
                    color={m.role === "user" ? "cyan" : "purple"}
                    style={{ marginInlineEnd: 0, height: "fit-content", justifySelf: "start" }}
                  >
                    {m.role === "user"
                      ? t("conversations_detail.role_user")
                      : t("conversations_detail.role_assistant")}
                  </Tag>
                  {m.role === "assistant" && m.channel === "commentary" ? (
                    // Minor#3 — same fix as PlaygroundTab's flat degradation
                    // path: don't render a commentary row full-width as if
                    // it were the answer body.
                    <CommentarySegmentLine
                      text={m.content}
                      label={t("playground.segment_commentary")}
                    />
                  ) : (
                    <div style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                      {m.content}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
