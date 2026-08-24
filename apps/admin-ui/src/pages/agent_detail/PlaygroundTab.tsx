/**
 * Playground tab — the per-agent debug console, backed by real
 * ``/v1/sessions`` + ``/v1/sessions/{thread_id}/runs`` SSE.
 *
 * 调试台重设计 PR-A Task 19 — this file used to be the whole surface (1476
 * lines of state + JSX). It is now the **session state + assembly layer**:
 * thread / draft / attachment / variable state, the export + artifact
 * requests, and the wiring of ``components/console/*`` into the two-column
 * ``ConsoleShell`` (left: sessions, middle: 对话 / 轨迹 / 工作区 三视图 +
 * 钉底的输入区). The run kernel (live turns, SSE consumption, approvals)
 * moved to ``playground/useRunEngine.ts``; everything visual lives in
 * ``components/console/*`` — see the plan's 「文件结构」 table.
 *
 * PR-A.2 Task 11(spec §九「壳」)—— 右栏检查面板整个退役:轨迹搬进中栏的
 * 「轨迹」tab(``TrajectoryView``),工作区搬进「工作区」tab,``ConsoleShell``
 * 不再传 ``inspect``。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Segmented, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { downloadArtifact } from "../../api/artifacts";
import { ApiError } from "../../api/client";
import { listRateCards, type RateCardRecord } from "../../api/rate_card";
import { streamRunEvents } from "../../api/runs";
import { computeSessionStats } from "../../api/session_stats";
import { createSession, type SseEvent, type ThreadMeta } from "../../api/sessions";
import type { FireNowResult } from "../../api/triggers";
import { uploadDocument, uploadImage } from "../../api/uploads";
import { AttachmentChips } from "../../components/console/AttachmentChips";
import { Composer } from "../../components/console/Composer";
import { ConsoleShell } from "../../components/console/ConsoleShell";
import { buildConsoleTurns, runIdOf, statsInputOf } from "../../components/console/console_turns";
import { PlanCard } from "../../components/console/PlanCard";
import { SessionSidebar, type SessionChange } from "../../components/console/SessionSidebar";
import { StatsBar } from "../../components/console/StatsBar";
import { Transcript } from "../../components/console/Transcript";
import { TrajectoryView } from "../../components/console/TrajectoryView";
import type { TurnTiming } from "../../components/console/types";
import type { FocusRequest } from "../../components/console/use_trajectory_state";
import { usePlanCard } from "../../components/console/usePlanCard";
import { missingRequired, VariablesForm, type PromptVariable } from "../../components/console/VariablesForm";
import { ViewPane, type ConsoleView } from "../../components/console/ViewPane";
import { WorkspacePanel } from "../../components/console/WorkspacePanel";
import { downloadJson } from "../../components/turn/download_json";
import type { Attachment, Turn } from "../../components/turn/types";
import { useHistoryTurns } from "../../components/turn/useHistoryTurns";
import type { AgentDetailResponse } from "../../api/agents";
import { useRunEngine, type RunDraft } from "./playground/useRunEngine";
import { readModel, readPromptJinja, readPromptVariables } from "../../components/manifest-editor/form_model";
import { useAuth } from "../../auth/AuthContext";
import { concreteTenantScope, useTenantScope } from "../../tenant/TenantScopeContext";
import { useIsTenantSwitched } from "../../tenant/useIsTenantSwitched";

const { Text } = Typography;

interface PlaygroundTabProps {
  detail: AgentDetailResponse;
}

const MAIN_HEAD_STYLE: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  padding: "8px 12px",
  borderBottom: "1px solid var(--ew-border-subtle)",
  flexShrink: 0,
};

const COMPOSER_STYLE: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 8,
  padding: 12,
  borderTop: "1px solid var(--ew-border-subtle)",
  flexShrink: 0,
};

export function PlaygroundTab({ detail }: PlaygroundTabProps) {
  const { t } = useTranslation();
  const r = detail.record;
  // Langfuse has no per-tenant isolation (single ClickHouse, all tenants
  // mixed), so the per-turn deep link is platform-ops only — TraceToolbar
  // has the same gate. Cost (rate cards) is system_admin-only too.
  const { identity } = useAuth();
  const isSystemAdmin = identity?.isSystemAdmin ?? false;
  // Track C W2 — 切入态读透传(artifact 下载带 ?tenant_id=);一期只开读:
  // 切入态所有写操作(发送 / 新建会话 / 重试 / 审批 / 反馈)置灰。
  const { apiTenantScope } = useTenantScope();
  const isTenantSwitched = useIsTenantSwitched();

  // Dynamic-Prompt — the agent's declared run-time variables (jinja only).
  // ``record.spec`` IS the full manifest ({apiVersion, kind, metadata, spec}),
  // so it goes to the form_model readers as-is: wrapping it in another
  // ``{ spec }`` shell made ``readPromptJinja`` look at
  // ``manifest.system_prompt`` (undefined) and hid the inputs for every jinja
  // agent (#824 → PR0 of this redesign).
  const promptJinja = readPromptJinja(r.spec);
  const promptVariables: PromptVariable[] = promptJinja
    ? readPromptVariables(r.spec).filter(
        (v): v is { name: string } & typeof v => Boolean(v.name),
      )
    : [];

  const [thread, setThread] = useState<ThreadMeta | null>(null);
  const [threadError, setThreadError] = useState<string | null>(null);
  const [creatingThread, setCreatingThread] = useState(false);
  const [input, setInput] = useState("");
  const [exportingId, setExportingId] = useState<string | null>(null);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [varValues, setVarValues] = useState<Record<string, string>>({});
  const [rate, setRate] = useState<RateCardRecord | null>(null);
  // R9 — 中栏轮高亮:``null`` = 跟最新一轮;点轮块 / 脚注「查看轨迹」置为该轮。
  const [selectedTurnKey, setSelectedTurnKey] = useState<string | null>(null);
  const [view, setView] = useState<ConsoleView>("chat");
  // §九「联动」—— 中栏脚注 / 过程条发起的一次性跨视图定位请求。``nonce`` 是
  // 唯一的「这是一次新请求」信号:同一轮 / 同一行连点两次,turnKey + rowId 都
  // 没变,只有它变了 —— 少了它,关掉详情再点同一处就是空操作。
  const [focusRequest, setFocusRequest] = useState<FocusRequest | null>(null);
  const focusNonceRef = useRef(0);
  // 每轮的流式计时(ttft / 首末 token 时刻)—— 状态栏的 ttft / tok-per-s 用。
  const [timings, setTimings] = useState<Record<string, TurnTiming>>({});
  // 左栏列表刷新触发器 —— 新建会话 / 一轮跑完(标题、最近活动会变)。
  const [sidebarTick, setSidebarTick] = useState(0);
  // Spec 1 PR4 Task 5 — 「立即触发」的每次结果,作为 「任务结果」 卡片留在
  // 对话流末尾(追加,不替换)。
  const [taskResults, setTaskResults] = useState<FireNowResult[]>([]);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const docInputRef = useRef<HTMLInputElement>(null);

  // #6 / 历史懒重建 — the resumed thread's prior conversation: flat
  // ``/messages`` text (degradation) + count-paired lazy turn blocks and
  // their per-run replay state (shared with the conversation detail page).
  const {
    messages: history,
    turns: historyTurns,
    loads: historyLoads,
    registerRow: registerHistoryRow,
    loadRuns: loadHistoryRuns,
    load: loadHistory,
    reset: resetHistory,
  } = useHistoryTurns();

  // Lazy session creation — eager creation on mount POSTed an empty throwaway
  // thread on every open/rebind (StrictMode doubled it in dev). Returns the
  // existing thread, the freshly created one, or ``null`` on failure.
  const ensureThread = useCallback(async (): Promise<ThreadMeta | null> => {
    if (thread) return thread;
    setCreatingThread(true);
    setThreadError(null);
    try {
      const created = await createSession({
        agent_name: r.name,
        agent_version: r.version,
      });
      setThread(created);
      setSidebarTick((n) => n + 1);
      return created;
    } catch (err) {
      setThreadError(
        err instanceof ApiError
          ? `${err.code}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "unknown error",
      );
      setThread(null);
      return null;
    } finally {
      setCreatingThread(false);
    }
  }, [thread, r.name, r.version]);

  const engine = useRunEngine({
    thread,
    ensureThread,
    readOnly: isTenantSwitched,
  });
  const { turns, running, streamTurnId, tokenStream } = engine;
  const { startRun: engineStartRun, reset: resetRun, stop: stopRun } = engine;

  // One ordered timeline over lazily-rebuilt history turns + this session's
  // live turns (Task 5 view model) — middle column, status bar and right rail
  // all read it. ``null`` selection follows the newest turn.
  const consoleTurns = useMemo(
    () => buildConsoleTurns({ historyTurns, historyLoads, liveTurns: turns, timings }),
    [historyTurns, historyLoads, turns, timings],
  );
  const liveEvents = useMemo(() => turns.flatMap((tn) => tn.events), [turns]);
  const {
    plan,
    loaded: planLoaded,
    save: savePlan,
  } = usePlanCard({ threadId: thread?.thread_id ?? null, liveEvents });
  const stats = useMemo(
    () => computeSessionStats(consoleTurns.map(statsInputOf), rate),
    [consoleTurns, rate],
  );
  const missing = missingRequired(promptVariables, varValues);

  // #4 cost — the agent model's rate, fetched once per (provider, model).
  // Cross-tenant W4(D2)— rate_card 是 system_admin-only:租户用户不发请求
  // (否则每次进调试台吃一发静默 403),成本区随现有「无数据」态自然隐藏。
  useEffect(() => {
    if (!isSystemAdmin) return;
    const model = readModel(r.spec);
    if (!model.provider || !model.name) return;
    let cancelled = false;
    void listRateCards({ provider: model.provider, model: model.name })
      .then((rows) => {
        if (!cancelled) setRate(rows[0] ?? null);
      })
      .catch(() => {
        // No rate / not authorized → cost simply hidden.
      });
    return () => {
      cancelled = true;
    };
  }, [r.spec, isSystemAdmin]);

  // Freeze the finished stream's timing onto its turn — the shared
  // ``tokenStream`` buffer is reset by the next run, so the status bar would
  // otherwise lose every earlier turn's ttft. First writer per turn wins.
  useEffect(() => {
    if (!tokenStream.finalized || streamTurnId === null) return;
    const { ttftMs, firstTokenAt, lastTokenAt } = tokenStream;
    setTimings((prev) =>
      prev[streamTurnId]
        ? prev
        : { ...prev, [streamTurnId]: { ttftMs, firstTokenAt, lastTokenAt } },
    );
  }, [
    tokenStream.finalized,
    tokenStream.ttftMs,
    tokenStream.firstTokenAt,
    tokenStream.lastTokenAt,
    streamTurnId,
  ]);

  // Reset to a fresh draft — no backend session is created here (see
  // ``ensureThread``). R11: 变量值随会话,新会话清空(旧实现跨会话残留)。
  const resetDraft = useCallback(() => {
    setThreadError(null);
    resetHistory();
    resetRun();
    setTaskResults([]);
    setAttachments([]);
    setUploadError(null);
    setThread(null);
    setVarValues({});
    setSelectedTurnKey(null);
    setFocusRequest(null);
    setView("chat");
  }, [resetHistory, resetRun]);

  // #6 — resume an existing thread: switch to it + keep chatting (the backend
  // keeps the context); the prior conversation is rebuilt lazily, falling back
  // to flat text when the count guard fails. R7 retired the "resumed" banner —
  // the left rail's selected row already says which session you're in.
  const handleResume = useCallback(
    (picked: ThreadMeta) => {
      resetRun();
      setTaskResults([]);
      setAttachments([]);
      setThreadError(null);
      setVarValues({});
      setSelectedTurnKey(null);
      setFocusRequest(null);
      setView("chat");
      setThread(picked);
      void loadHistory(picked.thread_id);
    },
    [loadHistory, resetRun],
  );

  // Re-bind a fresh thread when the agent changes.
  useEffect(() => {
    resetDraft();
    return stopRun;
  }, [r.name, r.version, resetDraft, stopRun]);

  const handleAttach = useCallback(
    (kind: "image" | "document") =>
      async (event: React.ChangeEvent<HTMLInputElement>) => {
        const file = event.target.files?.[0];
        event.target.value = "";
        if (!file) return;
        const active = thread ?? (await ensureThread());
        if (!active) return;
        setUploading(true);
        setUploadError(null);
        try {
          const value =
            kind === "image"
              ? await uploadImage(active.thread_id, file)
              : await uploadDocument(active.thread_id, file);
          setAttachments((prev) => [
            ...prev,
            { id: `${kind}:${value}`, name: file.name, kind, value },
          ]);
        } catch (err) {
          setUploadError(
            err instanceof ApiError && err.status === 429 && kind === "document"
              ? t("playground.workspace_full")
              : err instanceof ApiError
                ? `${err.code}: ${err.message}`
                : err instanceof Error
                  ? err.message
                  : "upload failed",
          );
        } finally {
          setUploading(false);
        }
      },
    [thread, ensureThread, t],
  );

  const handleRemoveAttachment = useCallback((id: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  }, []);

  const handleVarChange = useCallback((name: string, value: string) => {
    setVarValues((prev) => ({ ...prev, [name]: value }));
  }, []);

  // A dispatch makes the right rail follow the newest turn again (R9), and a
  // settled one moves the session's title / last activity → re-pull the rail.
  const startRun = useCallback(
    (draft: RunDraft, onDispatched?: () => void) => {
      setSelectedTurnKey(null);
      return engineStartRun(draft, onDispatched).finally(() =>
        setSidebarTick((n) => n + 1),
      );
    },
    [engineStartRun],
  );

  const handleRun = useCallback(async () => {
    // 必填变量没填齐就发不出去(Composer 已置灰,这里守住 Enter / 程序化调用)。
    if (missing.length > 0) return;
    const inputs: Record<string, string> = {};
    for (const v of promptVariables) {
      const val = varValues[v.name];
      if (val !== undefined && val !== "") inputs[v.name] = val;
    }
    await startRun({ input, attachments, inputs }, () => {
      // Consume the input + attachments only once the turn really dispatched
      // (a failed ensureThread keeps the draft intact).
      setInput("");
      setAttachments([]);
    });
  }, [startRun, input, attachments, promptVariables, varValues, missing]);

  // #10 — live-turn retry: re-dispatch the turn's original request as a NEW
  // turn (attachments are stored refs — no re-upload).
  const handleRetry = useCallback(
    (turn: Turn) => {
      void startRun({
        input: turn.input,
        attachments: turn.attachments,
        inputs: turn.inputs ?? {},
      });
    },
    [startRun],
  );

  // #10 — history-turn retry backfills the input box; BUG-16 — the jinja
  // inputs now replay from the run's system_prompt frame (console_turns
  // extracts them onto ``turn.inputs``), so backfill the variables too.
  // Old runs without frame data leave the current values untouched.
  const handleHistoryRetry = useCallback(
    (turn: Turn) => {
      if (running) return;
      setInput(turn.input);
      if (turn.inputs && Object.keys(turn.inputs).length > 0) {
        setVarValues((prev) => ({ ...prev, ...turn.inputs }));
      }
    },
    [running],
  );

  // Export a turn's full event stream as JSON. Prefer the authoritative
  // persisted stream (the ``/events`` replay) — the live client may have
  // missed frames (e.g. a paused run that never delivered ``end``); fall back
  // to this client's frames when there is no run_id or the fetch fails.
  // Either way a file always downloads.
  const handleExport = useCallback(
    async (turn: Turn) => {
      const threadId = thread?.thread_id ?? null;
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
        // Best-effort — fall back to the client-side frames already assigned.
      } finally {
        setExportingId(null);
      }
      downloadJson(`expert-work-events-${runId ?? turn.id}.json`, {
        run_id: runId,
        thread_id: threadId,
        input: turn.input,
        source,
        exported_at: new Date().toISOString(),
        events,
      });
    },
    [thread],
  );

  // The answer bubble's inline artifact download (WorkspacePanel owns its own
  // copy) — the only surviving use of the old workspace handlers.
  const handleDownloadArtifact = useCallback(
    async (name: string) => {
      try {
        await downloadArtifact(name, undefined, concreteTenantScope(apiTenantScope));
      } catch {
        // Swallow — the artifact may be gone between render and click; the
        // workspace panel's refresh re-syncs.
      }
    },
    [apiTenantScope],
  );

  const handleFireResult = useCallback((result: FireNowResult) => {
    setTaskResults((prev) => [...prev, result]);
  }, []);

  // §九「联动」—— 脚注「查看轨迹」:切「轨迹」tab + 选中该轮**最后一条**
  // ASSISTANT 记录(``rowId: null`` 就是这个约定,由 ``use_trajectory_state``
  // 落到具体记录上)。``selectedTurnKey`` 照旧给中栏的轮高亮用。
  // **只接脚注按钮**:点轮卡片空白处走 ``setSelectedTurnKey``(纯高亮),
  // 否则误点一下就被传送到另一个视图(修复轮 2)。
  const handleInspectTurn = useCallback((key: string) => {
    focusNonceRef.current += 1;
    setSelectedTurnKey(key);
    setView("trajectory");
    setFocusRequest({ turnKey: key, rowId: null, nonce: focusNonceRef.current });
  }, []);

  // §九「联动」—— 过程条每行「轨迹」:同上,但带上该行的 id(``think:<seq>``
  // 由账本侧映射成 ``assistant:<seq>``,其它 id 同名)。
  const handleInspectRow = useCallback((turnKey: string, rowId: string) => {
    focusNonceRef.current += 1;
    setSelectedTurnKey(turnKey);
    setView("trajectory");
    setFocusRequest({ turnKey, rowId, nonce: focusNonceRef.current });
  }, []);

  // 轨迹账本翻页 / 扩窗时主动回放这批历史 run(没有会话就没有历史可放)。
  const handleEnsureLoaded = useCallback(
    (runIds: readonly string[]): Promise<void> =>
      thread ? loadHistoryRuns(runIds, thread.thread_id) : Promise.resolve(),
    [thread, loadHistoryRuns],
  );

  // 左栏改名 / 归档 / 清除的回声:只有动到当前会话才影响本页。
  const handleSidebarChanged = useCallback(
    ({ kind, threadId, title }: SessionChange) => {
      if (thread === null || thread.thread_id !== threadId) return;
      if (kind === "rename") setThread({ ...thread, title: title ?? thread.title });
      else resetDraft();
    },
    [thread, resetDraft],
  );

  return (
    <ConsoleShell
      sidebarLabel={t("console.sidebar_title")}
      sidebar={
        <SessionSidebar
          agentName={r.name}
          currentThreadId={thread?.thread_id ?? null}
          running={running || creatingThread}
          onNew={resetDraft}
          onResume={handleResume}
          readOnly={isTenantSwitched}
          reloadTick={sidebarTick}
          onChanged={handleSidebarChanged}
        />
      }
      main={
        <>
          <div style={MAIN_HEAD_STYLE}>
            {threadError !== null ? (
              <Alert
                type="error"
                showIcon
                message={t("playground.session_failed")}
                description={threadError}
                data-testid="playground-session-error"
                style={{ flex: 1 }}
              />
            ) : thread ? (
              <Text
                type="secondary"
                className="mono"
                style={{ fontSize: 12 }}
                data-testid="console-thread-id"
              >
                {`${t("console.thread_id_label")}: ${thread.thread_id}`}
              </Text>
            ) : null}
            <Segmented
              value={view}
              onChange={(value) => setView(value as ConsoleView)}
              size="small"
              aria-label={t("console.view_aria")}
              data-testid="console-view-tabs"
              style={{ marginLeft: "auto" }}
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
                {
                  value: "workspace",
                  label: (
                    <span data-testid="console-view-tab-workspace">
                      {t("console.view_workspace")}
                    </span>
                  ),
                },
              ]}
            />
            {/* 轮数只跟在切换器右边:再来一个 `marginLeft: "auto"` 会把弹性
                余量分成两段,Segmented 自己也被推走,不再居中偏右。 */}
            {consoleTurns.length > 0 && (
              <Text type="secondary" style={{ fontSize: 12, marginLeft: 12 }}>
                {t("console.turn_count", { n: consoleTurns.length })}
              </Text>
            )}
          </div>

          {/* §八.2 —— 会话级状态栏那条细行。守卫用 ``stats.turns`` 而不是
              ``consoleTurns.length``:StatsBar 自己在 ``stats.turns === 0``
              时返回 null,而一轮跑到 agent 步之前就失败时是「有轮次但不计
              数」的 —— 拿 ``consoleTurns.length`` 守会留下一条只有描边的空
              行。 */}
          {stats.turns > 0 && (
            <div
              style={{ padding: "6px 16px", borderBottom: "1px solid var(--ew-border-subtle)" }}
              data-testid="console-stats-row"
            >
              <StatsBar stats={stats} isSystemAdmin={isSystemAdmin} />
            </div>
          )}

          <ViewPane view="chat" active={view === "chat"}>
            <Transcript
              turns={consoleTurns}
              flatHistory={historyTurns === null ? history : []}
              taskResults={taskResults}
              threadId={thread?.thread_id ?? null}
              selectedKey={selectedTurnKey}
              onSelectTurn={setSelectedTurnKey}
              onInspectTurn={handleInspectTurn}
              onInspectRow={handleInspectRow}
              streamTurnKey={streamTurnId}
              liveByStep={tokenStream.liveByStep}
              registerHistoryRow={registerHistoryRow}
              rate={rate}
              isSystemAdmin={isSystemAdmin}
              readOnly={false}
              isTenantSwitched={isTenantSwitched}
              onDecide={engine.decideApproval}
              deciding={running}
              onExport={handleExport}
              exportingKey={exportingId}
              // Track C W2 — 切入态只读:重试是写操作,不传 handler 按钮就不渲染。
              onRetryLive={isTenantSwitched ? undefined : handleRetry}
              onRetryHistory={isTenantSwitched ? undefined : handleHistoryRetry}
              onDownloadArtifact={handleDownloadArtifact}
              onFireResult={handleFireResult}
            />
          </ViewPane>

          <ViewPane view="trajectory" active={view === "trajectory"}>
            <TrajectoryView
              turns={consoleTurns}
              threadId={thread?.thread_id ?? null}
              agentName={r.name}
              agentVersion={r.version}
              streamTurnKey={streamTurnId}
              // 稳定引用:``tokenStream.liveByStep`` 自己是 state,只在流更新
              // 时换。这里包一层 ``new Map`` 就是每帧重建账本 / 时间轴。
              liveByStep={tokenStream.liveByStep}
              running={running}
              // 藏起来的时候不跑秒针(不改 running 的语义,只省每秒一次的
              // 账本 / 时间轴重建)。
              visible={view === "trajectory"}
              isSystemAdmin={isSystemAdmin}
              focusRequest={focusRequest}
              onEnsureLoaded={handleEnsureLoaded}
              onFireResult={handleFireResult}
            />
          </ViewPane>

          <ViewPane view="workspace" active={view === "workspace"} scroll>
            <WorkspacePanel running={running} readOnly={isTenantSwitched} />
          </ViewPane>

          <div style={COMPOSER_STYLE}>
            <PlanCard
              plan={plan}
              loaded={planLoaded}
              running={running}
              readOnly={isTenantSwitched}
              onSave={savePlan}
            />
            <VariablesForm
              variables={promptVariables}
              values={varValues}
              onChange={handleVarChange}
              disabled={running}
            />
            {uploadError !== null && (
              <Alert
                type="error"
                showIcon
                message={t("playground.upload_failed")}
                description={uploadError}
                data-testid="playground-upload-error"
              />
            )}
            <AttachmentChips attachments={attachments} onRemove={handleRemoveAttachment} />
            <Composer
              value={input}
              onChange={setInput}
              onSend={() => void handleRun()}
              onStop={stopRun}
              running={running}
              uploading={uploading}
              readOnly={isTenantSwitched}
              missingVariables={missing}
              onAttachImage={() => fileInputRef.current?.click()}
              onAttachDocument={() => docInputRef.current?.click()}
            />
          </div>

          <input
            ref={fileInputRef}
            type="file"
            accept="image/png,image/jpeg,image/webp,image/gif"
            style={{ display: "none" }}
            onChange={handleAttach("image")}
            data-testid="playground-file-input"
          />
          <input
            ref={docInputRef}
            type="file"
            accept=".pdf,.docx,.xlsx,.pptx,.txt,.md,.csv"
            style={{ display: "none" }}
            onChange={handleAttach("document")}
            data-testid="playground-doc-input"
          />
        </>
      }
    />
  );
}
