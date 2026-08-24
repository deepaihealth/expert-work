/**
 * Session status-bar formulas (spec §二.1 状态栏表, R12 修正缓存项) — pure
 * aggregation over a session's turns: loaded turns contribute their parsed
 * SSE frames (``summarizeTurn``'s usage/steps + raw ``updates`` node
 * durations), unloaded history turns contribute their persisted
 * ``RunTokens`` rollup, and this session's live token-stream timing feeds
 * ttft/tok-per-s. See
 * .superpowers/sdd/2026-08-18-debug-console-pr-a-console/task-5-brief.md.
 */
import type { TurnTiming } from "../components/console/types";
import type { RateCardRecord } from "./rate_card";
import type { RunTokens } from "./runs";
import type { SseEvent } from "./sessions";
import { summarizeTurn } from "./turn_summary";

export interface StatsTurnInput {
  events: readonly SseEvent[];
  /** ``events`` is complete (live turns are always ``true``; history turns
   *  only once ``loadState === "done"``). */
  loaded: boolean;
  status: "running" | "done" | "error" | "interrupted";
  /** Used only when ``!loaded`` (unloaded history turn's persisted rollup). */
  tokens: RunTokens | null;
  timing: TurnTiming | null;
}

export interface SessionStats {
  turns: number;
  steps: number;
  llmMs: number;
  toolMs: number;
  ttftAvgMs: number | null;
  /** ≈, client wall-clock derived. */
  tokPerSec: number | null;
  /** 0-100, rounded to the nearest integer. */
  cacheHitPct: number | null;
  inputTokens: number;
  outputTokens: number;
  /** ``null`` when no rate card applies. */
  costCny: number | null;
  /** A turn contributed neither loaded events nor a persisted rollup. */
  partial: boolean;
}

/** Sum an ``agent``/``tools`` node's numeric ``_duration_ms`` across a
 *  turn's ``updates`` frames — the node names are literally ``"agent"`` /
 *  ``"tools"``. */
function nodeDurations(events: readonly SseEvent[]): { llmMs: number; toolMs: number } {
  let llmMs = 0;
  let toolMs = 0;
  for (const e of events) {
    if (e.event !== "updates" || e.data === null || typeof e.data !== "object") continue;
    const data = e.data as Record<string, unknown>;
    const agent = data.agent;
    if (agent !== null && typeof agent === "object") {
      const d = (agent as Record<string, unknown>)._duration_ms;
      if (typeof d === "number") llmMs += d;
    }
    const tools = data.tools;
    if (tools !== null && typeof tools === "object") {
      const d = (tools as Record<string, unknown>)._duration_ms;
      if (typeof d === "number") toolMs += d;
    }
  }
  return { llmMs, toolMs };
}

/** 一轮已加载事件对总计的贡献 —— 缓存单元(见下面的 WeakMap)。 */
interface TurnContribution {
  stepCount: number;
  llmMs: number;
  toolMs: number;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
}

// PlaygroundTab 每收一帧就整表重算(consoleTurns 每帧换新引用),而
// summarizeTurn + nodeDurations 是按整轮事件扫的 —— 不缓存就是每帧
// O(全会话事件数)。按 events 数组的**引用**缓存:已完成轮的数组引用逐帧
// 不变(``console_turns.ts`` 原样透传 ``turn.events``)所以命中;流式那轮
// 每帧是 ``[...tn.events, frame]`` 的新数组(``useRunEngine``),自然未命中
// 并重算。数组只追加不原地改,所以命中的那份一定还是当初算的那份。
const CONTRIBUTION_CACHE = new WeakMap<readonly SseEvent[], TurnContribution>();

function contributionOf(events: readonly SseEvent[]): TurnContribution {
  const cached = CONTRIBUTION_CACHE.get(events);
  if (cached !== undefined) return cached;
  const summary = summarizeTurn(events);
  const { llmMs, toolMs } = nodeDurations(events);
  const contribution: TurnContribution = {
    stepCount: summary.stepCount ?? 0,
    llmMs,
    toolMs,
    inputTokens: summary.usage?.inputTokens ?? 0,
    outputTokens: summary.usage?.outputTokens ?? 0,
    cacheReadTokens: summary.usage?.cacheReadTokens ?? 0,
  };
  CONTRIBUTION_CACHE.set(events, contribution);
  return contribution;
}

export function computeSessionStats(
  turns: readonly StatsTurnInput[],
  rate: RateCardRecord | null,
): SessionStats {
  let turnsCount = 0;
  let steps = 0;
  let llmMs = 0;
  let toolMs = 0;
  let inputTokens = 0;
  let outputTokens = 0;
  let cacheReadTokens = 0;
  let partial = false;
  const ttfts: number[] = [];
  let tokSum = 0;
  let secSum = 0;

  for (const t of turns) {
    let turnInput = 0;
    let turnOutput = 0;
    let turnCacheRead = 0;

    if (t.loaded) {
      const c = contributionOf(t.events);
      llmMs += c.llmMs;
      toolMs += c.toolMs;
      steps += c.stepCount;
      turnInput = c.inputTokens;
      turnOutput = c.outputTokens;
      turnCacheRead = c.cacheReadTokens;
      if (c.stepCount >= 1 || t.status === "running") turnsCount += 1;
    } else {
      if (t.tokens) {
        turnInput = t.tokens.input_tokens;
        turnOutput = t.tokens.output_tokens;
        turnCacheRead = t.tokens.cache_read_tokens;
      } else {
        partial = true;
      }
      turnsCount += 1;
    }

    inputTokens += turnInput;
    outputTokens += turnOutput;
    cacheReadTokens += turnCacheRead;

    if (t.timing !== null && t.timing.ttftMs !== null) ttfts.push(t.timing.ttftMs);
    if (
      t.timing !== null &&
      t.timing.firstTokenAt !== null &&
      t.timing.lastTokenAt !== null &&
      t.timing.firstTokenAt < t.timing.lastTokenAt
    ) {
      tokSum += turnOutput;
      secSum += (t.timing.lastTokenAt - t.timing.firstTokenAt) / 1000;
    }
  }

  const ttftAvgMs = ttfts.length > 0 ? ttfts.reduce((a, b) => a + b, 0) / ttfts.length : null;
  const tokPerSec = secSum > 0 ? Math.round((tokSum / secSum) * 10) / 10 : null;
  const cacheHitPct = inputTokens > 0 ? Math.round((cacheReadTokens / inputTokens) * 100) : null;
  const costCny = rate
    ? (Math.max(0, inputTokens - cacheReadTokens) * rate.input_per_mtok_micros +
        cacheReadTokens * rate.cache_read_per_mtok_micros +
        outputTokens * rate.output_per_mtok_micros) /
      1e12
    : null;

  return {
    turns: turnsCount,
    steps,
    llmMs,
    toolMs,
    ttftAvgMs,
    tokPerSec,
    cacheHitPct,
    inputTokens,
    outputTokens,
    costCny,
    partial,
  };
}
