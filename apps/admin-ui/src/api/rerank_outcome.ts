/** How a knowledge search's rerank ended (B-206 K-7): ``ok`` the rerank model
 *  decided the order; ``partial`` one of its two passes did (the other
 *  failed); every other value means the results kept the recall order, and
 *  says why. Sent by the retrieval-test API (``rerank_outcome``) and in a
 *  ``knowledge_search`` tool result's meta (only when not ``ok``). */
export type RerankOutcome =
  | "ok"
  | "partial"
  | "rate_limited"
  | "model_error"
  | "model_unavailable"
  | "model_unsupported"
  | "no_model";

export type RerankFallback = Exclude<RerankOutcome, "ok">;

const RERANK_FALLBACKS: readonly string[] = [
  "partial",
  "rate_limited",
  "model_error",
  "model_unavailable",
  "model_unsupported",
  "no_model",
];

/** ``value`` when the rerank did not fully decide the order (each has a
 *  ``knowledge_page.rerank_outcome_<value>`` notice); else ``null`` — ``ok``,
 *  missing, or a value this build does not know yet. */
export function rerankFallbackOf(value: unknown): RerankFallback | null {
  return typeof value === "string" && RERANK_FALLBACKS.includes(value)
    ? (value as RerankFallback)
    : null;
}

const CONFIGURATION_STATES: readonly RerankFallback[] = [
  "no_model",
  "model_unavailable",
  "model_unsupported",
];

/** B-206 K-7a — no rerank model, no credential for it, or (K-7c) a model the
 *  platform has no client for is how the deployment is set up (``info``);
 *  everything else is a failure (``warning``). */
export function rerankNoticeTone(fallback: RerankFallback): "info" | "warning" {
  return CONFIGURATION_STATES.includes(fallback) ? "info" : "warning";
}
