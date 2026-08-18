/**
 * useRunEngine — the debug console's run kernel: the live turn list and every
 * request that mutates it. Lifted verbatim out of ``PlaygroundTab.tsx``
 * (调试台重设计 PR-A Task 19) so that file stays the state + assembly layer;
 * the behaviour, comments and concurrency rules below are unchanged.
 *
 * Owns: ``turns`` / ``running`` / ``streamTurnId`` / the shared token-stream
 * buffer / the in-flight ``AbortController``, plus the two SSE consumers —
 * ``startRun`` (send + retry) and ``decideApproval`` (approve/reject, then
 * stream the continuation run into the SAME turn).
 */
import { useCallback, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  decideApprovals,
  listApprovals,
  type ApprovalItem,
} from "../../../api/approvals";
import { streamRunEvents } from "../../../api/runs";
import {
  streamRun,
  type RunRequest,
  type SseEvent,
  type ThreadMeta,
} from "../../../api/sessions";
import { summarizeTurn } from "../../../api/turn_summary";
import { runIdOf } from "../../../components/console/console_turns";
import { approvalItemFromEvent } from "../../../components/turn/TurnCard";
import type { Attachment, Turn } from "../../../components/turn/types";
import { useTokenStream, type TokenStreamController } from "./useTokenStream";

/** One dispatch's raw request — the shared kernel re-derives the doc note and
 *  the effective prompt from it (never a pre-baked body). */
export interface RunDraft {
  input: string;
  attachments: Attachment[];
  inputs: Record<string, string>;
}

export interface RunEngine {
  turns: Turn[];
  running: boolean;
  /** Which turn currently owns the shared token buffer (only that turn's
   *  block receives the live props — history turns never do). */
  streamTurnId: string | null;
  tokenStream: TokenStreamController;
  startRun: (draft: RunDraft, onDispatched?: () => void) => Promise<void>;
  decideApproval: (
    turnId: string,
    approval: ApprovalItem,
    decision: "approve" | "reject",
  ) => Promise<void>;
  /** User pressed 「停止」 — aborts the in-flight stream. */
  stop: () => void;
  /** Drop every live turn (new session / resume) — aborts first. */
  reset: () => void;
}

export function useRunEngine(args: {
  thread: ThreadMeta | null;
  /** Lazy session creation — returns the existing/created thread, or null. */
  ensureThread: () => Promise<ThreadMeta | null>;
  /** 切入态只读:审批决策是写操作,残留 gate 的防御性拦截。 */
  readOnly: boolean;
}): RunEngine {
  const { thread, ensureThread, readOnly } = args;
  const { t } = useTranslation();

  const [turns, setTurns] = useState<Turn[]>([]);
  const [running, setRunning] = useState(false);
  const tokenStream = useTokenStream();
  const [streamTurnId, setStreamTurnId] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

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
              a.thread_id === threadId && (runId === null || a.run_id === runId),
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
  // the given raw input/attachments/jinja inputs, pushes a fresh turn and
  // streams it. Concurrency guard = ``running`` + ``abortRef``, shared by both
  // callers. ``onDispatched`` fires once the turn is actually in the
  // transcript — the send path uses it to consume the input box/attachments
  // only when a run really started.
  const startRun = useCallback(
    async (draft: RunDraft, onDispatched?: () => void) => {
      if (running) return;
      // Lazy — create the backend thread on this first send if missing.
      const active = thread ?? (await ensureThread());
      if (!active) return;
      setRunning(true);
      const turnAttachments = draft.attachments;
      const turnInput = draft.input;
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
      const inputs = draft.inputs;
      const body: RunRequest = { input: docNote + turnInput };
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
        for await (const frame of streamRun(threadId, body, { signal: ac.signal })) {
          if (frame.event === "token") {
            tokenStream.push(frame);
            continue;
          }
          frames.push(frame);
          // #5 — a dedicated ``approval`` event surfaces the gate
          // deterministically (no dependence on ``end`` or a post-stream poll).
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
      // Fire-and-forget so the Stop button frees immediately.
      if (
        frames.at(-1)?.event === "end" &&
        summarizeTurn(frames).finalText === null
      ) {
        void detectApproval(turnId, threadId, runIdOf(frames));
      }
    },
    [thread, ensureThread, running, turns.length, t, detectApproval, tokenStream],
  );

  // #5 — decide a turn's pending approval, then stream the continuation run
  // (the decision spawns it) into the SAME turn, then re-check for a next gate.
  const decideApproval = useCallback(
    async (
      turnId: string,
      approval: ApprovalItem,
      decision: "approve" | "reject",
    ) => {
      // 切入态下发不出新 run;这里拦的是残留 gate(在本层拦,不动 TurnBlock)。
      if (readOnly) return;
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
    [thread, patchTurn, detectApproval, tokenStream, readOnly],
  );

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    setTurns([]);
  }, []);

  return {
    turns,
    running,
    streamTurnId,
    tokenStream,
    startRun,
    decideApproval,
    stop,
    reset,
  };
}
