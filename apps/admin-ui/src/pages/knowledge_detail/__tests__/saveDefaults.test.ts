/**
 * 「存为库默认参数」 — which of the retrieval test's parameters differ from the
 * base's defaults, and the PATCH that saves only those (B-175 P2-PR4).
 */
import { describe, expect, it } from "vitest";

import type { RetrievalConfig } from "../../../api/knowledge";
import { defaultsBody, defaultsChanges, testDefaults } from "../saveDefaults";

const CONFIG: RetrievalConfig = {
  top_k: 5,
  score_threshold: null,
  method: "hybrid",
  rerank_enabled: true,
  rerank_score_threshold: 0.7,
  rerank_threshold_paused: false,
};

describe("testDefaults", () => {
  it("seeds the test from the base's defaults, without a rerank override", () => {
    expect(testDefaults(CONFIG)).toEqual({
      topK: 5,
      method: "hybrid",
      threshold: null,
      rerank: true,
      rerankThreshold: null,
    });
  });

  it("falls back like the tab always has when the base reports no config", () => {
    expect(testDefaults(undefined)).toEqual({
      topK: 5,
      method: "hybrid",
      threshold: null,
      rerank: true,
      rerankThreshold: null,
    });
  });
});

describe("defaultsChanges / defaultsBody", () => {
  it("finds nothing to save when the test runs the defaults", () => {
    expect(defaultsChanges(CONFIG, testDefaults(CONFIG))).toEqual([]);
  });

  it("saves only what changed, in the PATCH's field names", () => {
    const changes = defaultsChanges(CONFIG, {
      ...testDefaults(CONFIG),
      topK: 8,
      method: "vector",
      rerank: false,
    });
    expect(changes).toEqual([
      { field: "retrieval_top_k", from: 5, to: 8 },
      { field: "retrieval_method", from: "hybrid", to: "vector" },
      { field: "rerank_enabled", from: true, to: false },
    ]);
    expect(defaultsBody(changes)).toEqual({
      retrieval_top_k: 8,
      retrieval_method: "vector",
      rerank_enabled: false,
    });
  });

  it("sets and clears the similarity threshold (an explicit null clears it)", () => {
    const set = defaultsChanges(CONFIG, { ...testDefaults(CONFIG), threshold: 0.5 });
    expect(defaultsBody(set)).toEqual({ retrieval_score_threshold: 0.5 });

    const withThreshold = { ...CONFIG, score_threshold: 0.5 };
    const cleared = defaultsChanges(withThreshold, {
      ...testDefaults(withThreshold),
      threshold: null,
    });
    expect(cleared).toEqual([{ field: "retrieval_score_threshold", from: 0.5, to: null }]);
    expect(defaultsBody(cleared)).toEqual({ retrieval_score_threshold: null });
  });

  it("ignores float noise from stepping a number up and back down", () => {
    const stepped = { ...CONFIG, score_threshold: 0.3 };
    expect(
      defaultsChanges(stepped, { ...testDefaults(stepped), threshold: 0.1 + 0.2 }),
    ).toEqual([]);
  });

  it("saves the rerank floor only when one was set for the test and it is news", () => {
    const base = testDefaults(CONFIG);
    // No override: the base's own floor applied — nothing to save.
    expect(defaultsChanges(CONFIG, { ...base, rerankThreshold: null })).toEqual([]);
    // The same value, still in force: nothing to save (it would only rebind it).
    expect(defaultsChanges(CONFIG, { ...base, rerankThreshold: 0.7 })).toEqual([]);
    // A new value.
    const changed = defaultsChanges(CONFIG, { ...base, rerankThreshold: 0.4 });
    expect(changed).toEqual([{ field: "rerank_score_threshold", from: 0.7, to: 0.4 }]);
    expect(defaultsBody(changed)).toEqual({ rerank_score_threshold: 0.4 });
    // The same value, but paused (set under another model): calibrated now.
    const paused = { ...CONFIG, rerank_threshold_paused: true };
    expect(defaultsChanges(paused, { ...base, rerankThreshold: 0.7 })).toEqual([
      { field: "rerank_score_threshold", from: 0.7, to: 0.7 },
    ]);
  });
});
