/**
 * Cost pricing over usage buckets — B-42.
 *
 * Until B-42 the console priced a turn as ``summary.usage × one rate card``
 * (the agent model's, ``PlaygroundTab`` fetched it once). Since #1374 that
 * ``usage`` includes every worker's tokens, and since #1322 a worker may run
 * on another model (``dynamic_workers.model``) — so 95% of a delegating run's
 * tokens could be priced at the wrong card (run f562fa69: worker 3,317,974 /
 * main line 175,137). Pricing now walks ``summary.usageByModel`` and looks
 * each bucket's ``(provider, model)`` up in a {@link RateBook}.
 *
 * Buckets that name no model (the main line, and worker frames from a
 * backend without ``usage_by_model``) price at the agent's own card — that is
 * exactly the pre-B-42 formula, so a worker sharing the agent's model yields a
 * bit-identical number.
 */
import type { RateCardRecord } from "./rate_card";
import type { TurnUsage, UsageBucket } from "./turn_summary";

/** Rate cards a session has on hand, keyed by {@link rateKey}. */
export interface RateBook {
  /** {@link rateKey} of the agent's own model — prices unattributed usage.
   *  ``null`` when the agent's ``spec.model`` is incomplete. */
  agentKey: string | null;
  /** One card per ``(provider, model)`` fetched so far (the agent's included).
   *  A key that was looked up and has no card is simply absent. */
  byModel: ReadonlyMap<string, RateCardRecord>;
}

export function rateKey(provider: string, model: string): string {
  return `${provider} ${model}`;
}

/** The book key a bucket prices under: its own pair when attributed, else the
 *  agent's. ``null`` = nothing to price it with. */
function keyOf(book: RateBook, bucket: UsageBucket): string | null {
  return bucket.provider !== null && bucket.model !== null
    ? rateKey(bucket.provider, bucket.model)
    : book.agentKey;
}

interface Tokens {
  input: number;
  output: number;
  cacheRead: number;
}

/** Σ over cards of the #4-cost formula (non-cached input + cache read + output,
 *  each at its per-mtok rate), in micro-元·token units; divided once by 1e12 at
 *  the end. Buckets are folded **per card** before the ``max(0, …)`` clamp
 *  and the division, so a session whose buckets all resolve to one card
 *  computes the exact same expression the single-card formula did — same
 *  operands, same order, same float result.
 *
 *  ``null`` when nothing reported usage, when there is no book, or when any
 *  bucket has no card yet (cards for worker models arrive asynchronously;
 *  showing the agent-rate number meanwhile would be the very bug this fixes). */
export function costCnyOfBuckets(
  buckets: readonly UsageBucket[],
  book: RateBook | null,
): number | null {
  if (buckets.length === 0 || book === null) return null;
  const perCard = new Map<string, Tokens>();
  for (const bucket of buckets) {
    const key = keyOf(book, bucket);
    if (key === null || !book.byModel.has(key)) return null;
    const acc = perCard.get(key) ?? { input: 0, output: 0, cacheRead: 0 };
    perCard.set(key, {
      input: acc.input + bucket.usage.inputTokens,
      output: acc.output + bucket.usage.outputTokens,
      cacheRead: acc.cacheRead + bucket.usage.cacheReadTokens,
    });
  }
  let micros = 0;
  for (const [key, t] of perCard) {
    const rate = book.byModel.get(key);
    if (rate === undefined) return null;
    micros +=
      Math.max(0, t.input - t.cacheRead) * rate.input_per_mtok_micros +
      t.cacheRead * rate.cache_read_per_mtok_micros +
      t.output * rate.output_per_mtok_micros;
  }
  return micros / 1e12;
}

/** One main-line model call's cost (B-202 — the trajectory 「计时」 tab prices
 *  each ASSISTANT step). Main-line usage carries no model attribution, so it
 *  prices exactly like the turn total does: one unattributed bucket at the
 *  agent's own card. ``null`` without a book / card. */
export function costCnyOfMainLineCall(
  tokens: { input: number; output: number; cacheRead: number },
  book: RateBook | null,
): number | null {
  const usage: TurnUsage = {
    inputTokens: tokens.input,
    outputTokens: tokens.output,
    totalTokens: tokens.input + tokens.output,
    cacheReadTokens: tokens.cacheRead,
    cacheCreationTokens: 0,
    reasoningTokens: 0,
  };
  return costCnyOfBuckets([{ provider: null, model: null, usage }], book);
}
