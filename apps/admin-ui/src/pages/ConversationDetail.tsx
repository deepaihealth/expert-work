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
import {
  Alert,
  Button,
  Card,
  Empty,
  Popconfirm,
  Segmented,
  Skeleton,
  Space,
  Tag,
  Typography,
  message,
} from "antd";
import { useLocation, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { decideApprovals, type ApprovalItem } from "../api/approvals";
import { downloadArtifact } from "../api/artifacts";
import { ApiError, errMessage } from "../api/client";
import { getConversation, type ConversationDetail as ConversationDetailModel } from "../api/conversations";
import { cancelRun, streamRunEvents } from "../api/runs";
import { reducePlan } from "../api/plan_reducer";
import { computeSessionStats } from "../api/session_stats";
import { getSessionMessages, type HistoryMessage, type SseEvent } from "../api/sessions";
import type { FireNowResult } from "../api/triggers";
import { useAuth } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { buildConsoleTurns, runIdOf, statsInputOf } from "../components/console/console_turns";
import { StatsBar } from "../components/console/StatsBar";
import { Transcript } from "../components/console/Transcript";
import { TrajectoryView } from "../components/console/TrajectoryView";
import type { ConsoleTurn, TurnTiming } from "../components/console/types";
import type { FocusRequest } from "../components/console/use_trajectory_state";
import { ViewPane, type ConsoleView } from "../components/console/ViewPane";
import { CommentarySegmentLine } from "../components/turn/CommentarySegmentLine";
import { downloadJson } from "../components/turn/download_json";
import type { Turn } from "../components/turn/types";
import { useHistoryTurns } from "../components/turn/useHistoryTurns";
import { NON_TERMINAL_RUN_STATUSES } from "../api/runs";
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

  // D-5 — a live-attached tail run just went terminal: silently re-read the
  // summary + run list (statuses/tokens patch in below; a NEW run — someone
  // sent another message, or an approval decide spawned a continuation —
  // grows the run list and triggers a full re-pair).
  const refreshRef = useRef<(opts?: { silent?: boolean }) => Promise<void>>(
    async () => {},
  );
  const handleRunTerminal = useCallback(() => {
    void refreshRef.current({ silent: true });
  }, []);

  // 调试台组件族 — count-paired lazy console turns + each one's replay state;
  // D-5 adds the live attach for non-terminal tail runs.
  const {
    turns: historyTurns,
    loads: historyLoads,
    registerRow: registerHistoryRow,
    loadRuns: loadHistoryRuns,
    load: loadHistory,
    reset: resetHistory,
    patchRuns: patchHistoryRuns,
  } = useHistoryTurns({ onRunTerminal: handleRunTerminal });
  const [deciding, setDeciding] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [exportingId, setExportingId] = useState<string | null>(null);
  // R9 — 中栏轮高亮:``null`` = 跟随最新一轮;点轮块 / 脚注「查看轨迹」置为该轮。
  const [selectedTurnKey, setSelectedTurnKey] = useState<string | null>(null);
  const [view, setView] = useState<ConsoleView>("chat");
  // §九「联动」—— 脚注 / 过程条发起的一次性跨视图定位请求(见 PlaygroundTab)。
  const [focusRequest, setFocusRequest] = useState<FocusRequest | null>(null);
  const focusNonceRef = useRef(0);

  const refresh = useCallback(async (opts: { silent?: boolean } = {}) => {
    if (!threadId) return;
    // D-5 — ``silent`` skips the loading flip: a background status refresh
    // (run just finished / approval decided) must not unmount the whole
    // page into a skeleton while live turns are on screen.
    if (!opts.silent) setLoading(true);
    setError(null);
    let loaded: ConversationDetailModel | null = null;
    try {
      loaded = await getConversation(threadId, concreteTenantScope(apiTenantScope));
      setConvo(loaded);
    } catch (err) {
      // I-1 (终审) — a FAILED silent refresh must not blank the whole page
      // into the error Alert (the render layer short-circuits on ``error``);
      // keep whatever is on screen and let the next refresh try again.
      if (!opts.silent) {
        setError(
          err instanceof ApiError
            ? `${err.code}: ${err.message}`
            : err instanceof Error
              ? err.message
              : "unknown error",
        );
      }
    } finally {
      if (!opts.silent) setLoading(false);
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
  // Render-phase ref write (cf. use_trajectory_state.ts) — the hook's
  // ``onRunTerminal`` closure must always see the LATEST refresh without
  // being a dependency of anything; idempotent under StrictMode双渲染.
  refreshRef.current = refresh;

  // D3 (lifted, W2) — the replay/runs endpoints are scope-aware now, so the
  // rich transcript rebuilds for any tenant's thread; the thread's own
  // ``tenant_id`` rides along (authoritative even from the "*" aggregate).
  // M-3 — ``convo`` can still be the *previous* threadId's row for one
  // render after the params change (this effect's own dependency), so pin
  // the rebuild to the thread actually being viewed rather than whatever
  // the last fetch resolved.
  const viewedConvo = convo !== null && convo.thread_id === threadId ? convo : null;
  // D-5 — key the rebuild on (thread, tenant), NOT on the ``convo`` object:
  // every silent refresh produces a fresh object, and re-pairing on each one
  // would flatten a thread whose paused run's continuation just finished
  // (mid-thread paused → honest degradation; ROADMAP D-7). Growth-triggered
  // re-pairs are handled explicitly below.
  const viewedReady = viewedConvo !== null;
  const viewedTenantId = viewedConvo?.tenant_id;

  // D-5 (终审 I-4) — one growth-triggered re-pair per ``convo.runs``
  // INSTANCE: each refresh hands a fresh array, so a transiently-stale
  // ``listThreadRuns`` read gets another chance on the next refresh, while a
  // stable disagreement (same instance re-rendering) cannot loop.
  const repairAttemptRef = useRef<readonly unknown[] | null>(null);

  useEffect(() => {
    if (!threadId || !viewedReady) {
      // H-1 — the route's param can change without remounting this
      // component (react-router keys by route id, not ``:threadId``), so a
      // thread switch must explicitly clear whatever turn cards the
      // previous thread built here; otherwise they linger over the new
      // thread's own content indefinitely.
      resetHistory();
      repairAttemptRef.current = null;
      return;
    }
    void loadHistory(threadId, viewedTenantId);
  }, [threadId, viewedReady, viewedTenantId, loadHistory, resetHistory]);

  const convoRuns = convo?.runs;

  // D-5 — a fresh run-list read after the initial pairing: a NEW run
  // (another user message, or an approval decide's continuation) re-pairs
  // the whole thread; otherwise just patch status/tokens onto the existing
  // turns (identity-stable when nothing changed).
  useEffect(() => {
    if (!threadId || !convoRuns) return;
    // M-13 — the degraded (null) state also gets one attempt per refresh:
    // a thread can become pairable again once its run list moves on.
    if (historyTurns === null || convoRuns.length > historyTurns.length) {
      if (repairAttemptRef.current !== convoRuns) {
        repairAttemptRef.current = convoRuns;
        void loadHistory(threadId, viewedTenantId);
      }
      return;
    }
    patchHistoryRuns(
      convoRuns.map((r) => ({ runId: r.run_id, status: r.status, tokens: r.tokens })),
    );
  }, [threadId, convoRuns, historyTurns, viewedTenantId, loadHistory, patchHistoryRuns]);

  // D-5 — non-terminal tail runs attach eagerly (live view should not wait
  // for the row to scroll into the viewport). ``loadHistoryRuns`` skips
  // already-started replays, so this is idempotent across re-renders.
  useEffect(() => {
    if (!threadId || historyTurns === null) return;
    const tail = historyTurns
      .filter((h) => NON_TERMINAL_RUN_STATUSES.has(h.status))
      .map((h) => h.runId);
    if (tail.length > 0) void loadHistoryRuns(tail, threadId);
  }, [threadId, historyTurns, loadHistoryRuns]);

  // One ordered timeline over the lazily-rebuilt history turns (Task 5 view
  // model, shared with the playground); D-5 — the tail may be live.
  // M-12 — run ids whose approval this operator already decided here:
  // suppress the synthesised card immediately instead of waiting for the
  // refresh + growth re-pair to land (a second click would 409).
  const [decidedRunIds, setDecidedRunIds] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const consoleTurns = useMemo(() => {
    const built = buildConsoleTurns({
      historyTurns,
      historyLoads,
      liveTurns: EMPTY_LIVE_TURNS,
      timings: EMPTY_TIMINGS,
      synthesizeApprovals: true,
    }).map((turn) =>
      turn.turn.approval !== null && turn.runId !== null && decidedRunIds.has(turn.runId)
        ? { ...turn, turn: { ...turn.turn, approval: null } }
        : turn,
    );
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
  }, [historyTurns, historyLoads, convoRuns, decidedRunIds]);
  const stats = useMemo(
    () => computeSessionStats(consoleTurns.map(statsInputOf), null),
    [consoleTurns],
  );
  // BUG-13(修订)— 计划按轮呈现,不再有会话级的单张卡。
  // ``plan`` 是 thread 级的累积状态,一张卡只能显示最后一态:把它吊在
  // 时间线顶端,等于把会话的终态摆在第一轮之上(读者会以为一开始就有这
  // 份计划)。每一轮自己的事件流里带着它跑完时的完整快照(``plan`` 帧携
  // 带整份 ThreadPlan),所以逐轮归约,计划就长在产生它的那一轮里。
  // 未回放的轮(loadState 非 "done")没有事件可归约 → null,不渲染。
  const planOf = useCallback(
    (turn: ConsoleTurn) => reducePlan(turn.turn.events)?.plan ?? null,
    [],
  );

  // D-6 — operations gate: operator/admin, home tenant only (the
  // decide/cancel endpoints act on the caller's own tenant). Backend
  // mirrors: the cancel endpoint carries require("session","write") in this
  // PR; the decide endpoint gets the same gate in #1253 (B-20 ④) — until
  // that merges, this frontend gate is decide's only role check.
  // I-3 (终审) — ``concreteTenantScope`` maps BOTH home and the "*"
  // aggregate to undefined; the decide/cancel endpoints act on the caller's
  // home tenant only, so the gate must be "no tenant switch at all".
  const canOperate =
    apiTenantScope === undefined &&
    ((identity?.roles ?? []).some((r) => r === "admin" || r === "operator") ||
      isSystemAdmin);

  // D-5 — the thread's in-flight tail run (running/pending/queued; NOT
  // paused — a paused run can wait on a human for hours and must not keep
  // the 1s trajectory ticker alive, 终审 M-5). Drives the header's cancel
  // affordance and the running flags below.
  const cancellableRun = useMemo(() => {
    const last = convoRuns?.[convoRuns.length - 1];
    return last !== undefined &&
      (last.status === "running" || last.status === "pending" || last.status === "queued")
      ? last
      : null;
  }, [convoRuns]);
  const runInFlight = cancellableRun !== null;

  // D-6 — decide a paused turn's approval in place, then silently refresh:
  // the continuation run shows up in the run list, the growth effect
  // re-pairs, and the new tail live-attaches.
  const handleDecide = useCallback(
    (_turnId: string, approval: ApprovalItem, decision: "approve" | "reject") => {
      void (async () => {
        setDeciding(true);
        try {
          const result = await decideApprovals([
            { thread_id: approval.thread_id, run_id: approval.run_id, decision },
          ]);
          const item = result.results[0];
          if (item?.ok) {
            // M-12 — hide the card at once; the refresh + growth re-pair
            // catches up with the continuation run behind it.
            setDecidedRunIds((prev) => new Set(prev).add(approval.run_id));
            message.success(t("conversations_detail.decide_ok"));
          } else {
            message.error(item?.error ?? t("conversations_detail.decide_failed"));
          }
        } catch (err) {
          message.error(
            err instanceof Error ? err.message : t("conversations_detail.decide_failed"),
          );
        } finally {
          setDeciding(false);
          void refresh({ silent: true });
        }
      })();
    },
    [refresh, t],
  );

  const handleCancelRun = useCallback(() => {
    const target = cancellableRun;
    if (!threadId || target === null) return;
    void (async () => {
      setCancelling(true);
      try {
        await cancelRun(threadId, target.run_id);
        message.success(t("conversations_detail.cancel_done"));
      } catch (err) {
        message.error(
          err instanceof Error ? err.message : t("conversations_detail.cancel_failed"),
        );
      } finally {
        setCancelling(false);
        void refresh({ silent: true });
      }
    })();
  }, [threadId, cancellableRun, refresh, t]);

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
      } catch (err) {
        // 静默吞错让「下载 404 / 413」表现成「点了没反应」,用户以为产物丢了
        // (2026-08-26 反馈)。toast 带后端 detail(如「太大」「不存在」)。
        message.error(t("artifacts_page.download_failed", { detail: errMessage(err) }));
      }
    },
    [conversationUserId, apiTenantScope, t],
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
        actions={
          canOperate && cancellableRun !== null ? (
            <Popconfirm
              title={t("conversations_detail.cancel_run_confirm")}
              onConfirm={handleCancelRun}
              okText={t("conversations_detail.cancel_run")}
              okButtonProps={{ danger: true }}
              cancelText={t("common.cancel")}
            >
              <Button
                size="small"
                danger
                loading={cancelling}
                data-testid="conversation-cancel-run"
              >
                {t("conversations_detail.cancel_run")}
              </Button>
            </Popconfirm>
          ) : undefined
        }
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
                    allowDecide={canOperate}
                    isTenantSwitched={false}
                    onDecide={handleDecide}
                    deciding={deciding}
                    onExport={handleExport}
                    exportingKey={exportingId}
                    onDownloadArtifact={handleDownloadArtifact}
                    runHrefOf={runHrefOf}
                    planOf={planOf}
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
                    running={runInFlight}
                    visible={view === "trajectory"}
                    isSystemAdmin={isSystemAdmin}
                    focusRequest={focusRequest}
                    onEnsureLoaded={handleEnsureLoaded}
                  />
                </ViewPane>
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
