/**
 * Playground tab — per-agent debug surface backed by real ``/v1/sessions`` +
 * ``/v1/sessions/{thread_id}/runs`` SSE.
 *
 * Playground-Uplift:
 *   - **multi-turn (D2)**: the conversation accumulates as a transcript of
 *     turns (the thread is reused, so the backend already continues context);
 *     each turn keeps its own event stream.
 *   - **per-turn observability (D3)**: each turn distills token usage +
 *     reasoning trace from its frames (the fields surfaced in #847).
 *
 * The "edit manifest snippet + re-run" affordance is a follow-up (needs a
 * backend ad-hoc manifest override that doesn't exist yet).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Empty,
  Input,
  Popconfirm,
  Space,
  Tag,
  Typography,
} from "antd";
import {
  Download,
  FileText,
  HardDrive,
  History,
  ImagePlus,
  Play,
  RefreshCw,
  RotateCcw,
  Send,
  Square,
  Trash2,
  X,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  decideApprovals,
  listApprovals,
  type ApprovalItem,
} from "../../api/approvals";
import { ApiError } from "../../api/client";
import { listRateCards, type RateCardRecord } from "../../api/rate_card";
import { streamRunEvents } from "../../api/runs";
import {
  createSession,
  streamRun,
  type RunRequest,
  type SessionWorkspace,
  type SseEvent,
  type ThreadMeta,
  type WorkspaceFile,
} from "../../api/sessions";
import {
  deleteUserWorkspaceFile,
  downloadUserWorkspaceFile,
  getUserWorkspace,
  getUserWorkspaceFiles,
} from "../../api/workspace";
import { deleteArtifact, downloadArtifact } from "../../api/artifacts";
import type { FireNowResult } from "../../api/triggers";
import { summarizeTurn } from "../../api/turn_summary";
import { uploadDocument, uploadImage } from "../../api/uploads";
import { MarkdownView } from "../../components/MarkdownView";
import { SessionHistoryDrawer } from "../../components/SessionHistoryDrawer";
import { downloadJson } from "../../components/turn/download_json";
import { HistoryDivider } from "../../components/turn/HistoryDivider";
import { TaskResultCard } from "../../components/turn/TaskResultCard";
import {
  approvalItemFromEvent,
  runIdOf,
  TurnCard,
} from "../../components/turn/TurnCard";
import type { Attachment, Turn } from "../../components/turn/types";
import { useHistoryTurns } from "../../components/turn/useHistoryTurns";
import type { AgentDetailResponse } from "../../api/agents";
import { useTokenStream } from "./playground/useTokenStream";
import {
  readModel,
  readPromptJinja,
  readPromptVariables,
} from "../../components/manifest-editor/form_model";
import { useAuth } from "../../auth/AuthContext";

const { Text } = Typography;
const { TextArea } = Input;

/** Char cap for the free-text message box — mirrors the server's
 *  ``RunRequest.input`` ``max_length`` (``MAX_RUN_INPUT_CHARS``). A long
 *  pasted email/spec fits; a whole book rides the document-upload path. */
const MAX_INPUT_CHARS = 65536;

const EVENT_VIEW_STORAGE_KEY = "expert_work.playground.eventView";

interface PlaygroundTabProps {
  detail: AgentDetailResponse;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(1)} ${units[i]}`;
}

export function PlaygroundTab({ detail }: PlaygroundTabProps) {
  const { t } = useTranslation();
  const r = detail.record;
  // Langfuse has no per-tenant isolation (single ClickHouse, all tenants
  // mixed), so the per-turn deep link (item 15) is platform-ops only — see
  // TraceToolbar for the same gate.
  const { identity } = useAuth();
  const isSystemAdmin = identity?.isSystemAdmin ?? false;

  // Dynamic-Prompt — the agent's declared run-time variables (jinja agents only).
  const manifestLike = { spec: r.spec };
  const promptJinja = readPromptJinja(manifestLike);
  const promptVariables = promptJinja
    ? readPromptVariables(manifestLike).filter(
        (v): v is { name: string } & typeof v => Boolean(v.name),
      )
    : [];

  const [thread, setThread] = useState<ThreadMeta | null>(null);
  const [threadError, setThreadError] = useState<string | null>(null);
  const [creatingThread, setCreatingThread] = useState(false);
  const [input, setInput] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [eventView, setEventViewState] = useState<"timeline" | "raw" | "exact">(
    () => {
      if (typeof window === "undefined") return "timeline";
      const stored = window.localStorage.getItem(EVENT_VIEW_STORAGE_KEY);
      return stored === "raw" || stored === "exact" ? stored : "timeline";
    },
  );
  const setEventView = useCallback((next: "timeline" | "raw" | "exact") => {
    setEventViewState(next);
    if (typeof window !== "undefined") {
      window.localStorage.setItem(EVENT_VIEW_STORAGE_KEY, next);
    }
  }, []);
  const [running, setRunning] = useState(false);
  const [exportingId, setExportingId] = useState<string | null>(null);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [varValues, setVarValues] = useState<Record<string, string>>({});
  // Playground-Uplift D4 — workspace inspector (verify the VM started + persists).
  const [workspace, setWorkspace] = useState<SessionWorkspace | null>(null);
  const [workspaceFiles, setWorkspaceFiles] = useState<WorkspaceFile[]>([]);
  const [workspaceLoading, setWorkspaceLoading] = useState(false);
  const [downloadingPath, setDownloadingPath] = useState<string | null>(null);
  // Workspace mutation in-flight: the file path or `artifact:<name>` being
  // downloaded/deleted — disables the row's buttons + drives the spinner.
  const [busyWorkspaceKey, setBusyWorkspaceKey] = useState<string | null>(null);
  // Playground-Uplift iter2 — #4 cost (agent model's rate), #6 resume history.
  const [rate, setRate] = useState<RateCardRecord | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [resumed, setResumed] = useState(false);
  // #6 / 历史懒重建 — the resumed thread's prior conversation: flat
  // ``/messages`` text (degradation) + count-paired lazy TurnCards and their
  // per-run replay state. Owned by ``useHistoryTurns`` (shared with the
  // conversation detail page); the local names below are unchanged.
  const {
    messages: history,
    turns: historyTurns,
    loads: historyLoads,
    registerRow: registerHistoryRow,
    load: loadHistory,
    reset: resetHistory,
  } = useHistoryTurns();

  // 流式打字机(子项目 3a)— live token buffer for the running turn's step
  // cards; ``streamTurnId`` marks which turn currently owns that buffer (only
  // that turn's TurnCard receives the live props — history turns never do).
  const tokenStream = useTokenStream();
  const [streamTurnId, setStreamTurnId] = useState<string | null>(null);
  // Spec 1 PR4 Task 5 — every fire-now result the「立即触发」button reports,
  // rendered inline as a 「任务结果」 card in the transcript (below the turns).
  const [taskResults, setTaskResults] = useState<FireNowResult[]>([]);

  const abortRef = useRef<AbortController | null>(null);
  const transcriptRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const docInputRef = useRef<HTMLInputElement>(null);

  // #4 cost — fetch the agent model's rate once (per-(provider,model), no tier).
  useEffect(() => {
    const model = readModel({ spec: r.spec });
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
  }, [r.spec]);

  // Reset to a fresh draft — no backend session is created here. The thread is
  // created lazily on the first real action (see ``ensureThread``), so opening
  // the Playground / switching agent no longer POSTs an empty throwaway session.
  const resetDraft = useCallback(() => {
    setThreadError(null);
    setResumed(false);
    resetHistory();
    setTurns([]);
    setTaskResults([]);
    setAttachments([]);
    setUploadError(null);
    setThread(null);
  }, [resetHistory]);

  // Lazy session creation — avoids the empty-thread spam that eager creation on
  // mount produced (each mount/rebind POSTed a session before the user did
  // anything; StrictMode doubled it in dev). Returns the existing thread, the
  // freshly created one, or ``null`` on failure.
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
      return created;
    } catch (err) {
      const message =
        err instanceof ApiError
          ? `${err.code}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "unknown error";
      setThreadError(message);
      setThread(null);
      return null;
    } finally {
      setCreatingThread(false);
    }
  }, [thread, r.name, r.version]);

  // #6 — resume an existing thread: switch to it + continue chatting (the
  // backend keeps the context). Past turns aren't replayed in the transcript;
  // a banner makes that explicit.
  const handleResume = useCallback(
    (picked: ThreadMeta) => {
      abortRef.current?.abort();
      setTurns([]);
      setTaskResults([]);
      setAttachments([]);
      setThreadError(null);
      setResumed(true);
      setThread(picked);
      // 历史重建:并行拉文本轮 + runs,按序配对成懒重建描述符;
      // 计数守卫失败或任一失败 → 落回扁平文本(hook 内保留原行为)。
      void loadHistory(picked.thread_id);
    },
    [loadHistory],
  );

  // Re-bind a fresh thread when the agent changes.
  useEffect(() => {
    resetDraft();
    return () => {
      abortRef.current?.abort();
    };
  }, [r.name, r.version, resetDraft]);

  // Auto-scroll the transcript as turns/frames arrive.
  useEffect(() => {
    const node = transcriptRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [turns]);

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
          const message =
            err instanceof ApiError
              ? `${err.code}: ${err.message}`
              : err instanceof Error
                ? err.message
                : "upload failed";
          setUploadError(message);
        } finally {
          setUploading(false);
        }
      },
    [thread, ensureThread],
  );

  const handleRemoveAttachment = useCallback((id: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  }, []);

  const patchTurn = useCallback((id: string, patch: Partial<Turn>) => {
    setTurns((prev) =>
      prev.map((tn) => (tn.id === id ? { ...tn, ...patch } : tn)),
    );
  }, []);

  // #5 — a paused run registers its agent_approval row just after the stream's
  // end frame, so poll briefly (race) for a pending approval on this thread.
  const detectApproval = useCallback(
    async (turnId: string, threadId: string, runId: string | null) => {
      for (let attempt = 0; attempt < 4; attempt++) {
        try {
          const list = await listApprovals({ status: "pending" });
          const match = list.items.find(
            (a) =>
              a.thread_id === threadId &&
              (runId === null || a.run_id === runId),
          );
          if (match) {
            patchTurn(turnId, { approval: match });
            return;
          }
        } catch {
          // best-effort — approval surfacing never fails the turn.
        }
        await new Promise((resolve) => setTimeout(resolve, 500));
      }
    },
    [patchTurn],
  );

  // #10 — send/retry shared kernel. Assembles docNote + effectiveInput from
  // the given raw input/attachments/jinja inputs (recomputed here, not passed
  // pre-baked), pushes a fresh turn and streams it. Concurrency guard =
  // ``running`` + ``abortRef``, shared by both callers. ``onDispatched`` fires
  // once the turn is actually in the transcript — the send path uses it to
  // consume the input box/attachments only when a run really started.
  const startRun = useCallback(
    async (
      req: {
        input: string;
        attachments: Attachment[];
        inputs: Record<string, string>;
      },
      onDispatched?: () => void,
    ) => {
    if (running) return;
    // Lazy — create the backend thread on this first send if it doesn't exist.
    const active = thread ?? (await ensureThread());
    if (!active) return;
    setRunning(true);
    const turnAttachments = req.attachments;
    const turnInput = req.input;
    const imageRefs = turnAttachments
      .filter((a) => a.kind === "image")
      .map((a) => a.value);
    const docPaths = turnAttachments
      .filter((a) => a.kind === "document")
      .map((a) => a.value);
    const docNote =
      docPaths.length > 0
        ? `${t("playground.uploaded_docs_note")}: ${docPaths.join(", ")}\n\n`
        : "";
    const effectiveInput = docNote + turnInput;
    const inputs = req.inputs;
    const body: RunRequest = { input: effectiveInput };
    if (imageRefs.length > 0) body.image_refs = imageRefs;
    if (Object.keys(inputs).length > 0) body.inputs = inputs;

    const turnId = `${Date.now()}-${turns.length}`;
    const updateTurn = (patch: Partial<Turn>) =>
      setTurns((prev) =>
        prev.map((tn) => (tn.id === turnId ? { ...tn, ...patch } : tn)),
      );
    setTurns((prev) => [
      ...prev,
      {
        id: turnId,
        input: turnInput,
        attachments: turnAttachments,
        inputs,
        events: [],
        status: "running",
        error: null,
        approval: null,
      },
    ]);
    onDispatched?.();

    tokenStream.reset();
    setStreamTurnId(turnId);

    const ac = new AbortController();
    abortRef.current = ac;
    const frames: SseEvent[] = [];
    const threadId = active.thread_id;
    try {
      for await (const frame of streamRun(threadId, body, {
        signal: ac.signal,
      })) {
        if (frame.event === "token") {
          tokenStream.push(frame);
          continue;
        }
        frames.push(frame);
        // #5 — a dedicated ``approval`` event surfaces the gate deterministically
        // (no dependence on the terminal ``end`` frame or a post-stream poll).
        const approvalFromFrame =
          frame.event === "approval" ? approvalItemFromEvent(frame.data) : null;
        setTurns((prev) =>
          prev.map((tn) =>
            tn.id === turnId
              ? {
                  ...tn,
                  events: [...tn.events, frame],
                  approval: approvalFromFrame ?? tn.approval,
                }
              : tn,
          ),
        );
        if (frame.event === "end") break;
      }
      updateTurn({ status: "done" });
    } catch (err) {
      if (err instanceof Error && err.name === "AbortError") {
        updateTurn({ status: "done" });
      } else {
        const message = err instanceof Error ? err.message : "stream failed";
        updateTurn({ status: "error", error: message });
      }
    } finally {
      tokenStream.finalize();
      setRunning(false);
      abortRef.current = null;
    }
    // #5 — a paused run yields no final answer; look for its approval gate.
    // Fire-and-forget so the run UI (Stop button) frees immediately; a found
    // gate patches the turn asynchronously.
    if (
      frames.at(-1)?.event === "end" &&
      summarizeTurn(frames).finalText === null
    ) {
      void detectApproval(turnId, threadId, runIdOf(frames));
    }
    },
    [
      thread,
      ensureThread,
      running,
      turns.length,
      t,
      detectApproval,
      tokenStream,
    ],
  );

  const handleRun = useCallback(async () => {
    const inputs: Record<string, string> = {};
    for (const v of promptVariables) {
      const val = varValues[v.name];
      if (val !== undefined && val !== "") inputs[v.name] = val;
    }
    await startRun({ input, attachments, inputs }, () => {
      // Consume the input + attachments — the next turn starts fresh. Only
      // fires once the turn actually dispatched (a failed ensureThread keeps
      // the draft intact, same as before the #10 extraction).
      setInput("");
      setAttachments([]);
    });
  }, [startRun, input, attachments, promptVariables, varValues]);

  // #10 — live-turn retry: re-dispatch the turn's original input/attachments/
  // jinja inputs as a NEW turn (attachments are stored refs — no re-upload).
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

  // #10 — history-turn retry: a past run's enqueued inputs live in the
  // checkpoint (not exposed yet), so retry backfills the input box for the
  // user to re-send — no backend re-dispatch.
  const handleHistoryRetry = useCallback(
    (turn: Turn) => {
      if (running) return;
      setInput(turn.input);
    },
    [running],
  );

  // #5 — decide a turn's pending approval, then stream the continuation run
  // (the decision spawns it) into the SAME turn, then re-check for a next gate.
  const handleDecide = useCallback(
    async (
      turnId: string,
      approval: ApprovalItem,
      decision: "approve" | "reject",
    ) => {
      if (!thread) return;
      const threadId = thread.thread_id;
      setRunning(true);
      patchTurn(turnId, { approval: null, status: "running" });
      let continuationRunId: string | null = null;
      try {
        const result = await decideApprovals([
          { thread_id: threadId, run_id: approval.run_id, decision },
        ]);
        continuationRunId = result.results[0]?.continuation_run_id ?? null;
      } catch (err) {
        const message = err instanceof Error ? err.message : "decision failed";
        patchTurn(turnId, { status: "error", error: message });
        setRunning(false);
        return;
      }
      if (continuationRunId === null) {
        patchTurn(turnId, { status: "done" });
        setRunning(false);
        return;
      }
      tokenStream.reset();
      setStreamTurnId(turnId);

      const ac = new AbortController();
      abortRef.current = ac;
      const frames: SseEvent[] = [];
      try {
        for await (const frame of streamRunEvents(threadId, continuationRunId, {
          signal: ac.signal,
        })) {
          if (frame.event === "token") {
            tokenStream.push(frame);
            continue;
          }
          frames.push(frame);
          setTurns((prev) =>
            prev.map((tn) =>
              tn.id === turnId ? { ...tn, events: [...tn.events, frame] } : tn,
            ),
          );
          if (frame.event === "end") break;
        }
        patchTurn(turnId, { status: "done" });
      } catch (err) {
        if (!(err instanceof Error && err.name === "AbortError")) {
          const message = err instanceof Error ? err.message : "stream failed";
          patchTurn(turnId, { status: "error", error: message });
        } else {
          patchTurn(turnId, { status: "done" });
        }
      } finally {
        tokenStream.finalize();
        setRunning(false);
        abortRef.current = null;
      }
      // Chained gate — re-check after the continuation, fire-and-forget.
      if (
        frames.at(-1)?.event === "end" &&
        summarizeTurn(frames).finalText === null
      ) {
        void detectApproval(turnId, threadId, continuationRunId);
      }
    },
    [thread, patchTurn, detectApproval, tokenStream],
  );

  // Export a turn's full event stream as JSON for offline analysis. Prefer the
  // authoritative persisted stream (the ``/events`` replay) — the live client
  // may have missed frames (e.g. a paused run that never delivered ``end``);
  // fall back to the frames this client received when there is no run_id or the
  // fetch fails. Either way a file always downloads.
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

  // The workspace is keyed on the current user (not a thread), so it loads
  // once and survives session deletion — the whole point of the user-scoped
  // ``/v1/workspace`` route.
  const loadWorkspace = useCallback(async () => {
    setWorkspaceLoading(true);
    try {
      const [ws, fs] = await Promise.all([
        getUserWorkspace(),
        getUserWorkspaceFiles().catch(() => [] as WorkspaceFile[]),
      ]);
      setWorkspace(ws);
      setWorkspaceFiles(fs);
    } catch {
      setWorkspace(null);
      setWorkspaceFiles([]);
    } finally {
      setWorkspaceLoading(false);
    }
  }, []);

  const handleDownloadFile = useCallback(async (path: string) => {
    setDownloadingPath(path);
    try {
      await downloadUserWorkspaceFile(path);
    } catch {
      // Swallow — the file may have been removed between list + click; the
      // refresh button re-syncs. A toast here would need the App message API.
    } finally {
      setDownloadingPath(null);
    }
  }, []);

  const handleDownloadArtifact = useCallback(async (name: string) => {
    setBusyWorkspaceKey(`artifact:${name}`);
    try {
      await downloadArtifact(name);
    } catch {
      // Swallow — same rationale as the file download.
    } finally {
      setBusyWorkspaceKey(null);
    }
  }, []);

  const handleDeleteFile = useCallback(
    async (path: string) => {
      setBusyWorkspaceKey(path);
      try {
        await deleteUserWorkspaceFile(path);
        await loadWorkspace();
      } catch {
        // Swallow — refresh re-syncs the listing on the next manual refresh.
      } finally {
        setBusyWorkspaceKey(null);
      }
    },
    [loadWorkspace],
  );

  const handleDeleteArtifact = useCallback(
    async (name: string) => {
      setBusyWorkspaceKey(`artifact:${name}`);
      try {
        await deleteArtifact(name);
        await loadWorkspace();
      } catch {
        // Swallow — refresh re-syncs.
      } finally {
        setBusyWorkspaceKey(null);
      }
    },
    [loadWorkspace],
  );

  // Load the current user's workspace on mount and refresh after each run — a
  // run that wrote files makes the volume appear / its size grow. Not tied to a
  // thread, so the panel stays visible after the session is deleted.
  useEffect(() => {
    if (!running) void loadWorkspace();
  }, [running, loadWorkspace]);

  const handleStop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  // Spec 1 PR4 Task 5 — the manage_task card's 「立即触发」 button reports its
  // result here (threaded through TurnCard → StepTimeline → AgentStepCard →
  // ToolCallCard); appended (never replacing) so every fire this session
  // stays visible in the transcript.
  const handleFireResult = useCallback((result: FireNowResult) => {
    setTaskResults((prev) => [...prev, result]);
  }, []);

  return (
    <div
      data-testid="playground-tab"
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(360px, 2fr) minmax(420px, 3fr)",
        gap: 12,
        alignItems: "stretch",
      }}
    >
      <SessionHistoryDrawer
        open={historyOpen}
        onClose={() => setHistoryOpen(false)}
        agentName={r.name}
        currentThreadId={thread?.thread_id ?? null}
        onResume={handleResume}
      />
      {/* Left — session + input */}
      <div
        style={{
          border: "1px solid var(--ew-border-subtle)",
          borderRadius: 6,
          padding: 12,
          display: "flex",
          flexDirection: "column",
          gap: 12,
          minHeight: "calc(100vh - 360px)",
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
          }}
        >
          <Text strong style={{ fontSize: 13 }}>
            {t("playground.session_label")}
          </Text>
          <Space size={6}>
            <Button
              size="small"
              icon={<History size={12} strokeWidth={1.75} />}
              onClick={() => setHistoryOpen(true)}
              disabled={running}
              data-testid="playground-history-open"
            >
              {t("playground.history_button")}
            </Button>
            <Button
              size="small"
              icon={<RotateCcw size={12} strokeWidth={1.75} />}
              onClick={resetDraft}
              loading={creatingThread}
              disabled={running}
              data-testid="playground-new-session"
            >
              {t("playground.new_session")}
            </Button>
          </Space>
        </div>
        {resumed && (
          <Alert
            type="info"
            showIcon
            message={t("playground.resumed_notice")}
            data-testid="playground-resumed-notice"
            style={{ padding: "4px 8px" }}
          />
        )}

        {threadError !== null ? (
          <Alert
            type="error"
            showIcon
            message={t("playground.session_failed")}
            description={threadError}
            data-testid="playground-session-error"
          />
        ) : thread ? (
          <Text type="secondary" style={{ fontSize: 12 }} className="mono">
            {`${t("playground.thread_id")}: ${thread.thread_id}`}
          </Text>
        ) : null}

        {promptVariables.length > 0 && (
          <div data-testid="playground-vars" style={{ marginBottom: 8 }}>
            <Text
              type="secondary"
              style={{ fontSize: 12, display: "block", marginBottom: 4 }}
            >
              {t("playground.prompt_vars_label")}
            </Text>
            {promptVariables.map((v) => (
              <div
                key={v.name}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  marginBottom: 4,
                }}
              >
                <Text style={{ width: 140, fontSize: 12 }} className="mono">
                  {v.name}
                  {v.required !== false ? " *" : ""}
                </Text>
                <Input
                  size="small"
                  value={varValues[v.name] ?? ""}
                  placeholder={v.description ?? v.name}
                  aria-label={`${t("playground.prompt_vars_label")}: ${v.name}`}
                  data-testid={`playground-var-${v.name}`}
                  disabled={running}
                  onChange={(e) =>
                    setVarValues((prev) => ({
                      ...prev,
                      [v.name]: e.target.value,
                    }))
                  }
                />
              </div>
            ))}
          </div>
        )}

        <TextArea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={t("playground.input_placeholder")}
          autoSize={{ minRows: 6, maxRows: 14 }}
          disabled={running}
          maxLength={MAX_INPUT_CHARS}
          showCount
          data-testid="playground-input"
        />

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

        {uploadError !== null && (
          <Alert
            type="error"
            showIcon
            message={t("playground.upload_failed")}
            description={uploadError}
            data-testid="playground-upload-error"
          />
        )}

        {attachments.length > 0 && (
          <div data-testid="playground-attachments">
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t("playground.attachments_label")}
            </Text>
            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                gap: 6,
                marginTop: 6,
              }}
            >
              {attachments.map((a) => (
                <Tag
                  key={a.id}
                  closable
                  onClose={(e) => {
                    e.preventDefault();
                    handleRemoveAttachment(a.id);
                  }}
                  closeIcon={
                    <X
                      size={11}
                      strokeWidth={1.75}
                      aria-label={t("playground.remove_attachment")}
                    />
                  }
                  bordered={false}
                  data-testid="playground-attachment"
                >
                  {a.name}
                </Tag>
              ))}
            </div>
          </div>
        )}

        <Space size={8}>
          <Button
            type="primary"
            icon={
              running ? (
                <Play size={14} strokeWidth={1.75} />
              ) : (
                <Send size={14} strokeWidth={1.75} />
              )
            }
            onClick={handleRun}
            loading={running}
            disabled={!running && input.trim().length === 0}
            data-testid="playground-run"
          >
            {running ? t("playground.running") : t("playground.run")}
          </Button>
          <Button
            icon={<ImagePlus size={14} strokeWidth={1.75} />}
            onClick={() => fileInputRef.current?.click()}
            loading={uploading}
            disabled={running}
            data-testid="playground-attach"
          >
            {uploading
              ? t("playground.uploading")
              : t("playground.attach_image")}
          </Button>
          <Button
            icon={<FileText size={14} strokeWidth={1.75} />}
            onClick={() => docInputRef.current?.click()}
            loading={uploading}
            disabled={running}
            data-testid="playground-attach-doc"
          >
            {uploading
              ? t("playground.uploading")
              : t("playground.attach_document")}
          </Button>
          {running && (
            <Button
              danger
              icon={<Square size={14} strokeWidth={1.75} />}
              onClick={handleStop}
              data-testid="playground-stop"
            >
              {t("playground.stop")}
            </Button>
          )}
        </Space>

        {/* Playground-Uplift D4 — workspace inspector. Keyed on the current
            user, not the bound thread, so it stays visible after the session
            is deleted (loads once ``workspace`` is fetched). */}
        {workspace && (
          <div
            data-testid="playground-workspace"
            style={{
              marginTop: "auto",
              borderTop: "1px solid var(--ew-border-subtle)",
              paddingTop: 8,
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                marginBottom: 6,
              }}
            >
              <HardDrive size={13} strokeWidth={1.75} />
              <Text strong style={{ fontSize: 12 }}>
                {t("playground.workspace_label")}
              </Text>
              <Button
                size="small"
                type="text"
                icon={<RefreshCw size={11} strokeWidth={1.75} />}
                loading={workspaceLoading}
                onClick={() => void loadWorkspace()}
                aria-label={t("playground.workspace_refresh")}
                data-testid="playground-workspace-refresh"
                style={{ marginLeft: "auto" }}
              />
            </div>
            {workspace?.workspace ? (
              <div style={{ fontSize: 11 }} className="mono">
                <div data-testid="playground-workspace-volume">
                  {t("playground.workspace_volume")}:{" "}
                  {workspace.workspace.volume_name}
                </div>
                <div>
                  {t("playground.workspace_size")}:{" "}
                  {formatBytes(workspace.workspace.size_bytes)}
                  {workspace.workspace.deleted_at
                    ? ` · ${t("playground.workspace_deleted")}`
                    : ""}
                </div>
              </div>
            ) : (
              <Text
                type="secondary"
                style={{ fontSize: 11 }}
                data-testid="playground-workspace-none"
              >
                {t("playground.workspace_none")}
              </Text>
            )}
            {/* Artifacts — the agent's registered deliverables: download +
                (soft-)delete each. A list, not chips, since they're the things
                you actually take away. */}
            {workspace && workspace.artifacts.length > 0 && (
              <div
                style={{ marginTop: 6 }}
                data-testid="playground-workspace-artifacts"
              >
                <Text type="secondary" style={{ fontSize: 11 }}>
                  {t("playground.workspace_artifacts")}:
                </Text>
                <div
                  style={{
                    marginTop: 4,
                    display: "flex",
                    flexDirection: "column",
                    gap: 2,
                  }}
                >
                  {workspace.artifacts.map((a) => (
                    <div
                      key={a.name}
                      data-testid="playground-workspace-artifact"
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 6,
                        fontSize: 11,
                      }}
                    >
                      <span
                        className="mono"
                        style={{
                          flex: 1,
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                        title={`${a.name} · ${a.kind} v${a.latest_version}`}
                      >
                        {a.name}
                      </span>
                      <Text type="secondary" style={{ fontSize: 10 }}>
                        {a.kind} v{a.latest_version}
                      </Text>
                      <Button
                        size="small"
                        type="text"
                        icon={<Download size={11} strokeWidth={1.75} />}
                        loading={busyWorkspaceKey === `artifact:${a.name}`}
                        disabled={busyWorkspaceKey !== null}
                        onClick={() => void handleDownloadArtifact(a.name)}
                        aria-label={t("playground.artifact_download", {
                          name: a.name,
                        })}
                        data-testid="playground-workspace-artifact-download"
                      />
                      <Popconfirm
                        title={t("playground.artifact_delete_confirm")}
                        okText={t("playground.delete_ok")}
                        cancelText={t("playground.delete_cancel")}
                        okButtonProps={{ danger: true }}
                        onConfirm={() => void handleDeleteArtifact(a.name)}
                      >
                        <Button
                          size="small"
                          type="text"
                          danger
                          icon={<Trash2 size={11} strokeWidth={1.75} />}
                          disabled={busyWorkspaceKey !== null}
                          aria-label={t("playground.artifact_delete", {
                            name: a.name,
                          })}
                          data-testid="playground-workspace-artifact-delete"
                        />
                      </Popconfirm>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {/* Browse + download + delete the raw files the agent wrote. Hidden
                files (.npm/.cache/.mplconfig …) are filtered — runtime noise. */}
            {workspaceFiles.some((f) => !isHiddenWorkspacePath(f.path)) && (
              <div
                style={{ marginTop: 8 }}
                data-testid="playground-workspace-files"
              >
                <Text type="secondary" style={{ fontSize: 11 }}>
                  {t("playground.workspace_files")}:
                </Text>
                <div
                  style={{
                    marginTop: 4,
                    display: "flex",
                    flexDirection: "column",
                    gap: 2,
                  }}
                >
                  {workspaceFiles
                    .filter((f) => !isHiddenWorkspacePath(f.path))
                    .map((f) => (
                      <div
                        key={f.path}
                        data-testid="playground-workspace-file"
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: 6,
                          fontSize: 11,
                        }}
                      >
                        <span
                          className="mono"
                          style={{
                            flex: 1,
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                          title={f.path}
                        >
                          {f.path}
                        </span>
                        <Text type="secondary" style={{ fontSize: 10 }}>
                          {formatBytes(f.size)}
                        </Text>
                        <Button
                          size="small"
                          type="text"
                          icon={<Download size={11} strokeWidth={1.75} />}
                          loading={downloadingPath === f.path}
                          disabled={
                            downloadingPath !== null || busyWorkspaceKey !== null
                          }
                          onClick={() => void handleDownloadFile(f.path)}
                          aria-label={t("playground.workspace_file_download", {
                            name: f.path,
                          })}
                          data-testid="playground-workspace-file-download"
                        />
                        <Popconfirm
                          title={t("playground.file_delete_confirm")}
                          okText={t("playground.delete_ok")}
                          cancelText={t("playground.delete_cancel")}
                          okButtonProps={{ danger: true }}
                          onConfirm={() => void handleDeleteFile(f.path)}
                        >
                          <Button
                            size="small"
                            type="text"
                            danger
                            icon={<Trash2 size={11} strokeWidth={1.75} />}
                            loading={busyWorkspaceKey === f.path}
                            disabled={busyWorkspaceKey !== null}
                            aria-label={t("playground.file_delete", {
                              name: f.path,
                            })}
                            data-testid="playground-workspace-file-delete"
                          />
                        </Popconfirm>
                      </div>
                    ))}
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Right — conversation transcript */}
      <div
        style={{
          border: "1px solid var(--ew-border-subtle)",
          borderRadius: 6,
          padding: 0,
          display: "flex",
          flexDirection: "column",
          // Definite height (not just a floor) so the transcript body below can
          // be a bounded flex child that scrolls internally. A minHeight-only
          // box grows with content → the cap below is ignored → no scroll.
          height: "calc(100vh - 360px)",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            padding: "8px 12px",
            borderBottom: "1px solid var(--ew-border-subtle)",
            display: "flex",
            alignItems: "center",
            gap: 8,
          }}
        >
          <Text strong style={{ fontSize: 13 }}>
            {t("playground.transcript_label")}
          </Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {turns.length === 0
              ? ""
              : t("playground.turn_count", { n: turns.length })}
          </Text>
        </div>

        <div
          ref={transcriptRef}
          style={{
            flex: 1,
            // ``minHeight: 0`` is the critical bit — a flex child defaults to
            // ``min-height: auto`` (≥ its content), which beats the parent's cap
            // when events pile up, so the list grows past the viewport and never
            // scrolls. Zeroing it lets the bounded parent clip → overflow scrolls.
            minHeight: 0,
            padding: 12,
            overflow: "auto",
            display: "flex",
            flexDirection: "column",
            gap: 12,
          }}
          data-testid="playground-transcript"
        >
          {turns.length === 0 && history.length === 0 && (
            <Empty
              description={t("playground.empty_log")}
              style={{ marginTop: 64 }}
              data-testid="playground-empty-log"
            />
          )}
          {/* #6 / 历史懒重建 — prior conversation (read-only) when resuming a
              thread. ``historyTurns`` non-null → count-paired lazy TurnCards
              (each replays its run's events when scrolled into view); else
              the flat text block (degradation — count mismatch or a failed
              ``listThreadRuns``/``buildHistoryTurns``). */}
          {historyTurns !== null ? (
            <div
              data-testid="playground-history"
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 8,
                flexShrink: 0,
              }}
            >
              {historyTurns.map((h, idx) => {
                const load = historyLoads[h.runId] ?? {
                  state: "pending" as const,
                  events: [],
                };
                return (
                  <div
                    key={h.key}
                    ref={registerHistoryRow(h.runId, thread?.thread_id ?? "")}
                  >
                    <TurnCard
                      turn={{
                        id: h.key,
                        input: h.input,
                        attachments: [],
                        events: load.events,
                        // #10 — surface the run's real terminal state instead
                        // of a hardcoded "done"; other statuses (interrupted/
                        // paused/…) keep the normal rendering.
                        status:
                          h.status === "error" || h.status === "timeout"
                            ? "error"
                            : "done",
                        // C1 — ``ThreadRunSummary`` carries no error text yet,
                        // so the raw status string keeps the failure banner
                        // from rendering an empty frame.
                        error:
                          h.status === "error" || h.status === "timeout"
                            ? h.status
                            : null,
                        approval: null,
                      }}
                      turnSeq={idx}
                      initialEventView={eventView}
                      onViewChange={setEventView}
                      threadId={thread?.thread_id ?? null}
                      onDownloadArtifact={handleDownloadArtifact}
                      rate={rate}
                      onDecide={() => {}}
                      deciding={false}
                      onExport={handleExport}
                      exporting={exportingId === h.key}
                      isSystemAdmin={isSystemAdmin}
                      readOnly
                      loadState={load.state}
                      fallbackLines={h.fallbackLines}
                      onFireResult={handleFireResult}
                      onRetry={handleHistoryRetry}
                    />
                  </div>
                );
              })}
              <HistoryDivider />
            </div>
          ) : (
            history.length > 0 && (
              <div
                data-testid="playground-history"
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: 8,
                  flexShrink: 0,
                }}
              >
                {history.map((m, idx) => (
                  <div
                    key={idx}
                    style={{
                      alignSelf: m.role === "user" ? "flex-end" : "flex-start",
                      maxWidth: "85%",
                      padding: "6px 10px",
                      borderRadius: 8,
                      fontSize: 13,
                      whiteSpace: "pre-wrap",
                      background:
                        m.role === "user"
                          ? "var(--ew-surface-raised)"
                          : "transparent",
                      border:
                        m.role === "user"
                          ? "1px solid var(--ew-border-subtle)"
                          : "none",
                      opacity: 0.75,
                    }}
                  >
                    {m.role === "user" ? (
                      m.content
                    ) : (
                      <MarkdownView>{m.content}</MarkdownView>
                    )}
                  </div>
                ))}
                <HistoryDivider />
              </div>
            )
          )}
          {turns.map((turn, turnIndex) => (
            <TurnCard
              key={turn.id}
              turn={turn}
              turnSeq={turnIndex}
              initialEventView={eventView}
              onViewChange={setEventView}
              threadId={thread?.thread_id ?? null}
              onDownloadArtifact={handleDownloadArtifact}
              rate={rate}
              onDecide={handleDecide}
              deciding={running}
              onExport={handleExport}
              exporting={exportingId === turn.id}
              isSystemAdmin={isSystemAdmin}
              liveByStep={
                turn.id === streamTurnId ? tokenStream.liveByStep : undefined
              }
              ttftMs={turn.id === streamTurnId ? tokenStream.ttftMs : null}
              finalized={
                turn.id === streamTurnId ? tokenStream.finalized : false
              }
              onFireResult={handleFireResult}
              onRetry={handleRetry}
            />
          ))}
          {taskResults.map((result) => (
            <TaskResultCard key={result.run_id} result={result} />
          ))}
        </div>
      </div>
    </div>
  );
}

/** Hide dotfiles/dotdirs (``.npm``, ``.cache``, ``.mplconfig``, …) — runtime
 *  scaffolding the agent didn't author, just noise in the workspace browser. */
function isHiddenWorkspacePath(path: string): boolean {
  return path.split("/").some((seg) => seg.startsWith("."));
}
