/**
 * form_model_profiles.test.ts — config-page redesign v2 Task 6's run-profile
 * presets (``applyRunProfile``/``inferRunProfile``/``countProfileDiff``,
 * form_model.ts). Split from ``form_model.test.ts`` (already ~2500 lines)
 * since these tests exercise a single, self-contained feature over 18
 * previously-independent fields.
 *
 * The linchpin invariant: applying "balanced" must leave every managed field
 * but ``policies.max_no_progress`` ABSENT from the manifest (its backend
 * default of 0 is not the same as balanced's 4 — see form_model.ts's
 * ``PROFILE_BACKEND_DEFAULTS`` comment) — a forgotten field in
 * ``applyRunProfile``'s setter chain shows up here as a stray non-undefined
 * value.
 */
import { describe, expect, it } from "vitest";

import {
  applyRunProfile,
  countProfileDiff,
  inferRunProfile,
  readAbstainThreshold,
  readConsolidation,
  readContextGates,
  readDynamicWorkersOn,
  readMemoryBudgets,
  readRecallMode,
  readRewriteReads,
  readRunBudget,
  readTopK,
  readVerifyReads,
  setTopK,
  type AgentManifest,
} from "../form_model";
import { dumpYaml, parseYaml } from "../yaml";
import { BASE_MANIFEST_YAML } from "../defaults";

// A truly blank manifest has NO ``memory.long_term`` block — memory is OFF
// (presence semantics, ``readMemoryOn``), and presets never flip feature
// switches, so the 7 memory-backed fields are skipped on it. The "all 18
// fields" suites therefore run on ``BASE_ON`` (memory declared on, knobs
// untouched); ``BASE`` exercises the memory-off gating suite below.
const BASE: AgentManifest = { spec: {} };
const BASE_ON: AgentManifest = { spec: { memory: { long_term: {} } } };

describe("applyRunProfile", () => {
  it("writes all 18 fields for the 'cost' preset", () => {
    const m = applyRunProfile(BASE_ON, "cost");
    expect(readTopK(m)).toBe(3);
    expect(readVerifyReads(m)).toBe(false);
    expect(readRewriteReads(m)).toBe(false);
    expect(readRecallMode(m)).toBe("per_session");
    expect(readAbstainThreshold(m)).toBe(0.2);
    expect(readMemoryBudgets(m).injectionTokenBudget).toBe(1000);
    expect(readMemoryBudgets(m).correctionTokenBudget).toBe(300);
    expect(readConsolidation(m).consolidationEnabled).toBe(false);
    expect(readRunBudget(m).maxIterations).toBe(20);
    expect(readRunBudget(m).maxNoProgress).toBe(3);
    const gates = readContextGates(m);
    expect(gates.prThresholdPct).toBe(0.6);
    expect(gates.prRecentKept).toBe(2);
    expect(gates.wmThresholdPct).toBe(0.6);
    expect(gates.wmMaxRecentTurns).toBe(10);
    expect(gates.ccThresholdPct).toBe(0.6);
    expect(gates.ccHeadKeep).toBe(2);
    expect(gates.ccTailKeep).toBe(4);
    expect(readDynamicWorkersOn(m)).toBe(false);
  });

  it("writes all 18 fields for the 'capability' preset", () => {
    const m = applyRunProfile(BASE_ON, "capability");
    expect(readTopK(m)).toBe(8);
    expect(readVerifyReads(m)).toBe(true);
    expect(readRewriteReads(m)).toBe(true);
    expect(readRecallMode(m)).toBe("per_turn");
    expect(readAbstainThreshold(m)).toBe(0);
    expect(readMemoryBudgets(m).injectionTokenBudget).toBe(4000);
    expect(readMemoryBudgets(m).correctionTokenBudget).toBe(800);
    // consolidationEnabled's capability value (true) === its backend default
    // (true), so ``applyRunProfile`` omits the key entirely (the
    // "value===default → omit" convention) — the RAW reader legitimately
    // returns undefined here; the effective (UI-facing) value is still true.
    expect(readConsolidation(m).consolidationEnabled ?? true).toBe(true);
    expect(readRunBudget(m).maxIterations).toBe(60);
    expect(readRunBudget(m).maxNoProgress).toBe(6);
    const gates = readContextGates(m);
    expect(gates.prThresholdPct).toBe(0.8);
    expect(gates.prRecentKept).toBe(8);
    expect(gates.wmThresholdPct).toBe(0.8);
    expect(gates.wmMaxRecentTurns).toBe(40);
    expect(gates.ccThresholdPct).toBe(0.85);
    expect(gates.ccHeadKeep).toBe(6);
    expect(gates.ccTailKeep).toBe(10);
    expect(readDynamicWorkersOn(m)).toBe(true);
  });

  // "balanced" matches the backend default on every field but
  // maxNoProgress, so ``applyRunProfile`` omits every OTHER key — reading
  // through the RAW helpers (readTopK/readMemoryBudgets/readConsolidation/
  // readRunBudget/readContextGates) legitimately returns undefined for
  // those; ``?? <backend default>`` recovers the effective value a UI
  // control would show (mirrors e.g. ``MemorySection``'s own
  // ``readTopK(formData) ?? 5``).
  it("writes all 18 fields for the 'balanced' preset (effective values, from a blank manifest)", () => {
    const m = applyRunProfile(BASE_ON, "balanced");
    expect(readTopK(m) ?? 5).toBe(5);
    expect(readVerifyReads(m)).toBe(true);
    expect(readRewriteReads(m)).toBe(false);
    expect(readRecallMode(m)).toBe("per_session");
    expect(readAbstainThreshold(m)).toBe(0);
    expect(readMemoryBudgets(m).injectionTokenBudget ?? 2000).toBe(2000);
    expect(readMemoryBudgets(m).correctionTokenBudget ?? 500).toBe(500);
    expect(readConsolidation(m).consolidationEnabled ?? true).toBe(true);
    expect(readRunBudget(m).maxIterations ?? 30).toBe(30);
    expect(readRunBudget(m).maxNoProgress).toBe(4);
    const gates = readContextGates(m);
    expect(gates.prThresholdPct ?? 0.7).toBe(0.7);
    expect(gates.prRecentKept ?? 4).toBe(4);
    expect(gates.wmThresholdPct ?? 0.7).toBe(0.7);
    expect(gates.wmMaxRecentTurns ?? 20).toBe(20);
    expect(gates.ccThresholdPct ?? 0.7).toBe(0.7);
    expect(gates.ccHeadKeep ?? 4).toBe(4);
    expect(gates.ccTailKeep ?? 6).toBe(6);
    expect(readDynamicWorkersOn(m)).toBe(true);
  });

  it("applying 'balanced' clears every managed key EXCEPT policies.max_no_progress (=== 4, explicit)", () => {
    const m = applyRunProfile(applyRunProfile(BASE_ON, "capability"), "balanced");
    const spec = (m as { spec: Record<string, unknown> }).spec;
    const policies = spec.policies as Record<string, unknown> | undefined;
    // The one field whose balanced value (4) differs from its backend
    // default (0) — must survive as an explicit key.
    expect(policies?.max_no_progress).toBe(4);

    const longTerm = (
      spec.memory as { long_term?: Record<string, unknown> } | undefined
    )?.long_term;
    expect(longTerm?.retrieve_top_k).toBeUndefined();
    expect(longTerm?.verify_reads).toBeUndefined();
    expect(longTerm?.rewrite_reads).toBeUndefined();
    expect(longTerm?.recall_mode).toBeUndefined();
    expect(longTerm?.abstain_threshold).toBeUndefined();
    expect(longTerm?.injection_token_budget).toBeUndefined();
    expect(longTerm?.correction_token_budget).toBeUndefined();

    expect(policies?.memory_consolidation).toBeUndefined();
    expect(policies?.tool_result_prune).toBeUndefined();
    expect(policies?.working_memory).toBeUndefined();
    expect(policies?.context_compression).toBeUndefined();

    const workflow = spec.workflow as Record<string, unknown> | undefined;
    expect(workflow?.max_iterations).toBeUndefined();

    const dynamicWorkers = spec.dynamic_workers as
      | Record<string, unknown>
      | undefined;
    expect(dynamicWorkers?.enabled).toBeUndefined();

    // Every managed field reads back at its (balanced === backend-default)
    // effective value regardless of the above absences.
    expect(inferRunProfile(m)).toBe("balanced");
  });

  it("does not mutate the input manifest", () => {
    const m = applyRunProfile(BASE_ON, "capability");
    const snapshot = JSON.stringify(m);
    applyRunProfile(m, "balanced");
    expect(JSON.stringify(m)).toBe(snapshot);
  });

  it("round-trips through YAML (deleted keys stay deleted, not present-but-undefined)", () => {
    const m = applyRunProfile(applyRunProfile(BASE_ON, "capability"), "balanced");
    const reparsed = parseYaml(dumpYaml(m));
    expect(inferRunProfile(reparsed)).toBe("balanced");
    const spec = (reparsed as { spec: Record<string, unknown> }).spec;
    expect(
      (spec.memory as { long_term?: Record<string, unknown> })?.long_term
        ?.retrieve_top_k,
    ).toBeUndefined();
  });

  it("re-applying the same preset is idempotent", () => {
    const once = applyRunProfile(BASE_ON, "cost");
    const twice = applyRunProfile(once, "cost");
    expect(twice).toEqual(once);
  });
});

describe("inferRunProfile", () => {
  it("round-trips: applying a preset then inferring returns the same preset", () => {
    expect(inferRunProfile(applyRunProfile(BASE_ON, "balanced"))).toBe(
      "balanced",
    );
    expect(inferRunProfile(applyRunProfile(BASE_ON, "cost"))).toBe("cost");
    expect(inferRunProfile(applyRunProfile(BASE_ON, "capability"))).toBe(
      "capability",
    );
  });

  it("flips to 'custom' the moment a single managed field drifts", () => {
    const balanced = applyRunProfile(BASE_ON, "balanced");
    expect(inferRunProfile(balanced)).toBe("balanced");
    const drifted = setTopK(balanced, 999);
    expect(inferRunProfile(drifted)).toBe("custom");
  });

  it("a blank manifest is 'custom' (backend default max_no_progress=0 matches no preset)", () => {
    expect(inferRunProfile(BASE)).toBe("custom");
  });

  it("the default new-agent seed (defaults.ts) infers as 'balanced'", () => {
    const seed = parseYaml(BASE_MANIFEST_YAML);
    expect(inferRunProfile(seed)).toBe("balanced");
  });
});

describe("countProfileDiff", () => {
  it("is 0 once the manifest already matches the target preset", () => {
    const m = applyRunProfile(BASE_ON, "cost");
    expect(countProfileDiff(m, "cost")).toBe(0);
  });

  it("is 16 for a memory-on blank manifest vs. 'cost' (every field differs except rewriteReads/recallMode, which cost shares with the backend default)", () => {
    expect(countProfileDiff(BASE_ON, "cost")).toBe(16);
  });

  it("is 11 for a memory-OFF blank manifest vs. 'cost' (only the 11 non-memory applicable fields are compared)", () => {
    expect(countProfileDiff(BASE, "cost")).toBe(11);
  });

  it("counts exactly 1 after a single field is hand-edited off a matched preset", () => {
    const balanced = applyRunProfile(BASE_ON, "balanced");
    const drifted = setTopK(balanced, 999);
    expect(countProfileDiff(drifted, "balanced")).toBe(1);
  });
});

// ---- backend full-dump echo (#12) ----
// The control plane never stores/echoes the sparse manifest the editor
// submits: PUT re-validates through Pydantic and persists
// ``spec.model_dump(by_alias=True, mode="json")`` (api/agents.py
// ``_load_manifest``), and GET re-dumps the record — so EVERY defaulted key
// comes back explicit and every unset optional key comes back as literal
// ``null`` (``extends: null`` / ``short_term: null`` / ``builder: null`` /
// ``aux_model: null`` …). After a save the editor resyncs its working copy
// from that echo (ManifestTab snapshot resync), so ``inferRunProfile`` must
// keep recognizing an applied preset on the full-dump shape, not just on
// the sparse apply output. Fixtures below mirror the real dump byte-for-
// byte for the managed-field subtree (generated via
// ``AgentSpec.model_validate`` + ``model_dump(by_alias=True, mode="json")``
// on the current protocol package).
interface EchoValues {
  topK: number;
  verifyReads: boolean;
  rewriteReads: boolean;
  recallMode: string;
  abstainThreshold: number;
  injectionTokenBudget: number;
  correctionTokenBudget: number;
  consolidationEnabled: boolean;
  maxIterations: number;
  maxNoProgress: number;
  prThresholdPct: number;
  prRecentKept: number;
  wmThresholdPct: number;
  wmMaxRecentTurns: number;
  ccThresholdPct: number;
  ccHeadKeep: number;
  ccTailKeep: number;
  dynamicWorkersOn: boolean;
}

// Returns ``unknown`` (the readers' own input type) — the fixture mirrors
// the wire shape, not the editor's ``AgentManifest`` view of it.
function backendEcho(v: EchoValues, memoryOn: boolean): unknown {
  return {
    spec: {
      extends: null,
      memory: memoryOn
        ? {
            short_term: null,
            long_term: {
              retrieve_top_k: v.topK,
              write_back: true,
              recall_mode: v.recallMode,
              reconcile_writes: true,
              write_min_importance: 0.3,
              verify_reads: v.verifyReads,
              rewrite_reads: v.rewriteReads,
              abstain_threshold: v.abstainThreshold,
              injection_token_budget: v.injectionTokenBudget,
              correction_token_budget: v.correctionTokenBudget,
            },
          }
        : null,
      workflow: {
        type: "react",
        max_iterations: v.maxIterations,
        early_stop: {},
        builder: null,
      },
      policies: {
        context_compression: {
          enabled: true,
          threshold_pct: v.ccThresholdPct,
          head_keep: v.ccHeadKeep,
          tail_keep: v.ccTailKeep,
          flush_before_compaction: true,
          max_passes: 3,
          max_turns: null,
          max_tokens: null,
          pressure_feedback: true,
          pressure_warn_pct: 0.75,
        },
        working_memory: {
          enabled: true,
          threshold_pct: v.wmThresholdPct,
          max_recent_turns: v.wmMaxRecentTurns,
          keep_first_turn: true,
        },
        tool_result_prune: {
          enabled: true,
          threshold_pct: v.prThresholdPct,
          recent_tool_results_kept: v.prRecentKept,
        },
        tool_output_budget: { enabled: true },
        memory_consolidation: {
          enabled: v.consolidationEnabled,
          aux_model: null,
        },
        max_no_progress: v.maxNoProgress,
        token_budget: 0,
        run_deadline_s: 0,
      },
      dynamic_workers: { enabled: v.dynamicWorkersOn },
    },
  };
}

// Each preset's values as the backend echoes them (all 18 explicit).
const ECHO_VALUES: Record<"balanced" | "cost" | "capability", EchoValues> = {
  balanced: {
    topK: 5,
    verifyReads: true,
    rewriteReads: false,
    recallMode: "per_session",
    abstainThreshold: 0,
    injectionTokenBudget: 2000,
    correctionTokenBudget: 500,
    consolidationEnabled: true,
    maxIterations: 30,
    maxNoProgress: 4,
    prThresholdPct: 0.7,
    prRecentKept: 4,
    wmThresholdPct: 0.7,
    wmMaxRecentTurns: 20,
    ccThresholdPct: 0.7,
    ccHeadKeep: 4,
    ccTailKeep: 6,
    dynamicWorkersOn: true,
  },
  cost: {
    topK: 3,
    verifyReads: false,
    rewriteReads: false,
    recallMode: "per_session",
    abstainThreshold: 0.2,
    injectionTokenBudget: 1000,
    correctionTokenBudget: 300,
    consolidationEnabled: false,
    maxIterations: 20,
    maxNoProgress: 3,
    prThresholdPct: 0.6,
    prRecentKept: 2,
    wmThresholdPct: 0.6,
    wmMaxRecentTurns: 10,
    ccThresholdPct: 0.6,
    ccHeadKeep: 2,
    ccTailKeep: 4,
    dynamicWorkersOn: false,
  },
  capability: {
    topK: 8,
    verifyReads: true,
    rewriteReads: true,
    recallMode: "per_turn",
    abstainThreshold: 0,
    injectionTokenBudget: 4000,
    correctionTokenBudget: 800,
    consolidationEnabled: true,
    maxIterations: 60,
    maxNoProgress: 6,
    prThresholdPct: 0.8,
    prRecentKept: 8,
    wmThresholdPct: 0.8,
    wmMaxRecentTurns: 40,
    ccThresholdPct: 0.85,
    ccHeadKeep: 6,
    ccTailKeep: 10,
    dynamicWorkersOn: true,
  },
};

describe("backend full-dump echo (#12)", () => {
  for (const profile of ["balanced", "cost", "capability"] as const) {
    it(`recognizes '${profile}' on the memory-ON echo (explicit defaults + nulls)`, () => {
      expect(inferRunProfile(backendEcho(ECHO_VALUES[profile], true))).toBe(
        profile,
      );
    });

    it(`recognizes '${profile}' on the memory-OFF echo (memory: null)`, () => {
      expect(inferRunProfile(backendEcho(ECHO_VALUES[profile], false))).toBe(
        profile,
      );
    });

    it(`apply→save-payload round-trip on the '${profile}' echo stays '${profile}'`, () => {
      // Editing an echoed manifest: apply another pass of the same preset
      // (idempotent) and YAML round-trip like the PUT payload does.
      const applied = applyRunProfile(backendEcho(ECHO_VALUES[profile], true), profile);
      expect(inferRunProfile(applied)).toBe(profile);
      expect(inferRunProfile(parseYaml(dumpYaml(applied)))).toBe(profile);
    });
  }

  it("explicit null knobs (the ``extends: null`` class) fall back to backend defaults, not mismatch", () => {
    // Apply "balanced" (deletes every managed key but max_no_progress), then
    // simulate an echo that materializes those absent keys as explicit null —
    // the ``??`` readers must fold null back to the backend default so the
    // preset still reads back.
    const applied = applyRunProfile(BASE_ON, "balanced");
    const spec = (JSON.parse(JSON.stringify(applied)) as {
      spec: Record<string, unknown>;
    }).spec;
    spec.extends = null;
    const memory = spec.memory as { long_term: Record<string, unknown> };
    for (const k of [
      "retrieve_top_k",
      "verify_reads",
      "rewrite_reads",
      "recall_mode",
      "abstain_threshold",
      "injection_token_budget",
      "correction_token_budget",
    ]) {
      memory.long_term[k] = null;
    }
    spec.workflow = { max_iterations: null, builder: null };
    const policies = spec.policies as Record<string, unknown>;
    policies.memory_consolidation = null;
    policies.context_compression = {
      threshold_pct: null,
      head_keep: null,
      tail_keep: null,
      max_turns: null,
    };
    policies.working_memory = { threshold_pct: null, max_recent_turns: null };
    policies.tool_result_prune = {
      threshold_pct: null,
      recent_tool_results_kept: null,
    };
    spec.dynamic_workers = { enabled: null };

    expect(inferRunProfile({ spec })).toBe("balanced");
  });
});

// Memory-off gating (spec §③: presets tune knobs, never flip feature
// switches). ``long_term``'s PRESENCE is the memory on/off switch, so an
// apply on a memory-off manifest must not materialize it — and infer must
// match on the 11 applicable fields so the just-applied preset reads back.
describe("memory-off gating", () => {
  it("applying 'cost' on a memory-off manifest leaves memory OFF (no long_term materialized)", () => {
    const m = applyRunProfile(BASE, "cost");
    const spec = (m as { spec: Record<string, unknown> }).spec;
    expect(
      (spec.memory as { long_term?: unknown } | undefined)?.long_term,
    ).toBeUndefined();
    // the 11 non-memory fields still landed
    expect(readRunBudget(m).maxIterations).toBe(20);
    expect(readDynamicWorkersOn(m)).toBe(false);
    expect(readContextGates(m).ccThresholdPct).toBe(0.6);
  });

  it("applying 'balanced' on a memory-off manifest does not materialize an empty long_term block", () => {
    const m = applyRunProfile(BASE, "balanced");
    const spec = (m as { spec: Record<string, unknown> }).spec;
    expect(spec.memory).toBeUndefined();
    expect((spec.policies as Record<string, unknown>).max_no_progress).toBe(4);
  });

  it("apply→infer round-trips on a memory-off manifest (11 applicable fields decide)", () => {
    expect(inferRunProfile(applyRunProfile(BASE, "cost"))).toBe("cost");
    expect(inferRunProfile(applyRunProfile(BASE, "capability"))).toBe(
      "capability",
    );
    expect(inferRunProfile(applyRunProfile(BASE, "balanced"))).toBe(
      "balanced",
    );
  });
});
