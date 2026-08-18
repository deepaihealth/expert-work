/**
 * useTokenStream — accumulates live token SSE frames across three channels
 * (content / reasoning / tool_args) into a per-step LiveStep for the
 * playground's streaming step card (流式 epic 子项目 3a content + 3b
 * reasoning/tool_args).
 *
 * Token frames are high-frequency and deliberately kept OUT of `turn.events`
 * (so the O(n) `parseTimeline`/`summarizeTurn` memos stay stable during token
 * flow). This hook holds the buffers in mutable refs and flushes to React
 * state once per animation frame — many tokens in one frame cause a single
 * re-render. The authoritative `updates` frame remains the source of truth; a
 * step's live buffer is superseded at render time once its authoritative card
 * exists.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import type { SseEvent } from "../../../api/sessions";

/** One step's live, cross-channel token buffer. */
export interface LiveStep {
  /** Accumulated (already server-redacted) answer text. */
  content: string;
  /** Accumulated (already server-redacted) reasoning text. */
  reasoning: string;
  /** tool-call index → tool name (name-only; args arrive via the authoritative card). */
  toolNames: ReadonlyMap<number, string>;
  /** Reasoning duration in ms once known (reasoning-start → content-start, or
   *  → finalize for a step that never produced content); null while still
   *  reasoning (not yet collapsible). */
  reasoningMs: number | null;
}

export interface TokenStreamState {
  liveByStep: ReadonlyMap<number, LiveStep>;
  /** ms from run start to the first token (any channel); null until the first token. */
  ttftMs: number | null;
  /** epoch ms (`Date.now()`) of the first token seen on any channel; null until then; cleared by `reset`, kept by `finalize`. */
  firstTokenAt: number | null;
  /** epoch ms (`Date.now()`) of the most recent token seen on any channel; null until the first token; cleared by `reset`, kept by `finalize`. */
  lastTokenAt: number | null;
  /** true once the run ended; live steps without an authoritative card are interrupted. */
  finalized: boolean;
}

export interface TokenStreamController extends TokenStreamState {
  /** Feed one SSE frame; only `token` frames on a known channel mutate state. */
  push: (frame: SseEvent) => void;
  /** Begin a new run: clear buffers + finalized flag, record the start time. */
  reset: () => void;
  /** End the run: final flush, mark finalized (keeps buffered partial text). */
  finalize: () => void;
}

interface StepBuf {
  content: string;
  reasoning: string;
  toolNames: Map<number, string>;
}

type ParsedToken =
  | { kind: "text"; channel: "content" | "reasoning"; step: number; text: string }
  | { kind: "tool"; step: number; toolIndex: number; name: string };

function parseToken(frame: SseEvent): ParsedToken | null {
  if (frame.event !== "token") return null;
  const d = frame.data;
  if (d === null || typeof d !== "object") return null;
  const rec = d as Record<string, unknown>;
  if (typeof rec.step !== "number") return null;
  if (rec.channel === "content" || rec.channel === "reasoning") {
    if (typeof rec.text !== "string") return null;
    return { kind: "text", channel: rec.channel, step: rec.step, text: rec.text };
  }
  if (rec.channel === "tool_args") {
    if (typeof rec.tool_index !== "number" || typeof rec.name !== "string") return null;
    return { kind: "tool", step: rec.step, toolIndex: rec.tool_index, name: rec.name };
  }
  return null;
}

const EMPTY: ReadonlyMap<number, LiveStep> = new Map();

export function useTokenStream(): TokenStreamController {
  const bufRef = useRef<Map<number, StepBuf>>(new Map());
  const reasoningStartRef = useRef<Map<number, number>>(new Map());
  const contentStartRef = useRef<Map<number, number>>(new Map());
  const startRef = useRef<number | null>(null);
  const ttftRef = useRef<number | null>(null);
  const firstTokenAtRef = useRef<number | null>(null);
  const lastTokenAtRef = useRef<number | null>(null);
  const rafRef = useRef<number | null>(null);
  const [snapshot, setSnapshot] = useState<TokenStreamState>({
    liveByStep: EMPTY,
    ttftMs: null,
    firstTokenAt: null,
    lastTokenAt: null,
    finalized: false,
  });

  // Build the render snapshot from the mutable buffers. `finalizeTime` non-null
  // lets a reasoning-only step (no content start) report its thinking duration.
  const build = useCallback((finalizeTime: number | null): Map<number, LiveStep> => {
    const map = new Map<number, LiveStep>();
    for (const [step, b] of bufRef.current) {
      const rs = reasoningStartRef.current.get(step);
      const cs = contentStartRef.current.get(step);
      let reasoningMs: number | null = null;
      if (rs !== undefined) {
        if (cs !== undefined) reasoningMs = cs - rs;
        else if (finalizeTime !== null) reasoningMs = finalizeTime - rs;
      }
      map.set(step, {
        content: b.content,
        reasoning: b.reasoning,
        toolNames: new Map(b.toolNames),
        reasoningMs,
      });
    }
    return map;
  }, []);

  const flush = useCallback(() => {
    rafRef.current = null;
    setSnapshot((prev) => ({
      liveByStep: build(null),
      ttftMs: ttftRef.current,
      firstTokenAt: firstTokenAtRef.current,
      lastTokenAt: lastTokenAtRef.current,
      finalized: prev.finalized,
    }));
  }, [build]);

  const schedule = useCallback(() => {
    if (rafRef.current === null) rafRef.current = requestAnimationFrame(flush);
  }, [flush]);

  const cancel = useCallback(() => {
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
  }, []);

  const push = useCallback(
    (frame: SseEvent) => {
      const tok = parseToken(frame);
      if (tok === null) return;
      const now = Date.now();
      if (firstTokenAtRef.current === null) firstTokenAtRef.current = now;
      lastTokenAtRef.current = now;
      if (ttftRef.current === null && startRef.current !== null) {
        ttftRef.current = now - startRef.current;
      }
      let b = bufRef.current.get(tok.step);
      if (b === undefined) {
        b = { content: "", reasoning: "", toolNames: new Map() };
        bufRef.current.set(tok.step, b);
      }
      if (tok.kind === "tool") {
        b.toolNames.set(tok.toolIndex, tok.name);
      } else if (tok.channel === "content") {
        if (!contentStartRef.current.has(tok.step)) contentStartRef.current.set(tok.step, Date.now());
        b.content += tok.text;
      } else {
        if (!reasoningStartRef.current.has(tok.step)) reasoningStartRef.current.set(tok.step, Date.now());
        b.reasoning += tok.text;
      }
      schedule();
    },
    [schedule],
  );

  const reset = useCallback(() => {
    cancel();
    bufRef.current = new Map();
    reasoningStartRef.current = new Map();
    contentStartRef.current = new Map();
    startRef.current = Date.now();
    ttftRef.current = null;
    firstTokenAtRef.current = null;
    lastTokenAtRef.current = null;
    setSnapshot({ liveByStep: EMPTY, ttftMs: null, firstTokenAt: null, lastTokenAt: null, finalized: false });
  }, [cancel]);

  const finalize = useCallback(() => {
    cancel();
    setSnapshot({
      liveByStep: build(Date.now()),
      ttftMs: ttftRef.current,
      firstTokenAt: firstTokenAtRef.current,
      lastTokenAt: lastTokenAtRef.current,
      finalized: true,
    });
  }, [cancel, build]);

  useEffect(() => cancel, [cancel]);

  return { ...snapshot, push, reset, finalize };
}
