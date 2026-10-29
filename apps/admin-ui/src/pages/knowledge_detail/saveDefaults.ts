/**
 * 「存为库默认参数」 — B-175 P2-PR4 (master plan step 5).
 *
 * The retrieval test's parameters map onto the base's PATCH fields; only the
 * ones that differ from the base's defaults are sent, so the audit names just
 * those. The rerank floor follows the settings tab's rule (B-206 K-7b): sending
 * it binds it to the current rerank model, so it goes only when the test set
 * one and it is news — another value, or the stored one while it is paused
 * (set under another model).
 */
import type { RetrievalConfig, RetrievalMethod, UpdateBaseBody } from "../../api/knowledge";

/** The retrieval test's controls. */
export interface TestParams {
  topK: number;
  method: RetrievalMethod;
  /** Vector similarity floor; ``null`` = none. */
  threshold: number | null;
  rerank: boolean;
  /** A rerank floor for this search only; ``null`` = the base's own applies. */
  rerankThreshold: number | null;
}

type DefaultsField =
  | "retrieval_top_k"
  | "retrieval_method"
  | "retrieval_score_threshold"
  | "rerank_enabled"
  | "rerank_score_threshold";

export interface DefaultsChange {
  field: DefaultsField;
  from: number | string | boolean | null;
  to: number | string | boolean | null;
}

/** The test's starting point: the base's defaults (with the tab's long-standing
 *  fallbacks), and no rerank override. */
export function testDefaults(config: RetrievalConfig | undefined): TestParams {
  return {
    topK: config?.top_k ?? 5,
    method: config?.method ?? "hybrid",
    threshold: config?.score_threshold ?? null,
    rerank: config?.rerank_enabled ?? true,
    rerankThreshold: null,
  };
}

/** Equal, ignoring float noise from an InputNumber's steps. */
function same(a: number | null, b: number | null): boolean {
  if (a === null || b === null) return a === b;
  return Math.abs(a - b) < 1e-9;
}

/** The parameters that differ from the base's defaults, in PATCH field names. */
export function defaultsChanges(
  config: RetrievalConfig | undefined,
  params: TestParams,
): DefaultsChange[] {
  const defaults = testDefaults(config);
  const changes: DefaultsChange[] = [];
  if (params.topK !== defaults.topK) {
    changes.push({ field: "retrieval_top_k", from: defaults.topK, to: params.topK });
  }
  if (params.method !== defaults.method) {
    changes.push({ field: "retrieval_method", from: defaults.method, to: params.method });
  }
  if (!same(params.threshold, defaults.threshold)) {
    changes.push({
      field: "retrieval_score_threshold",
      from: defaults.threshold,
      to: params.threshold,
    });
  }
  if (params.rerank !== defaults.rerank) {
    changes.push({ field: "rerank_enabled", from: defaults.rerank, to: params.rerank });
  }
  const stored = config?.rerank_score_threshold ?? null;
  if (
    params.rerankThreshold !== null &&
    (!same(params.rerankThreshold, stored) || config?.rerank_threshold_paused === true)
  ) {
    changes.push({ field: "rerank_score_threshold", from: stored, to: params.rerankThreshold });
  }
  return changes;
}

/** The PATCH body that saves ``changes`` — nothing else. */
export function defaultsBody(changes: DefaultsChange[]): UpdateBaseBody {
  return Object.fromEntries(changes.map((c) => [c.field, c.to])) as UpdateBaseBody;
}
