/**
 * Per-turn summary parser — distills a turn's SSE ``updates`` frames into the
 * agent's final answer, its reasoning trace, and token usage.
 *
 * Reads the fields the OpenAI-compat decoder now surfaces (PR #847):
 * ``AIMessage.usage_metadata`` (tokens), ``additional_kwargs.reasoning_content``
 * (thinking trace), and the last AI text content (the answer).
 */
import type { SseEvent } from "./sessions";
import { messagesOf } from "./tool_timeline";

export interface TurnUsage {
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  cacheReadTokens: number;
  cacheCreationTokens: number;
  reasoningTokens: number;
}

export interface StepUsage {
  node: string;
  stepCount: number | null;
  usage: TurnUsage;
}

export interface TurnSummary {
  /** Last AI message text content — the agent's answer (null if none yet). */
  finalText: string | null;
  /** Every AI message text content, in arrival order (#8 — a multi-step turn
   *  emits one per LLM step; the transcript answer joins them all instead of
   *  showing only the last). ``finalText`` keeps its last-wins semantics —
   *  callers use its ``null`` as the "no answer → look for an approval gate"
   *  signal. */
  assistantTexts: string[];
  /** ``reasoning_content`` blocks, in arrival order. */
  reasoning: string[];
  /** Token usage summed across the turn's AI messages (null if none reported). */
  usage: TurnUsage | null;
  /** Highest ``step_count`` seen across the turn's node updates (null if none). */
  stepCount: number | null;
  /** Wall-clock from the turn's first frame to its last, in ms (null if <2 frames). */
  latencyMs: number | null;
  /** ``response_metadata.finish_reason`` of the last AI message that reports one (null if none). */
  finishReason: string | null;
  /** ``response_metadata.model_name`` of the last AI message that reports one (null if none). */
  modelName: string | null;
  /** Per-AI-message usage, each tagged with its owning node + step_count. */
  perStepUsage: StepUsage[];
}

function asInt(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function textOf(content: unknown): string | null {
  if (typeof content === "string") {
    return content.trim() === "" ? null : content;
  }
  // Block-list content (rare on the compat path) — join the text parts.
  if (Array.isArray(content)) {
    const parts = content
      .filter(
        (b): b is { text: string } =>
          b !== null &&
          typeof b === "object" &&
          typeof (b as { text?: unknown }).text === "string",
      )
      .map((b) => b.text);
    const joined = parts.join("");
    return joined.trim() === "" ? null : joined;
  }
  return null;
}

function usageFromMetadata(um: Record<string, unknown>): TurnUsage {
  const itd = um.input_token_details;
  const otd = um.output_token_details;
  const d = (v: unknown): Record<string, unknown> =>
    v !== null && typeof v === "object" ? (v as Record<string, unknown>) : {};
  return {
    inputTokens: asInt(um.input_tokens),
    outputTokens: asInt(um.output_tokens),
    totalTokens: asInt(um.total_tokens),
    cacheReadTokens: asInt(d(itd).cache_read),
    cacheCreationTokens: asInt(d(itd).cache_creation),
    reasoningTokens: asInt(d(otd).reasoning),
  };
}

/** Distill a turn's frames into answer + reasoning + usage. */
export function summarizeTurn(events: readonly SseEvent[]): TurnSummary {
  let finalText: string | null = null;
  const assistantTexts: string[] = [];
  const reasoning: string[] = [];
  let reported = false;
  let stepCount: number | null = null;
  let finishReason: string | null = null;
  let modelName: string | null = null;
  const perStepUsage: StepUsage[] = [];
  const usage: TurnUsage = {
    inputTokens: 0,
    outputTokens: 0,
    totalTokens: 0,
    cacheReadTokens: 0,
    cacheCreationTokens: 0,
    reasoningTokens: 0,
  };

  for (const evt of events) {
    if (evt.event !== "updates") continue;
    // ``step_count`` lives at the node level (alongside ``messages``), not on a
    // message — take the highest seen across the turn. Also collect a
    // per-AI-message usage row tagged with its owning node + step_count.
    if (evt.data !== null && typeof evt.data === "object") {
      for (const [nodeName, node] of Object.entries(evt.data as Record<string, unknown>)) {
        if (node === null || typeof node !== "object") continue;
        const n = node as Record<string, unknown>;
        const sc = typeof n.step_count === "number" ? n.step_count : null;
        if (sc !== null && (stepCount === null || sc > stepCount)) stepCount = sc;
        const msgs = Array.isArray(n.messages) ? n.messages : [];
        for (const m of msgs) {
          if (m === null || typeof m !== "object") continue;
          const mm = m as Record<string, unknown>;
          if (mm.type !== "ai") continue;
          const um = mm.usage_metadata;
          if (um !== null && typeof um === "object") {
            perStepUsage.push({
              node: nodeName,
              stepCount: sc,
              usage: usageFromMetadata(um as Record<string, unknown>),
            });
          }
        }
      }
    }
    for (const m of messagesOf(evt.data)) {
      if (m.type !== "ai") continue;
      const text = textOf(m.content);
      if (text !== null) {
        finalText = text; // last AI text wins
        assistantTexts.push(text); // #8 — but every step's text is kept too
      }

      const rm = m.response_metadata;
      if (rm !== null && typeof rm === "object") {
        const r = rm as Record<string, unknown>;
        if (typeof r.finish_reason === "string") finishReason = r.finish_reason;
        if (typeof r.model_name === "string") modelName = r.model_name;
      }

      const ak = m.additional_kwargs;
      if (ak !== null && typeof ak === "object") {
        const rc = (ak as Record<string, unknown>).reasoning_content;
        if (typeof rc === "string" && rc.trim() !== "") reasoning.push(rc);
      }

      const um = m.usage_metadata;
      if (um !== null && typeof um === "object") {
        reported = true;
        const su = usageFromMetadata(um as Record<string, unknown>);
        usage.inputTokens += su.inputTokens;
        usage.outputTokens += su.outputTokens;
        usage.totalTokens += su.totalTokens;
        usage.cacheReadTokens += su.cacheReadTokens;
        usage.cacheCreationTokens += su.cacheCreationTokens;
        usage.reasoningTokens += su.reasoningTokens;
      }
    }
  }

  let latencyMs: number | null = null;
  if (events.length >= 2) {
    const first = Date.parse(events[0].receivedAt);
    const last = Date.parse(events[events.length - 1].receivedAt);
    if (!Number.isNaN(first) && !Number.isNaN(last) && last >= first) {
      latencyMs = last - first;
    }
  }

  return {
    finalText,
    assistantTexts,
    reasoning,
    usage: reported ? usage : null,
    stepCount,
    latencyMs,
    finishReason,
    modelName,
    perStepUsage,
  };
}
