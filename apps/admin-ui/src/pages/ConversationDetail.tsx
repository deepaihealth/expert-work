/**
 * Conversation detail — one ``(agent, user, session=thread)`` conversation
 * (``docs/design/conversation-centric-ia.md``).
 *
 * Shows the conversation summary (agent / user / status / token rollup /
 * last active), the transcript, and its run list; each run drills into the
 * existing per-run detail (``/runs/{thread}/{run}``) with its event stream,
 * approval card, and Langfuse deep link.
 *
 * The transcript is the debug console's own read-only turn timeline
 * (``components/turn``): ``useHistoryTurns`` pairs the checkpoint's
 * user/assistant text (``GET /v1/sessions/{id}/messages``) 1:1 with the
 * thread's runs and replays each run's persisted event stream when its row
 * scrolls into view, so every LLM step, tool call, timing and token rollup is
 * visible here — not just the two endpoints of a turn. Whenever that pairing
 * is unavailable (count mismatch, failed lookup, cross-tenant drill-in) the
 * page degrades to the flat M1.5 message block it has always rendered.
 */
import { useCallback, useEffect, useState } from "react";
import { Alert, Card, Empty, Skeleton, Space, Table, Tag, Tooltip, Typography } from "antd";
import type { TableColumnsType } from "antd";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { downloadArtifact } from "../api/artifacts";
import { ApiError } from "../api/client";
import {
  getConversation,
  type ConversationDetail as ConversationDetailModel,
  type ConversationRun,
} from "../api/conversations";
import { streamRunEvents } from "../api/runs";
import { getSessionMessages, type HistoryMessage, type SseEvent } from "../api/sessions";
import { useAuth } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { downloadJson } from "../components/turn/download_json";
import { CommentarySegmentLine, runIdOf, TurnCard } from "../components/turn/TurnCard";
import type { Turn } from "../components/turn/types";
import { useHistoryTurns } from "../components/turn/useHistoryTurns";
import { concreteTenantScope, useTenantScope } from "../tenant/TenantScopeContext";
import { formatCompact, formatDuration } from "../utils/runFormat";

const { Text } = Typography;

const STATUS_COLOR: Record<string, string> = {
  active: "processing",
  paused: "warning",
  completed: "success",
  failed: "error",
  cancelled: "default",
  archived: "default",
  pending: "default",
  running: "processing",
  success: "success",
  error: "error",
  timeout: "error",
  interrupted: "default",
};

export function ConversationDetail() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const { threadId } = useParams<{ threadId: string }>();
  // Langfuse has no per-tenant isolation, so TurnCard's per-turn deep link is
  // platform-ops only — same gate as the playground.
  const { identity } = useAuth();
  const isSystemAdmin = identity?.isSystemAdmin ?? false;
  const homeTenantId = identity?.homeTenantId ?? null;
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

  // 只读调试台 — count-paired lazy turn cards + each one's replay state.
  const {
    turns: historyTurns,
    loads: historyLoads,
    registerRow: registerHistoryRow,
    load: loadHistory,
    reset: resetHistory,
  } = useHistoryTurns();
  // Per-page seed for each turn's event view (the playground persists its own
  // default under a playground-scoped key; this page just starts on timeline).
  const [eventView, setEventView] = useState<"timeline" | "raw" | "exact">(
    "timeline",
  );
  const [exportingId, setExportingId] = useState<string | null>(null);

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

  // D3 — the replay/runs endpoints take no ``tenant_id`` parameter, so a
  // system_admin drilling into a foreign tenant's thread could only ever
  // collect 404s. Rebuild the rich transcript for a thread in the caller's own
  // tenant; everyone else keeps the flat message block (which does carry the
  // tenant on the wire). Also skips the whole rebuild until identity resolves.
  const sameTenant =
    convo !== null &&
    homeTenantId !== null &&
    convo.tenant_id === homeTenantId &&
    // M-3 — ``convo`` can still be the *previous* threadId's row for one
    // render after the params change (this effect's own dependency), so
    // pin the check to the thread actually being viewed rather than
    // whatever the last fetch resolved.
    convo.thread_id === threadId;

  useEffect(() => {
    if (!threadId || !sameTenant) {
      // H-1 — the route's param can change without remounting this
      // component (react-router keys by route id, not ``:threadId``), so a
      // same-tenant → cross-tenant switch must explicitly clear whatever
      // turn cards the previous thread built here; otherwise they linger
      // over the new thread's own content indefinitely.
      resetHistory();
      return;
    }
    void loadHistory(threadId);
  }, [threadId, sameTenant, loadHistory, resetHistory]);

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
        await downloadArtifact(name, conversationUserId ?? undefined);
      } catch {
        // Swallow — same rationale as the playground: the artifact may have
        // been deleted, and a toast here would need the App message API.
      }
    },
    [conversationUserId],
  );

  const columns: TableColumnsType<ConversationRun> = [
    {
      title: t("runs_page.column_run_id"),
      dataIndex: "run_id",
      key: "run_id",
      width: 180,
      render: (id: string) => (
        <Tooltip title={id}>
          <Text code style={{ fontSize: 12 }}>
            {id.slice(0, 8)}…
          </Text>
        </Tooltip>
      ),
    },
    {
      title: t("runs_page.column_status"),
      dataIndex: "status",
      key: "status",
      width: 120,
      render: (status: string, record) => {
        const tag = <Tag color={STATUS_COLOR[status] ?? "default"}>{status}</Tag>;
        if (!record.error) return tag;
        return (
          <Tooltip title={record.error}>
            <span data-testid={`conversation-run-error-${record.run_id}`}>{tag}</span>
          </Tooltip>
        );
      },
    },
    {
      title: t("runs_page.column_duration"),
      key: "duration",
      width: 100,
      render: (_: unknown, record) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {formatDuration(t, record.created_at, record.finished_at)}
        </Text>
      ),
    },
    {
      title: t("runs_page.column_tokens"),
      key: "tokens",
      width: 90,
      render: (_: unknown, record) => {
        const tk = record.tokens;
        if (!tk || tk.total_tokens === 0) return <Text type="secondary">—</Text>;
        return <Text style={{ fontSize: 12 }}>{formatCompact(tk.total_tokens)}</Text>;
      },
    },
    {
      title: t("conversations_detail.column_started"),
      dataIndex: "created_at",
      key: "created_at",
      width: 190,
      render: (iso: string) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {new Date(iso).toLocaleString()}
        </Text>
      ),
    },
    {
      // The whole row has been clickable since M2, but nothing said so. The
      // explicit link makes the drill-in discoverable (and keyboard reachable).
      // M-6 — title stays visually blank (matches every other action-only
      // column in this codebase, e.g. SettingsApiKeys/KnowledgeAdmin), but an
      // empty <th> still reads as unnamed to a screen reader; ``onHeaderCell``
      // is antd's own escape hatch for attaching attributes straight to the
      // header cell without rendering anything visible.
      title: "",
      key: "open",
      width: 96,
      onHeaderCell: () => ({ "aria-label": t("conversations_detail.view_run") }),
      render: (_: unknown, record) => (
        <Link
          to={`/runs/${encodeURIComponent(record.thread_id)}/${encodeURIComponent(
            record.run_id,
          )}`}
          data-testid={`conversation-run-open-${record.run_id}`}
          // I-1 — the link sits inside a clickable <tr> (``onRow.onClick``
          // below navigates to the same URL). Without stopping propagation,
          // one click fires both: the Link's own navigation AND the row's
          // ``navigate()``, pushing the same URL twice — one "back" from the
          // run page then lands on the run page again, not here.
          onClick={(e) => e.stopPropagation()}
        >
          {t("conversations_detail.view_run")}
        </Link>
      ),
    },
  ];

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
            /* 只读调试台 — one lazy TurnCard per paired run; the row's ref
               registers with the shared IntersectionObserver so its event
               stream replays only once it scrolls into view. */
            <div
              data-testid="conversation-turns"
              // No list-level height cap: each TurnCard's answer block caps
              // itself (#11), and an outer 480px cap only produced nested
              // scrollbars fighting over the wheel (I1).
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 12,
              }}
            >
              {historyTurns.map((h, idx) => {
                const load = historyLoads[h.runId] ?? {
                  state: "pending" as const,
                  events: [],
                };
                return (
                  <div key={h.key} ref={registerHistoryRow(h.runId, threadId)}>
                    <TurnCard
                      turn={{
                        id: h.key,
                        input: h.input,
                        attachments: [],
                        events: load.events,
                        // #10 — surface the run's real terminal state instead
                        // of a hardcoded "done"; other statuses (interrupted/
                        // paused/…) keep the normal rendering. No onRetry
                        // here — the read-only page never dispatches runs.
                        status:
                          h.status === "error" || h.status === "timeout"
                            ? "error"
                            : "done",
                        // C1 — feed the failure banner: prefer the run list's
                        // real error text, fall back to the raw status string
                        // so the Alert never renders an empty frame.
                        error:
                          h.status === "error" || h.status === "timeout"
                            ? (convo.runs.find((r) => r.run_id === h.runId)
                                ?.error ?? h.status)
                            : null,
                        approval: null,
                      }}
                      turnSeq={idx}
                      initialEventView={eventView}
                      onViewChange={setEventView}
                      threadId={threadId}
                      onDownloadArtifact={handleDownloadArtifact}
                      rate={null}
                      onDecide={() => {}}
                      deciding={false}
                      onExport={handleExport}
                      exporting={exportingId === h.key}
                      isSystemAdmin={isSystemAdmin}
                      readOnly
                      loadState={load.state}
                      fallbackLines={h.fallbackLines}
                    />
                  </div>
                );
              })}
            </div>
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

      <Card
        size="small"
        title={t("conversations_detail.runs_title")}
        style={{ marginTop: 16 }}
        data-testid="conversation-runs"
      >
        <Table<ConversationRun>
          size="small"
          columns={columns}
          dataSource={convo.runs}
          rowKey={(record) => record.run_id}
          pagination={false}
          onRow={(record) => ({
            onClick: () =>
              navigate(
                `/runs/${encodeURIComponent(record.thread_id)}/${encodeURIComponent(record.run_id)}`,
              ),
            style: { cursor: "pointer" },
          })}
          locale={{ emptyText: <Empty description={t("conversations_detail.runs_empty")} /> }}
          data-testid="conversation-runs-table"
        />
      </Card>
    </div>
  );
}
