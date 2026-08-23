export interface ModelFields {
  provider?: string;
  name?: string;
  supports_vision?: boolean;
  temperature?: number;
  max_tokens?: number;
  rate_limit_rpm?: number;
  // Declared context window in tokens; drives the compaction threshold
  // (context_window * threshold_pct). Undefined = resolve from the model
  // catalog, falling back to 200k (agent_factory._resolved_context_window).
  context_window?: number;
  // Thinking-Toggle — tri-state on/off (undefined = inherit vendor default).
  thinking_enabled?: boolean;
  // Reasoning effort tier (low/medium/high/max). Undefined = provider
  // default. Only meaningful when the catalog entry has a thinking knob —
  // the ModelSelect widget gates the control on that same condition.
  effort?: string;
  // Adaptive thinking (Anthropic 4.6+) — model decides its own depth.
  // Anthropic-only; undefined (default) = off.
  adaptive_thinking?: boolean;
  // Anthropic prompt caching. Anthropic-only; default TRUE (undefined = on),
  // explicit ``false`` = off.
  cache_enabled?: boolean;
  // E.11 — provider fallback chain, flat + ordered: primary → fallback[0] →
  // fallback[1] → …. The backend pre-order-flattens the tree
  // (agent_factory._flatten_chain), so a hand-authored nested sub-chain on an
  // entry is preserved and walked before the next sibling.
  fallback?: ModelFields[];
  [k: string]: unknown;
}
export interface LongTermFields {
  retrieve_top_k?: number;
  write_back?: boolean;
  recall_mode?: string;
  // Stream Memory-Enhance (M-3) — read-time verification of recalled memories.
  verify_reads?: boolean;
  // Stream Memory-Enhance (M-2) — importance floor for run-end write-back.
  write_min_importance?: number;
  // Stream CM-7 — Mem0-style reconcile (dedup/supersede) at write-back.
  reconcile_writes?: boolean;
  // Stream RT-2 PR-2 (RT-ADR-10) — token ceiling for the injected
  // recalled-memory block (LongTermMemorySpec.injection_token_budget).
  injection_token_budget?: number;
  // Stream RT-2 PR-2 — guaranteed token slice for user-corrected
  // (confidence=1.0) memories within the injection budget.
  correction_token_budget?: number;
  // Stream P5a (Task 7) — rewrite the user's message into a search query before recall.
  rewrite_reads?: boolean;
  // Stream P5a (Task 8) — abstain (inject nothing) when top candidate similarity < this.
  abstain_threshold?: number;
}
export interface RouteRuleFields {
  when?: string;
  model?: ModelFields;
  [k: string]: unknown;
}
export interface RoutingFields {
  rules?: RouteRuleFields[];
  [k: string]: unknown;
}
export type ToolEntry = {
  type: string;
  name?: string;
  allow_tools?: string[];
  servers?: string[];
  config?: Record<string, unknown>;
  [k: string]: unknown;
};
export interface AgentManifest {
  apiVersion?: string;
  kind?: string;
  metadata?: {
    name?: string;
    version?: string;
    tenant?: string;
    [k: string]: unknown;
  };
  spec?: {
    description?: string;
    display_name?: string;
    model?: ModelFields;
    system_prompt?: {
      template?: string;
      // Dynamic-Prompt — opt-in run-time Jinja rendering of the template.
      jinja?: boolean;
      variables?: PromptVariableFields[];
      [k: string]: unknown;
    };
    // DynamicContextSpec — build-time context injected alongside the system
    // prompt. Only ``inject_current_date`` is form-curated; ``inject_memory``
    // (reserved, not read at runtime — see ``MemorySection``) and
    // ``custom_reminders`` (a structured list, YAML-only) survive as unknown
    // keys.
    dynamic_context?: { inject_current_date?: boolean; [k: string]: unknown };
    memory?: { long_term?: LongTermFields | null; [k: string]: unknown } | null;
    // Stream J.11 — reflect-node config. PRESENCE is the on/off switch: `{}`
    // = on with defaults (budget 2 / deadline_s 30), absent OR explicit
    // `null` = off. See ``readReflectionOn``/``setReflectionOn``.
    reflection?: { budget?: number; deadline_s?: number; [k: string]: unknown } | null;
    tools?: ToolEntry[];
    // Sandbox egress policy (NetworkSpec) — nested two levels under spec.
    // ``sandbox`` itself commonly carries unrelated unknown keys authored in
    // YAML (runtime/image/resources/filesystem) that must survive untouched.
    sandbox?: {
      network?: {
        egress?: string;
        allowlist?: string[];
        denylist?: string[];
        [k: string]: unknown;
      };
      filesystem?: { persistent_workspace?: boolean; [k: string]: unknown };
      [k: string]: unknown;
    };
    routing?: RoutingFields | null;
    // Stream J.6 Path B — VL fallback for a text-only main model (ask_image).
    vision?: {
      model?: ModelFields;
      fallbacks?: ModelFields[];
      [k: string]: unknown;
    } | null;
    // Declarative human-approval gate — tool names that pause the run for a
    // human verdict before they execute (the governance counterweight to the
    // always-on exec_python base capability).
    policies?: {
      approval_required_tools?: string[];
      // Seconds a pending approval may sit before auto-reject (default 24h).
      approval_timeout_s?: number;
      clarification_timeout_s?: number;
      // Wall-clock cap on the whole run incl. sub-agent recursion (0 = off).
      run_deadline_s?: number;
      // No-progress stop — consecutive loop-detection trips after which the
      // ReAct loop force-wraps up early (0 = off).
      max_no_progress?: number;
      // Stream L.L7 — record completed runs to ObjectStore (privacy toggle).
      trajectory_recording?: boolean;
      // Tool-call-rate uplift — whether to append the tool-use enforcement
      // block to the system prompt. "auto" (default) enables it for every
      // model except the families that reliably self-initiate tool calls.
      tool_use_enforcement?: string;
      // Phase 3 — per-agent master switch for the tool-output-budget feature
      // (externalization + persist + prune). Block absent = enabled.
      tool_output_budget?: { enabled?: boolean; [k: string]: unknown };
      // Stream L.L2 — per-agent context compression knobs (ContextCompressionPolicy).
      context_compression?: {
        enabled?: boolean;
        threshold_pct?: number;
        head_keep?: number;
        tail_keep?: number;
        flush_before_compaction?: boolean;
        max_passes?: number;
        max_turns?: number;
        max_tokens?: number;
        pressure_feedback?: boolean;
        pressure_warn_pct?: number;
        [k: string]: unknown;
      };
      // Stream CM-2 — working-memory sliding-window knobs (WorkingMemoryPolicy).
      working_memory?: {
        enabled?: boolean;
        threshold_pct?: number;
        max_recent_turns?: number;
        keep_first_turn?: boolean;
        [k: string]: unknown;
      };
      // Stream CM-12 — mechanical tool-result prune gate knobs (ToolResultPrunePolicy).
      tool_result_prune?: {
        enabled?: boolean;
        threshold_pct?: number;
        recent_tool_results_kept?: number;
        [k: string]: unknown;
      };
      // Capability Uplift Sprint #7 (Mini-ADR U-39) — per-agent
      // MemoryConsolidator master switch (default_factory ⇒ absent block =
      // enabled with the platform-default aux_model).
      memory_consolidation?: { enabled?: boolean; [k: string]: unknown };
      // B3 — per-run total token budget across the whole delegation tree
      // (main agent + all workers), 0/absent = disabled.
      token_budget?: number;
      [k: string]: unknown;
    } | null;
    // Group 6 试点(运行预算与超时) — ReAct loop step budget + free-form knobs
    // (early_stop, builder) authored via YAML. The curated form surfaces
    // max_iterations and type (react/plan_execute/custom).
    workflow?: { max_iterations?: number; type?: string; [k: string]: unknown };
    // Stream L (P1) — time-to-first-token budget for a single LLM provider
    // call (0 = disabled). Top-level spec key (sibling of policies), not
    // nested under policies.
    stream_deadline_s?: number;
    // Stream L (P1) — inter-token idle cap once streaming has started
    // (0 = disabled). Top-level spec key (sibling of policies).
    idle_timeout_s?: number;
    // Orchestrator-Worker — whether the agent may spawn ephemeral workers at
    // run time (spawn_worker). Block absent = enabled (the platform default).
    dynamic_workers?: { enabled?: boolean; [k: string]: unknown } | null;
    // RAG — tenant knowledge bases this agent may search (activates the
    // knowledge_search tool). Block absent = no knowledge access.
    knowledge?: { knowledge_base_refs?: string[]; [k: string]: unknown } | null;
    // Stream RT-1 (RT-ADR-4) — structured final reply: a JSON Schema the
    // agent's FINAL assistant message must validate against (intermediate
    // tool-calling turns are unaffected). Flat schemas are editable in the
    // curated form's field-list editor (config-page redesign v2 Task 7 —
    // see the "structured output field editor" section below); non-flat
    // schemas stay YAML-only behind a read-only notice.
    output_schema?: {
      name?: string;
      json_schema?: Record<string, unknown>;
      strict?: boolean;
      [k: string]: unknown;
    } | null;
    // Attached skills — skill refs (``name`` or ``name@N``) the agent loads.
    skills?: string[];
    // SE-16 (SE-A42) — opt-in: build auto-attaches this agent's own ACTIVE
    // distilled skills (lazy, summary only). Absent = off.
    auto_attach_evolved_skills?: boolean;
    // Static delegation — named sub-agents (agent_ref to a deployed agent)
    // the parent may delegate to via a per-subagent tool.
    subagents?: SubAgentFields[];
    // Safety posture — the DefenseSpec switches, surfaced in the "defenses" form
    // section. Every field optional; an absent field takes its DefenseSpec
    // default (output_screen=block, prompt_injection=spotlight, rest off/open).
    defenses?: {
      prompt_injection?: string;
      output_screen?: string;
      output_judge?: string;
      output_judge_on_error?: string;
      action_screen?: string;
      action_screen_on_error?: string;
      output_dlp?: string;
      [k: string]: unknown;
    } | null;
    // Template this agent extends (AgentSpecBody.extends). Presence drives the
    // "template may enforce stricter defenses" hint in the defenses section.
    // The backend's full dump writes ``extends: null`` when unset — readExtends
    // folds that back to undefined so the hint doesn't misfire (#5).
    extends?: string | null;
    // Stream K.K4 (Mini-ADR K-3) — per-agent LLM response cache opt-out
    // (CacheSpec). DISTINCT from ``model.cache_enabled`` (ModelFields,
    // Anthropic prompt caching) — see ``ObservabilityFields`` below. Backend
    // default_factory ⇒ absent block = defaults (enabled=true).
    cache?: { enabled?: boolean; [k: string]: unknown };
    [k: string]: unknown;
  };
  [k: string]: unknown;
}

export interface SubAgentFields {
  name?: string;
  agent_ref?: string;
  description?: string;
  [k: string]: unknown;
}

export interface PromptVariableFields {
  name?: string;
  // ``true`` (default) → value rendered verbatim; ``false`` → spotlight-fenced
  // as DATA before substitution.
  trusted?: boolean;
  // ``true`` (default) → a run missing this input is rejected.
  required?: boolean;
  description?: string;
  [k: string]: unknown;
}

// ---- run profile presets (config-page redesign v2 Task 6) ----
// Three curated one-click presets over 18 previously-scattered "how hard
// does this agent think/remember" knobs (RunProfileCard, ``groups/
// BasicSection.tsx``). ``RunProfile`` is a concrete preset name; the wider
// ``RunProfileState`` adds "custom" — the UI state once any managed field
// has drifted off every preset's exact values.
export type RunProfile = "balanced" | "cost" | "capability";
export type RunProfileState = RunProfile | "custom";

// The 18 managed fields, camelCased to match each field's existing read/patch
// helper (``readTopK``/``patchRunBudget``/etc. — see ``applyRunProfile``).
interface RunProfileValues {
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

// Single source of truth for each managed field's BACKEND default (the value
// a patch omits entirely — see ``applyRunProfile``'s ``orDefault``). Must stay
// numerically identical to the matching ``FieldDef.effectiveDefault`` in
// ``groups/RunBudgetSection.tsx`` (max_iterations/max_no_progress),
// ``groups/ContextGatesSection.tsx`` (pr_/wm_/cc_ thresholds+keeps),
// ``groups/MemorySection.tsx`` (top_k/verify_reads/rewrite_reads/recall_mode/
// abstain_threshold/injection+correction budgets/consolidation), and
// ``readDynamicWorkersOn`` below (dynamic_workers) — those files' own
// comments point back here. NOTE ``maxNoProgress``'s backend default is 0,
// which is NOT any preset's value (balanced/cost/capability write 4/3/6) — a
// brand-new agent only reads as "balanced" because ``defaults.ts`` seeds
// ``policies.max_no_progress: 4`` explicitly (see
// ``form_model_profiles.test.ts``'s seed↔balanced consistency test).
const PROFILE_BACKEND_DEFAULTS: RunProfileValues = {
  topK: 5,
  verifyReads: true,
  rewriteReads: false,
  recallMode: "per_session",
  abstainThreshold: 0,
  injectionTokenBudget: 2000,
  correctionTokenBudget: 500,
  consolidationEnabled: true,
  maxIterations: 30,
  maxNoProgress: 0,
  prThresholdPct: 0.7,
  prRecentKept: 4,
  wmThresholdPct: 0.7,
  wmMaxRecentTurns: 20,
  ccThresholdPct: 0.7,
  ccHeadKeep: 4,
  ccTailKeep: 6,
  dynamicWorkersOn: true,
};

// The three presets' target values — spec §③'s authoritative table.
const RUN_PROFILES: Record<RunProfile, RunProfileValues> = {
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

export const RUN_PROFILE_IDS: readonly RunProfile[] = ["balanced", "cost", "capability"];

function asObj(v: unknown): AgentManifest {
  return v !== null && typeof v === "object" && !Array.isArray(v)
    ? (v as AgentManifest)
    : {};
}
function specOf(m: unknown): NonNullable<AgentManifest["spec"]> {
  return asObj(m).spec ?? {};
}
function patchSpec(m: unknown, spec: Record<string, unknown>): AgentManifest {
  const base = asObj(m);
  return { ...base, spec: { ...specOf(base), ...spec } };
}

// Merge a partial patch into ``spec.defenses`` preserving siblings. A patch
// value of ``undefined`` DELETES that key (a setter signalling "back to the
// DefenseSpec default → omit"). When the merge empties ``defenses``, the whole
// block is dropped so the manifest stays clean (js-yaml omits ``undefined``).
function patchDefenses(
  m: unknown,
  patch: Record<string, string | undefined>,
): AgentManifest {
  const merged: Record<string, unknown> = { ...(specOf(m).defenses ?? {}) };
  for (const [k, v] of Object.entries(patch)) {
    if (v === undefined) delete merged[k];
    else merged[k] = v;
  }
  return patchSpec(m, {
    defenses: Object.keys(merged).length > 0 ? merged : undefined,
  });
}

// ---- readers ----
export const readName = (m: unknown): string => asObj(m).metadata?.name ?? "";
export const readDescription = (m: unknown): string =>
  specOf(m).description ?? "";
export const readDisplayName = (m: unknown): string =>
  specOf(m).display_name ?? "";
export const readModel = (m: unknown): ModelFields => specOf(m).model ?? {};
// E.11 — the main model's fallback chain (flat list, primary excluded).
export const readFallback = (m: unknown): ModelFields[] => {
  const fb = readModel(m).fallback;
  return Array.isArray(fb) ? (fb as ModelFields[]) : [];
};
export const readSystemPrompt = (m: unknown): string =>
  specOf(m).system_prompt?.template ?? "";
export const readMemoryOn = (m: unknown): boolean =>
  (specOf(m).memory?.long_term ?? null) !== null;
export const readTopK = (m: unknown): number | undefined =>
  specOf(m).memory?.long_term?.retrieve_top_k;
// long_term knob readers — each defaults to the backend default so an
// unset field reads as its effective value (LongTermMemorySpec). The four
// run-profile-managed knobs (verify/recall/rewrite/abstain) take their
// fallback from ``PROFILE_BACKEND_DEFAULTS`` instead of an inline literal
// (#12): ``inferRunProfile`` compares these readers' effective values
// against ``RUN_PROFILES``, so a hand-maintained duplicate drifting from
// the table would silently make a matching preset read as "custom".
export const readWriteBack = (m: unknown): boolean =>
  specOf(m).memory?.long_term?.write_back ?? true;
export const readVerifyReads = (m: unknown): boolean =>
  specOf(m).memory?.long_term?.verify_reads ??
  PROFILE_BACKEND_DEFAULTS.verifyReads;
export const readWriteMinImportance = (m: unknown): number =>
  specOf(m).memory?.long_term?.write_min_importance ?? 0.3;
export const readReconcileWrites = (m: unknown): boolean =>
  specOf(m).memory?.long_term?.reconcile_writes ?? true;
export const readRecallMode = (m: unknown): string =>
  specOf(m).memory?.long_term?.recall_mode ??
  PROFILE_BACKEND_DEFAULTS.recallMode;
export const readRewriteReads = (m: unknown): boolean =>
  specOf(m).memory?.long_term?.rewrite_reads ??
  PROFILE_BACKEND_DEFAULTS.rewriteReads;
export const readAbstainThreshold = (m: unknown): number =>
  specOf(m).memory?.long_term?.abstain_threshold ??
  PROFILE_BACKEND_DEFAULTS.abstainThreshold;

// ---- reflection evaluator (Stream J.11 routing — the `when=reflection` rule) ----
// The "reflection evaluator model" friendly control is a curated view over the
// existing ``routing`` block: an independent evaluator is just a route rule that
// sends the reflection step to its own model. Empty = no rule = reflection reuses
// the agent's own model (the safe default).
export const readReflectionEvaluator = (m: unknown): ModelFields | undefined =>
  (specOf(m).routing?.rules ?? []).find((r) => r.when === "reflection")?.model;

export const readReflectionEvaluatorOn = (m: unknown): boolean =>
  readReflectionEvaluator(m) !== undefined;

// ---- vision fallback (Stream J.6 Path B — the ``vision:`` block) ----
// When the main model is NOT vision-capable, a separate VL model handles image
// understanding via the ``ask_image`` tool. Empty = no vision block = the agent
// can't read images (the safe default for a text-only model).
export const readVisionModel = (m: unknown): ModelFields | undefined =>
  specOf(m).vision?.model;
export const readVisionOn = (m: unknown): boolean =>
  readVisionModel(m) !== undefined;
export const readMainSupportsVision = (m: unknown): boolean =>
  readModel(m).supports_vision === true;

export interface ToolFlags {
  webSearch: boolean;
  http: boolean;
  mcp: boolean;
  mcpAllowTools: string[];
  mcpServers: string[];
}
export function readTools(m: unknown): ToolFlags {
  const tools = specOf(m).tools ?? [];
  const mcp = tools.find((t) => t.type === "mcp");
  return {
    webSearch: tools.some(
      (t) => t.type === "builtin" && t.name === "web_search",
    ),
    http: tools.some((t) => t.type === "http"),
    mcp: mcp !== undefined,
    mcpAllowTools: mcp?.allow_tools ?? [],
    mcpServers: mcp?.servers ?? [],
  };
}

// ---- writers (immutable; preserve siblings) ----
export function setName(m: unknown, name: string): AgentManifest {
  const base = asObj(m);
  return { ...base, metadata: { ...(base.metadata ?? {}), name } };
}
export const setDescription = (
  m: unknown,
  description: string,
): AgentManifest => patchSpec(m, { description });
export const setDisplayName = (
  m: unknown,
  display_name: string,
): AgentManifest => patchSpec(m, { display_name });
export function setModel(m: unknown, model: ModelFields): AgentManifest {
  return patchSpec(m, { model: { ...readModel(m), ...model } });
}
// E.11 — replace the main model's fallback chain. An empty chain drops the key
// so a single-provider agent's manifest stays byte-clean.
export function setFallback(m: unknown, chain: ModelFields[]): AgentManifest {
  const model: ModelFields = { ...readModel(m) };
  if (chain.length === 0) {
    delete model.fallback;
  } else {
    model.fallback = chain;
  }
  return patchSpec(m, { model });
}

// Serialize-boundary normalization. Drop fallback entries the backend would
// reject: an added-but-unfilled row (no provider/name), or a (provider, name)
// that repeats the primary or an earlier entry (``_check_fallback_chain`` treats
// a repeat as a cycle). Pruning here — not in ``setFallback`` — keeps the "Add"
// button able to show an empty row to fill in-form; only the submitted manifest
// is cleaned. Preserves every other field (incl. an entry's own nested chain).
export function normalizeForSubmit(m: unknown): AgentManifest {
  const primary = readModel(m);
  const seen = new Set<string>();
  // JSON-array key: collision-proof (JSON escaping separates the parts)
  // and plain ASCII — an earlier NUL-byte separator made grep/file
  // classify this whole module as binary, silently exempting it from
  // line-oriented tooling.
  const key = (e: ModelFields): string => JSON.stringify([e.provider, e.name]);
  if (primary.provider && primary.name) seen.add(key(primary));
  const pruned = readFallback(m).filter((e) => {
    if (!e.provider || !e.name || seen.has(key(e))) return false;
    seen.add(key(e));
    return true;
  });
  return setFallback(m, pruned);
}
export function setSystemPrompt(m: unknown, template: string): AgentManifest {
  return patchSpec(m, {
    system_prompt: { ...(specOf(m).system_prompt ?? {}), template },
  });
}

// ---- dynamic prompt (system_prompt.jinja + variables) ----
// Opt-in Jinja mode. ``off`` drops both ``jinja`` and ``variables`` so a plain
// agent's manifest stays clean (and satisfies the backend rule that variables
// require jinja). ``on`` sets ``jinja:true``; variable rows are stored verbatim
// (an in-progress row may be partial — validation happens on save).
export const readPromptJinja = (m: unknown): boolean =>
  specOf(m).system_prompt?.jinja === true;

export const readPromptVariables = (m: unknown): PromptVariableFields[] =>
  specOf(m).system_prompt?.variables ?? [];

export function setPromptJinja(m: unknown, on: boolean): AgentManifest {
  const sp = specOf(m).system_prompt ?? {};
  if (on) return patchSpec(m, { system_prompt: { ...sp, jinja: true } });
  const { jinja: _j, variables: _v, ...rest } = sp;
  return patchSpec(m, { system_prompt: rest });
}

export function setPromptVariables(
  m: unknown,
  rows: PromptVariableFields[],
): AgentManifest {
  const sp = specOf(m).system_prompt ?? {};
  if (rows.length === 0) {
    const { variables: _dropped, ...rest } = sp;
    return patchSpec(m, { system_prompt: rest });
  }
  return patchSpec(m, { system_prompt: { ...sp, variables: rows } });
}

// ---- dynamic context — inject_current_date (DynamicContextSpec) ----
// Whether the build writes today's date into the system prompt
// (agent_factory reads ``dynamic_context.inject_current_date`` — a
// day-granular, cache-stable injection). Like ``policies
// .memory_consolidation`` / ``spec.cache``, ``spec.dynamic_context`` has a
// backend ``default_factory`` (an absent block ⇒ every field at its Pydantic
// default, ``inject_current_date: True`` among them), so it follows the
// standard optional-block ``mergeBlock`` idiom: ``true`` (the default) drops
// the key entirely (js-yaml omits ``undefined``), ``false`` writes it.
// ``custom_reminders`` and any other unknown key already in the block
// survive untouched. Reader is RAW — no default substitution (the FormView
// widget supplies the effective default, true).
export const readInjectCurrentDate = (m: unknown): boolean | undefined =>
  specOf(m).dynamic_context?.inject_current_date;

export function setInjectCurrentDate(m: unknown, v: boolean): AgentManifest {
  const merged = mergeBlock(
    specOf(m).dynamic_context as Record<string, unknown> | undefined,
    { inject_current_date: v === true ? undefined : v },
  );
  return patchSpec(m, { dynamic_context: merged });
}

export function setMemoryOn(m: unknown, on: boolean): AgentManifest {
  const memory = specOf(m).memory ?? {};
  if (!on) return patchSpec(m, { memory: { ...memory, long_term: null } });
  const existing = specOf(m).memory?.long_term ?? null;
  const lt: LongTermFields = existing ?? {
    retrieve_top_k: 5,
    write_back: true,
    recall_mode: "per_session",
  };
  return patchSpec(m, { memory: { ...memory, long_term: lt } });
}
// Merge a partial patch into ``memory.long_term`` preserving siblings. Used by
// every long_term knob setter so toggling one never clobbers the others. A
// patch value of ``undefined`` DELETES that key (``applyRunProfile``'s
// "value === backend default → omit" convention) rather than merely
// assigning it a literal ``undefined`` — genuine deletion, not just a
// same-serialized-output shortcut, mirrors ``patchDefenses``'s idiom above
// and keeps ``Object.keys(long_term)`` (and any future "is this block
// empty" check) accurate. Unlike ``patchDefenses``/``mergeBlock``, an
// emptied result stays ``{}`` rather than dropping ``long_term`` itself:
// ``long_term``'s PRESENCE is the memory on/off switch (``readMemoryOn``),
// so dropping it here would silently turn memory off.
function patchLongTerm(
  m: unknown,
  patch: Partial<LongTermFields>,
): AgentManifest {
  const memory = specOf(m).memory ?? {};
  const lt: Record<string, unknown> = { ...(specOf(m).memory?.long_term ?? {}) };
  for (const [k, v] of Object.entries(patch)) {
    if (v === undefined) delete lt[k];
    else lt[k] = v;
  }
  return patchSpec(m, { memory: { ...memory, long_term: lt as LongTermFields } });
}
export const setTopK = (m: unknown, k: number): AgentManifest =>
  patchLongTerm(m, { retrieve_top_k: k });
export const setWriteBack = (m: unknown, on: boolean): AgentManifest =>
  patchLongTerm(m, { write_back: on });
export const setVerifyReads = (m: unknown, on: boolean): AgentManifest =>
  patchLongTerm(m, { verify_reads: on });
export const setWriteMinImportance = (m: unknown, v: number): AgentManifest =>
  patchLongTerm(m, { write_min_importance: v });
export const setReconcileWrites = (m: unknown, on: boolean): AgentManifest =>
  patchLongTerm(m, { reconcile_writes: on });
export const setRecallMode = (m: unknown, mode: string): AgentManifest =>
  patchLongTerm(m, { recall_mode: mode });
export const setRewriteReads = (m: unknown, on: boolean): AgentManifest =>
  patchLongTerm(m, { rewrite_reads: on });
export const setAbstainThreshold = (m: unknown, v: number): AgentManifest =>
  patchLongTerm(m, { abstain_threshold: v });
export function setReflectionEvaluator(
  m: unknown,
  model: ModelFields | null,
): AgentManifest {
  const routing = specOf(m).routing ?? {};
  // Preserve any other route rules (e.g. a planning rule); only touch reflection.
  const others = (routing.rules ?? []).filter((r) => r.when !== "reflection");
  const keep =
    model !== null &&
    (model.provider !== undefined || model.name !== undefined);
  const rules = keep ? [...others, { when: "reflection", model }] : others;
  if (rules.length === 0) {
    // Drop ``rules`` entirely; if routing then has no other keys, drop routing
    // so the manifest stays clean (js-yaml omits ``undefined``).
    const { rules: _dropped, ...rest } = routing;
    return patchSpec(m, {
      routing: Object.keys(rest).length > 0 ? rest : undefined,
    });
  }
  return patchSpec(m, { routing: { ...routing, rules } });
}

// Stream J.6 Path B — set / clear the VL fallback model. ``null`` (or an empty
// pick) removes the whole ``vision`` block so a text-only agent stays clean.
// ``fallbacks`` (advanced, multi-VL chain) is preserved if hand-added in YAML.
export function setVisionModel(
  m: unknown,
  model: ModelFields | null,
): AgentManifest {
  const keep =
    model !== null &&
    (model.provider !== undefined || model.name !== undefined);
  const existing = specOf(m).vision ?? {};
  if (!keep) {
    const { model: _dropped, ...rest } = existing;
    return patchSpec(m, {
      vision: Object.keys(rest).length > 0 ? rest : undefined,
    });
  }
  return patchSpec(m, { vision: { ...existing, model } });
}

export function setTool(
  m: unknown,
  kind: "webSearch" | "http" | "mcp",
  on: boolean,
): AgentManifest {
  const tools = specOf(m).tools ?? [];
  const without = (pred: (t: ToolEntry) => boolean): ToolEntry[] =>
    tools.filter((t) => !pred(t));
  if (kind === "webSearch") {
    const isWeb = (t: ToolEntry): boolean =>
      t.type === "builtin" && t.name === "web_search";
    return patchSpec(m, {
      tools: on
        ? [
            ...without(isWeb),
            { type: "builtin", name: "web_search", config: {} },
          ]
        : without(isWeb),
    });
  }
  if (kind === "http") {
    const isHttp = (t: ToolEntry): boolean => t.type === "http";
    return patchSpec(m, {
      tools: on ? [...without(isHttp), { type: "http" }] : without(isHttp),
    });
  }
  const isMcp = (t: ToolEntry): boolean => t.type === "mcp";
  return patchSpec(m, {
    tools: on
      ? [...without(isMcp), { type: "mcp", allow_tools: [] }]
      : without(isMcp),
  });
}

/** Whether a builtin tool by ``name`` is enabled (present in ``spec.tools``). */
export const hasBuiltinTool = (m: unknown, name: string): boolean =>
  (specOf(m).tools ?? []).some((t) => t.type === "builtin" && t.name === name);

/**
 * Toggle a builtin tool on/off by name. Unlike ``setTool``'s webSearch branch,
 * this NEVER rebuilds an already-present entry — so an existing entry's
 * ``config`` (or any of the sibling default-on essentials the form doesn't
 * show) survives a toggle untouched. ``on`` adds ``{type:"builtin", name}``
 * only when absent; ``off`` drops just that entry.
 */
export function setBuiltinTool(m: unknown, name: string, on: boolean): AgentManifest {
  const tools = specOf(m).tools ?? [];
  const has = tools.some((t) => t.type === "builtin" && t.name === name);
  if (on) {
    return has
      ? patchSpec(m, { tools })
      : patchSpec(m, { tools: [...tools, { type: "builtin", name }] });
  }
  return patchSpec(m, {
    tools: tools.filter((t) => !(t.type === "builtin" && t.name === name)),
  });
}

export function setMcpAllowTools(m: unknown, allow: string[]): AgentManifest {
  const tools = (specOf(m).tools ?? []).map((t) =>
    t.type === "mcp" ? { ...t, allow_tools: allow } : t,
  );
  return patchSpec(m, { tools });
}

// Selecting servers IS enabling MCP — there is no separate enable toggle. An
// empty selection drops the whole ``mcp`` tool entry (MCP off); a non-empty
// selection creates the entry on first pick. ``allow_tools`` is pruned to the
// selected servers' scope by the caller (the picker knows each server's tools).
export function setMcpServers(m: unknown, servers: string[]): AgentManifest {
  return setMcp(m, servers, readMcpAllowTools(m));
}

const readMcpAllowTools = (m: unknown): string[] =>
  (specOf(m).tools ?? []).find((t) => t.type === "mcp")?.allow_tools ?? [];

// Single writer for the whole ``mcp`` tool entry — both ``servers`` and
// ``allow_tools`` in one patch, so the picker can update them together without
// a stale-read double-patch. Empty ``servers`` ⇒ MCP off (entry dropped).
export function setMcp(
  m: unknown,
  servers: string[],
  allowTools: string[],
): AgentManifest {
  const withoutMcp = (specOf(m).tools ?? []).filter((t) => t.type !== "mcp");
  if (servers.length === 0) {
    return patchSpec(m, { tools: withoutMcp });
  }
  return patchSpec(m, {
    tools: [...withoutMcp, { type: "mcp", servers, allow_tools: allowTools }],
  });
}

// ---- approval gate (policies.approval_required_tools) ----
// Tool names that, when the agent dispatches them, pause the run for a human
// verdict (LangGraph interrupt). The governance counterweight to the always-on
// exec_python / bash base capability: the capability can't be removed, but it
// can be gated behind approval. Empty = no gate (drop the key + empty policies).
export const readApprovalTools = (m: unknown): string[] =>
  specOf(m).policies?.approval_required_tools ?? [];

export function setApprovalTools(m: unknown, tools: string[]): AgentManifest {
  const policies = specOf(m).policies ?? {};
  if (tools.length === 0) {
    const { approval_required_tools: _dropped, ...rest } = policies;
    return patchSpec(m, {
      policies: Object.keys(rest).length > 0 ? rest : undefined,
    });
  }
  return patchSpec(m, {
    policies: { ...policies, approval_required_tools: tools },
  });
}

// ---- other policy knobs (same ``policies`` block as the approval gate) ----
function patchPolicies(
  m: unknown,
  patch: Record<string, unknown>,
): AgentManifest {
  const policies = specOf(m).policies ?? {};
  return patchSpec(m, { policies: { ...policies, ...patch } });
}
export const readApprovalTimeout = (m: unknown): number =>
  specOf(m).policies?.approval_timeout_s ?? 86400;
export const setApprovalTimeout = (m: unknown, s: number): AgentManifest =>
  patchPolicies(m, { approval_timeout_s: s });
// B-20 — clarification-class ``ask_for_approval`` questions time out on their
// own (shorter) horizon; safety rows keep ``approval_timeout_s``.
export const readClarificationTimeout = (m: unknown): number =>
  specOf(m).policies?.clarification_timeout_s ?? 3600;
export const setClarificationTimeout = (m: unknown, s: number): AgentManifest =>
  patchPolicies(m, { clarification_timeout_s: s });

// ---- run budget (Group 6 试点: workflow.max_iterations + workflow.type
// + policies.max_no_progress + policies.run_deadline_s + top-level
// stream_deadline_s / idle_timeout_s) ----
// Aggregates six knobs that live in three different places in the manifest
// (workflow block / policies block / top-level spec) behind one curated
// "运行预算与超时" reader+writer pair. Readers return the raw stored value
// (``undefined`` when unset — the FieldRow widgets show the backend default
// themselves); ``patchRunBudget`` only touches the block(s) whose fields are
// present in ``patch`` so an untouched knob never materializes an empty
// parent block, and a field patched to ``undefined`` is removed (dropping
// the whole block if that empties it).
export interface RunBudgetFields {
  maxIterations?: number;
  // workflow.type (react/plan_execute/custom) — RAW reader, no default
  // substitution (the FieldRow widget shows the effective default itself).
  workflowType?: string;
  maxNoProgress?: number;
  runDeadlineS?: number;
  streamDeadlineS?: number;
  idleTimeoutS?: number;
  // B3 — per-run total token budget (policies.token_budget), 0/undefined = disabled.
  tokenBudget?: number;
}

export const readRunBudget = (m: unknown): RunBudgetFields => ({
  maxIterations: specOf(m).workflow?.max_iterations,
  workflowType: specOf(m).workflow?.type,
  maxNoProgress: specOf(m).policies?.max_no_progress,
  runDeadlineS: specOf(m).policies?.run_deadline_s,
  streamDeadlineS: specOf(m).stream_deadline_s,
  idleTimeoutS: specOf(m).idle_timeout_s,
  tokenBudget: specOf(m).policies?.token_budget,
});

// Merge a partial patch into a block, deleting keys whose patch value is
// ``undefined``; returns ``undefined`` (drop the block) when that empties it.
function mergeBlock(
  existing: Record<string, unknown> | undefined,
  patch: Record<string, unknown>,
): Record<string, unknown> | undefined {
  const merged: Record<string, unknown> = { ...(existing ?? {}) };
  for (const [k, v] of Object.entries(patch)) {
    if (v === undefined) delete merged[k];
    else merged[k] = v;
  }
  return Object.keys(merged).length > 0 ? merged : undefined;
}

export function patchRunBudget(
  m: unknown,
  patch: Partial<RunBudgetFields>,
): AgentManifest {
  const spec = specOf(m);
  const updates: Record<string, unknown> = {};

  const workflowPatch: Record<string, unknown> = {};
  if ("maxIterations" in patch) workflowPatch.max_iterations = patch.maxIterations;
  if ("workflowType" in patch) workflowPatch.type = patch.workflowType;
  if (Object.keys(workflowPatch).length > 0) {
    updates.workflow = mergeBlock(spec.workflow, workflowPatch);
  }

  const policiesPatch: Record<string, unknown> = {};
  if ("maxNoProgress" in patch) policiesPatch.max_no_progress = patch.maxNoProgress;
  if ("runDeadlineS" in patch) policiesPatch.run_deadline_s = patch.runDeadlineS;
  if ("tokenBudget" in patch) policiesPatch.token_budget = patch.tokenBudget;
  if (Object.keys(policiesPatch).length > 0) {
    updates.policies = mergeBlock(spec.policies ?? undefined, policiesPatch);
  }

  if ("streamDeadlineS" in patch) updates.stream_deadline_s = patch.streamDeadlineS;
  if ("idleTimeoutS" in patch) updates.idle_timeout_s = patch.idleTimeoutS;

  return patchSpec(m, updates);
}

// ---- context gates (policies.context_compression / working_memory /
// tool_result_prune / tool_output_budget) ----
// Curated "上下文与压缩" group over four independent PolicySpec sub-blocks
// (ContextCompressionPolicy / WorkingMemoryPolicy / ToolResultPrunePolicy /
// ToolOutputBudgetPolicy). Readers return the raw stored value (``undefined``
// when unset — the backend Pydantic defaults apply). ``patchContextGates``
// only touches the sub-block(s) whose fields are present in ``patch`` (never
// materializes an untouched sub-block or ``policies`` itself), deletes a key
// whose patch value is ``undefined``, and drops a sub-block that patching
// empties — mirrors ``patchRunBudget``.
export interface ContextGatesFields {
  ccEnabled?: boolean;
  ccThresholdPct?: number;
  ccHeadKeep?: number;
  ccTailKeep?: number;
  ccFlushBeforeCompaction?: boolean;
  ccMaxPasses?: number;
  ccMaxTurns?: number;
  ccMaxTokens?: number;
  ccPressureFeedback?: boolean;
  ccPressureWarnPct?: number;
  wmEnabled?: boolean;
  wmThresholdPct?: number;
  wmMaxRecentTurns?: number;
  wmKeepFirstTurn?: boolean;
  prEnabled?: boolean;
  prThresholdPct?: number;
  prRecentKept?: number;
  budgetEnabled?: boolean;
}

export const readContextGates = (m: unknown): ContextGatesFields => {
  const policies = specOf(m).policies ?? {};
  const cc = policies.context_compression ?? {};
  const wm = policies.working_memory ?? {};
  const pr = policies.tool_result_prune ?? {};
  const tb = policies.tool_output_budget ?? {};
  return {
    ccEnabled: cc.enabled,
    ccThresholdPct: cc.threshold_pct,
    ccHeadKeep: cc.head_keep,
    ccTailKeep: cc.tail_keep,
    ccFlushBeforeCompaction: cc.flush_before_compaction,
    ccMaxPasses: cc.max_passes,
    ccMaxTurns: cc.max_turns,
    ccMaxTokens: cc.max_tokens,
    ccPressureFeedback: cc.pressure_feedback,
    ccPressureWarnPct: cc.pressure_warn_pct,
    wmEnabled: wm.enabled,
    wmThresholdPct: wm.threshold_pct,
    wmMaxRecentTurns: wm.max_recent_turns,
    wmKeepFirstTurn: wm.keep_first_turn,
    prEnabled: pr.enabled,
    prThresholdPct: pr.threshold_pct,
    prRecentKept: pr.recent_tool_results_kept,
    budgetEnabled: tb.enabled,
  };
};

export function patchContextGates(
  m: unknown,
  patch: Partial<ContextGatesFields>,
): AgentManifest {
  const policies = specOf(m).policies ?? {};
  const updates: Record<string, unknown> = { ...policies };

  // Merge a patched sub-block into ``updates``, deleting the key entirely
  // (not setting it to ``undefined``) when it empties — so the subsequent
  // ``Object.keys(updates).length`` check below sees an accurate count and
  // an untouched sub-block is never materialized.
  const mergeSubBlock = (
    key: "context_compression" | "working_memory" | "tool_result_prune" | "tool_output_budget",
    subPatch: Record<string, unknown>,
  ): void => {
    if (Object.keys(subPatch).length === 0) return;
    const merged = mergeBlock(
      policies[key] as Record<string, unknown> | undefined,
      subPatch,
    );
    if (merged === undefined) delete updates[key];
    else updates[key] = merged;
  };

  const ccPatch: Record<string, unknown> = {};
  if ("ccEnabled" in patch) ccPatch.enabled = patch.ccEnabled;
  if ("ccThresholdPct" in patch) ccPatch.threshold_pct = patch.ccThresholdPct;
  if ("ccHeadKeep" in patch) ccPatch.head_keep = patch.ccHeadKeep;
  if ("ccTailKeep" in patch) ccPatch.tail_keep = patch.ccTailKeep;
  if ("ccFlushBeforeCompaction" in patch)
    ccPatch.flush_before_compaction = patch.ccFlushBeforeCompaction;
  if ("ccMaxPasses" in patch) ccPatch.max_passes = patch.ccMaxPasses;
  if ("ccMaxTurns" in patch) ccPatch.max_turns = patch.ccMaxTurns;
  if ("ccMaxTokens" in patch) ccPatch.max_tokens = patch.ccMaxTokens;
  if ("ccPressureFeedback" in patch)
    ccPatch.pressure_feedback = patch.ccPressureFeedback;
  if ("ccPressureWarnPct" in patch)
    ccPatch.pressure_warn_pct = patch.ccPressureWarnPct;
  mergeSubBlock("context_compression", ccPatch);

  const wmPatch: Record<string, unknown> = {};
  if ("wmEnabled" in patch) wmPatch.enabled = patch.wmEnabled;
  if ("wmThresholdPct" in patch) wmPatch.threshold_pct = patch.wmThresholdPct;
  if ("wmMaxRecentTurns" in patch)
    wmPatch.max_recent_turns = patch.wmMaxRecentTurns;
  if ("wmKeepFirstTurn" in patch) wmPatch.keep_first_turn = patch.wmKeepFirstTurn;
  mergeSubBlock("working_memory", wmPatch);

  const prPatch: Record<string, unknown> = {};
  if ("prEnabled" in patch) prPatch.enabled = patch.prEnabled;
  if ("prThresholdPct" in patch) prPatch.threshold_pct = patch.prThresholdPct;
  if ("prRecentKept" in patch)
    prPatch.recent_tool_results_kept = patch.prRecentKept;
  mergeSubBlock("tool_result_prune", prPatch);

  const budgetPatch: Record<string, unknown> = {};
  if ("budgetEnabled" in patch) budgetPatch.enabled = patch.budgetEnabled;
  mergeSubBlock("tool_output_budget", budgetPatch);

  return patchSpec(m, {
    policies: Object.keys(updates).length > 0 ? updates : undefined,
  });
}

// ---- dynamic workers (spawn_worker) ----
// Whether the agent's LLM may spawn ephemeral workers at run time. The block is
// absent by default and that means ENABLED (the platform switch governs the
// ceiling). The form surfaces this so the autonomous-worker behaviour is
// visible + can be opted out per agent: ``off`` writes ``{enabled:false}``;
// ``on`` drops the block (back to the default-on state, keeping YAML clean).
export const readDynamicWorkersOn = (m: unknown): boolean =>
  (specOf(m).dynamic_workers?.enabled ?? true) !== false;

export function setDynamicWorkersOn(m: unknown, on: boolean): AgentManifest {
  if (on) {
    const dw = specOf(m).dynamic_workers ?? {};
    const { enabled: _dropped, ...rest } = dw;
    return patchSpec(m, {
      dynamic_workers: Object.keys(rest).length > 0 ? rest : undefined,
    });
  }
  return patchSpec(m, {
    dynamic_workers: { ...(specOf(m).dynamic_workers ?? {}), enabled: false },
  });
}

// ---- structured output field editor (config-page redesign v2 Task 7) ----
// A flat "field list" visualization over ``spec.output_schema.json_schema``
// (Stream RT-1 / RT-ADR-4's structured-final-reply JSON Schema — see the
// block's own doc comment on ``AgentManifest.spec.output_schema`` above).
// Only a FLAT object schema — top-level scalar properties, or a homogeneous
// array of scalars — is representable in the curated form; anything richer
// (a nested object property, ``$ref``, ``oneOf``, an extra top-level key, …)
// reads as ``"unrepresentable"`` so the widget can fall back to a read-only
// notice and defer to the YAML view rather than silently mangle it.
// ``name``/``strict`` never surface in the curated form — ``setOutputSchemaRows``
// preserves them (and any other unknown sibling key already on the block)
// untouched, only ever replacing the ``json_schema`` key itself.
export type SchemaFieldType =
  | "string"
  | "number"
  | "integer"
  | "boolean"
  | "array_string"
  | "array_number";

export interface SchemaFieldRow {
  name: string;
  type: SchemaFieldType;
  required: boolean;
  description: string;
}

const OUTPUT_SCHEMA_TOP_KEYS = new Set([
  "type",
  "properties",
  "required",
  "additionalProperties",
]);
const OUTPUT_SCHEMA_PROPERTY_KEYS = new Set(["type", "description", "items"]);
const OUTPUT_SCHEMA_SCALAR_TYPES = new Set(["string", "number", "integer", "boolean"]);
const OUTPUT_SCHEMA_ARRAY_ITEM_TYPES = new Set(["string", "number"]);

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

// A single property's JSON-Schema fragment → its ``SchemaFieldType``, or
// ``null`` when it doesn't fit one of the six representable shapes (the
// caller then declares the WHOLE schema "unrepresentable"). Strict about
// ``items``: only present (and only ``{type}``-shaped) when the property
// itself is an array — a stray ``items`` key on a scalar property is
// rejected rather than silently discarded, so a hand-authored schema never
// loses data across a read→edit→write round trip.
function propertyFieldType(prop: unknown): SchemaFieldType | null {
  if (!isPlainObject(prop)) return null;
  if (!Object.keys(prop).every((k) => OUTPUT_SCHEMA_PROPERTY_KEYS.has(k))) return null;
  if ("description" in prop && typeof prop.description !== "string") return null;
  const { type } = prop;
  if (typeof type === "string" && OUTPUT_SCHEMA_SCALAR_TYPES.has(type)) {
    return "items" in prop ? null : (type as SchemaFieldType);
  }
  if (type === "array") {
    const items = prop.items;
    if (!isPlainObject(items)) return null;
    if (Object.keys(items).length !== 1 || typeof items.type !== "string") return null;
    if (!OUTPUT_SCHEMA_ARRAY_ITEM_TYPES.has(items.type)) return null;
    return items.type === "string" ? "array_string" : "array_number";
  }
  return null;
}

// ``undefined`` = ``spec.output_schema`` absent/null (not configured);
// ``"unrepresentable"`` = configured but not flat (read-only in the form);
// otherwise the flat field rows.
export function readOutputSchemaRows(
  m: unknown,
): SchemaFieldRow[] | "unrepresentable" | undefined {
  const block = specOf(m).output_schema;
  if (block == null) return undefined;
  const schema = block.json_schema;
  if (!isPlainObject(schema)) return "unrepresentable";
  if (!Object.keys(schema).every((k) => OUTPUT_SCHEMA_TOP_KEYS.has(k))) {
    return "unrepresentable";
  }
  if ("type" in schema && schema.type !== "object") return "unrepresentable";

  const propertiesRaw = "properties" in schema ? schema.properties : {};
  if (!isPlainObject(propertiesRaw)) return "unrepresentable";
  const properties = propertiesRaw;

  const requiredRaw = "required" in schema ? schema.required : [];
  if (
    !Array.isArray(requiredRaw) ||
    !requiredRaw.every((r) => typeof r === "string")
  ) {
    return "unrepresentable";
  }
  const required = requiredRaw as string[];
  const names = Object.keys(properties);
  if (!required.every((r) => names.includes(r))) return "unrepresentable";

  const rows: SchemaFieldRow[] = [];
  for (const name of names) {
    const type = propertyFieldType(properties[name]);
    if (type === null) return "unrepresentable";
    const prop = properties[name] as Record<string, unknown>;
    rows.push({
      name,
      type,
      required: required.includes(name),
      description: typeof prop.description === "string" ? prop.description : "",
    });
  }
  return rows;
}

// A field row → its JSON-Schema property fragment (the inverse of
// ``propertyFieldType``).
function rowToProperty(row: SchemaFieldRow): Record<string, unknown> {
  const base: Record<string, unknown> =
    row.type === "array_string"
      ? { type: "array", items: { type: "string" } }
      : row.type === "array_number"
        ? { type: "array", items: { type: "number" } }
        : { type: row.type };
  return row.description ? { ...base, description: row.description } : base;
}

// Writes the flat rows back as ``spec.output_schema.json_schema``, preserving
// any existing ``name``/``strict``/unknown sibling key on the block
// untouched. ``rows === null`` deletes the WHOLE ``output_schema`` block (the
// form's off-switch — js-yaml omits the ``undefined``-valued key). An empty
// ``rows`` array still writes a (non-empty, backend-legal) ``json_schema``
// dict — ``{type:"object", properties:{}, additionalProperties:false}`` —
// rather than a bare ``{}`` the backend would reject.
export function setOutputSchemaRows(
  m: unknown,
  rows: SchemaFieldRow[] | null,
): AgentManifest {
  if (rows === null) return patchSpec(m, { output_schema: undefined });

  let json_schema: Record<string, unknown>;
  if (rows.length === 0) {
    json_schema = { type: "object", properties: {}, additionalProperties: false };
  } else {
    const properties: Record<string, unknown> = {};
    const required: string[] = [];
    for (const row of rows) {
      properties[row.name] = rowToProperty(row);
      if (row.required) required.push(row.name);
    }
    json_schema = { type: "object", properties, required, additionalProperties: false };
  }

  const existing = specOf(m).output_schema;
  const base = isPlainObject(existing) ? existing : {};
  return patchSpec(m, { output_schema: { ...base, json_schema } });
}

// ---- evolved-skill auto-attach (SE-16 SE-A42) ----
// Whether build auto-attaches this agent's own ACTIVE distilled skills
// (evolution flywheel output). Plain opt-in bool: absent = off, so ``off``
// drops the key to keep the YAML clean.
export const readAutoAttachEvolvedSkills = (m: unknown): boolean =>
  specOf(m).auto_attach_evolved_skills === true;

export function setAutoAttachEvolvedSkills(m: unknown, on: boolean): AgentManifest {
  return patchSpec(m, { auto_attach_evolved_skills: on ? true : undefined });
}

// ---- knowledge (RAG knowledge_base_refs) ----
// Tenant knowledge bases the agent may search. Empty = drop the block (no
// knowledge access) so a non-RAG agent's manifest stays clean.
export const readKnowledgeRefs = (m: unknown): string[] =>
  specOf(m).knowledge?.knowledge_base_refs ?? [];

export function setKnowledgeRefs(m: unknown, refs: string[]): AgentManifest {
  const knowledge = specOf(m).knowledge ?? {};
  if (refs.length === 0) {
    const { knowledge_base_refs: _dropped, ...rest } = knowledge;
    return patchSpec(m, {
      knowledge: Object.keys(rest).length > 0 ? rest : undefined,
    });
  }
  return patchSpec(m, {
    knowledge: { ...knowledge, knowledge_base_refs: refs },
  });
}

// ---- skills (attached skill refs) ----
// Skill names the agent loads. Empty = drop the key.
export const readSkills = (m: unknown): string[] => specOf(m).skills ?? [];

export function setSkills(m: unknown, skills: string[]): AgentManifest {
  return patchSpec(m, { skills: skills.length > 0 ? skills : undefined });
}

// ---- subagents (static delegation) ----
// Named delegation targets referencing deployed agents. Rows are stored
// verbatim (an in-progress row may be partial — validation happens on save);
// empty = drop the block.
export const readSubagents = (m: unknown): SubAgentFields[] =>
  specOf(m).subagents ?? [];

export function setSubagents(
  m: unknown,
  rows: SubAgentFields[],
): AgentManifest {
  return patchSpec(m, { subagents: rows.length > 0 ? rows : undefined });
}

// ---- defenses (DefenseSpec switches — the "defenses" form section) ----
// Readers return the EFFECTIVE value: an absent key reads as its DefenseSpec
// default so the control shows what the backend would actually apply. Setters
// omit a key whose value equals the default (keeping the YAML minimal) and clear
// the ``_on_error`` sub-knob when its parent switch is turned off.
export const readPromptInjection = (m: unknown): "spotlight" | "off" =>
  (specOf(m).defenses?.prompt_injection as "spotlight" | "off") ?? "spotlight";
export const readOutputScreen = (m: unknown): "block" | "off" =>
  (specOf(m).defenses?.output_screen as "block" | "off") ?? "block";
export const readOutputJudge = (m: unknown): "block" | "off" =>
  (specOf(m).defenses?.output_judge as "block" | "off") ?? "off";
export const readOutputJudgeOnError = (m: unknown): "open" | "closed" =>
  (specOf(m).defenses?.output_judge_on_error as "open" | "closed") ?? "open";
export const readActionScreen = (m: unknown): "off" | "block" | "approval" =>
  (specOf(m).defenses?.action_screen as "off" | "block" | "approval") ?? "off";
export const readActionScreenOnError = (m: unknown): "open" | "closed" =>
  (specOf(m).defenses?.action_screen_on_error as "open" | "closed") ?? "open";
export const readOutputDlp = (m: unknown): "redact" | "off" =>
  (specOf(m).defenses?.output_dlp as "redact" | "off") ?? "off";
export const readExtends = (m: unknown): string | undefined =>
  specOf(m).extends ?? undefined;

export const setPromptInjection = (
  m: unknown,
  v: "spotlight" | "off",
): AgentManifest =>
  patchDefenses(m, { prompt_injection: v === "spotlight" ? undefined : v });

export const setOutputScreen = (m: unknown, v: "block" | "off"): AgentManifest =>
  patchDefenses(m, { output_screen: v === "block" ? undefined : v });

export function setOutputJudge(m: unknown, v: "block" | "off"): AgentManifest {
  if (v === "off") {
    return patchDefenses(m, {
      output_judge: undefined,
      output_judge_on_error: undefined,
    });
  }
  return patchDefenses(m, { output_judge: v });
}

export const setOutputJudgeOnError = (
  m: unknown,
  v: "open" | "closed",
): AgentManifest =>
  patchDefenses(m, { output_judge_on_error: v === "open" ? undefined : v });

export function setActionScreen(
  m: unknown,
  v: "off" | "block" | "approval",
): AgentManifest {
  if (v === "off") {
    return patchDefenses(m, {
      action_screen: undefined,
      action_screen_on_error: undefined,
    });
  }
  return patchDefenses(m, { action_screen: v });
}

export const setActionScreenOnError = (
  m: unknown,
  v: "open" | "closed",
): AgentManifest =>
  patchDefenses(m, { action_screen_on_error: v === "open" ? undefined : v });

export const setOutputDlp = (m: unknown, v: "redact" | "off"): AgentManifest =>
  patchDefenses(m, { output_dlp: v === "off" ? undefined : v });

// ---- security group (spec.sandbox.network egress + policies.tool_use_enforcement) ----
// Curated "安全" group over two independent locations: the sandbox egress
// policy (NetworkSpec, nested two levels under spec.sandbox.network) and the
// tool-use-enforcement knob (a scalar sibling of the other ``policies``
// fields). Readers return the raw stored value (``undefined`` when unset —
// the backend Pydantic defaults apply). ``patchSecurity`` only touches the
// block(s)/key(s) whose fields are present in ``patch`` (never materializes
// an untouched block) and deletes a key whose patch value is ``undefined``.
// Unlike the optional ``policies`` sub-blocks, ``sandbox.network`` is a
// REQUIRED pydantic field (no default) — an emptied block is kept as
// ``network: {}`` (valid: every NetworkSpec field is defaulted) instead of
// dropped, which would 422 the next deploy. ``sandbox``'s unrelated keys
// (runtime/image/resources/filesystem) survive untouched; a fully absent
// sandbox is still never materialized just to hold an empty network. Arrays
// are copied on write so the stored manifest never aliases the caller's array.
export interface SecurityFields {
  egress?: string;
  allowlist?: string[];
  denylist?: string[];
  toolUseEnforcement?: string;
}

export const readSecurity = (m: unknown): SecurityFields => {
  const network = specOf(m).sandbox?.network ?? {};
  return {
    egress: network.egress,
    allowlist: network.allowlist,
    denylist: network.denylist,
    toolUseEnforcement: specOf(m).policies?.tool_use_enforcement,
  };
};

export function patchSecurity(
  m: unknown,
  patch: Partial<SecurityFields>,
): AgentManifest {
  const spec = specOf(m);
  const updates: Record<string, unknown> = {};

  const networkPatch: Record<string, unknown> = {};
  if ("egress" in patch) networkPatch.egress = patch.egress;
  if ("allowlist" in patch) {
    networkPatch.allowlist =
      patch.allowlist === undefined ? undefined : [...patch.allowlist];
  }
  if ("denylist" in patch) {
    networkPatch.denylist =
      patch.denylist === undefined ? undefined : [...patch.denylist];
  }
  if (Object.keys(networkPatch).length > 0) {
    const sandbox = spec.sandbox;
    const mergedNetwork =
      mergeBlock(
        sandbox?.network as Record<string, unknown> | undefined,
        networkPatch,
      ) ?? {};
    if (sandbox !== undefined || Object.keys(mergedNetwork).length > 0) {
      updates.sandbox = { ...(sandbox ?? {}), network: mergedNetwork };
    }
  }

  if ("toolUseEnforcement" in patch) {
    updates.policies = mergeBlock(spec.policies ?? undefined, {
      tool_use_enforcement: patch.toolUseEnforcement,
    });
  }

  return patchSpec(m, updates);
}

// ---- sandbox filesystem group (spec.sandbox.filesystem.persistent_workspace) ----
// Mirrors patchSecurity's network handling: SandboxSpec.filesystem, like
// network, is a REQUIRED pydantic field with no default — an emptied block is
// kept as ``filesystem: {}`` (valid: every field is defaulted) instead of
// dropped, which would 422 the next deploy (hotfix #1017). Reader returns the
// raw stored value (``undefined`` when unset — the display layer supplies the
// effective default, false). ``sandbox``'s unrelated keys (runtime/resources/
// network etc.) and ``filesystem``'s own unrelated keys survive untouched. A
// fully absent sandbox is never materialized just to hold an empty filesystem.
export interface SandboxFsFields {
  persistentWorkspace?: boolean;
}

export const readSandboxFs = (m: unknown): SandboxFsFields => ({
  persistentWorkspace: specOf(m).sandbox?.filesystem?.persistent_workspace,
});

export function patchSandboxFs(
  m: unknown,
  patch: Partial<SandboxFsFields>,
): AgentManifest {
  const spec = specOf(m);
  const updates: Record<string, unknown> = {};

  const filesystemPatch: Record<string, unknown> = {};
  if ("persistentWorkspace" in patch) {
    filesystemPatch.persistent_workspace = patch.persistentWorkspace;
  }
  if (Object.keys(filesystemPatch).length > 0) {
    const sandbox = spec.sandbox;
    const mergedFilesystem =
      mergeBlock(
        sandbox?.filesystem as Record<string, unknown> | undefined,
        filesystemPatch,
      ) ?? {};
    if (sandbox !== undefined || Object.keys(mergedFilesystem).length > 0) {
      updates.sandbox = { ...(sandbox ?? {}), filesystem: mergedFilesystem };
    }
  }

  return patchSpec(m, updates);
}

// ---- memory injection budgets (spec.memory.long_term token budgets) ----
// Stream RT-2 PR-2 (RT-ADR-10) — curated "记忆注入预算" pair over
// LongTermMemorySpec.injection_token_budget / correction_token_budget.
// Reader returns the raw stored values (``undefined`` when unset — the
// backend Pydantic defaults, 2000 / 500, apply). Unlike the optional
// ``policies`` sub-blocks, ``memory.long_term``'s PRESENCE is the memory
// on/off switch at runtime (``{}`` = on with defaults, absent/``null`` =
// off — see ``readMemoryOn``/``setMemoryOn``), so an emptied ``long_term``
// must survive as ``{}`` instead of being dropped (mirrors ``patchSecurity``
// / ``patchSandboxFs``'s REQUIRED-block handling, not PR2's optional-block
// drop-empty). ``patchMemoryBudgets`` only touches ``long_term`` when the
// patch carries a budget key, never materializes an absent ``memory`` block
// for a patch that nets out empty, and preserves every other ``long_term``
// key (``retrieve_top_k`` etc.) and ``memory``'s own unknown keys untouched.
// Leaves the pre-existing ``readMemoryOn``/``patchLongTerm`` untouched.
export interface MemoryBudgetFields {
  injectionTokenBudget?: number;
  correctionTokenBudget?: number;
}

export const readMemoryBudgets = (m: unknown): MemoryBudgetFields => ({
  injectionTokenBudget: specOf(m).memory?.long_term?.injection_token_budget,
  correctionTokenBudget: specOf(m).memory?.long_term?.correction_token_budget,
});

export function patchMemoryBudgets(
  m: unknown,
  patch: Partial<MemoryBudgetFields>,
): AgentManifest {
  const memory = specOf(m).memory;
  const updates: Record<string, unknown> = {};

  const longTermPatch: Record<string, unknown> = {};
  if ("injectionTokenBudget" in patch) {
    longTermPatch.injection_token_budget = patch.injectionTokenBudget;
  }
  if ("correctionTokenBudget" in patch) {
    longTermPatch.correction_token_budget = patch.correctionTokenBudget;
  }
  if (Object.keys(longTermPatch).length > 0) {
    const existingLongTerm = (memory?.long_term ?? undefined) as
      | Record<string, unknown>
      | undefined;
    const mergedLongTerm = mergeBlock(existingLongTerm, longTermPatch) ?? {};
    if (memory !== undefined || Object.keys(mergedLongTerm).length > 0) {
      updates.memory = { ...(memory ?? {}), long_term: mergedLongTerm };
    }
  }

  return patchSpec(m, updates);
}

// ---- memory consolidation (policies.memory_consolidation.enabled) ----
// Capability Uplift Sprint #7 (Mini-ADR U-39) — per-agent MemoryConsolidator
// master switch (MemoryConsolidationPolicy.enabled). Unlike ``long_term``,
// ``policies.memory_consolidation`` has a backend ``default_factory`` (an
// absent block ⇒ ``enabled: True`` + platform-default ``aux_model``), so it
// follows the standard optional-block ``mergeBlock`` idiom — mirrors PR2's
// ``patchContextGates`` policies sub-block handling: an emptied
// ``memory_consolidation`` (and ``policies``, if that empties too) is
// dropped rather than kept as ``{}``. The block's own unknown keys
// (``aux_model``) survive untouched when patching ``enabled``.
export interface ConsolidationFields {
  consolidationEnabled?: boolean;
}

export const readConsolidation = (m: unknown): ConsolidationFields => ({
  consolidationEnabled: specOf(m).policies?.memory_consolidation?.enabled,
});

export function patchConsolidation(
  m: unknown,
  patch: Partial<ConsolidationFields>,
): AgentManifest {
  const policies = specOf(m).policies ?? {};
  const updates: Record<string, unknown> = { ...policies };

  if ("consolidationEnabled" in patch) {
    const merged = mergeBlock(
      policies.memory_consolidation as Record<string, unknown> | undefined,
      { enabled: patch.consolidationEnabled },
    );
    if (merged === undefined) delete updates.memory_consolidation;
    else updates.memory_consolidation = merged;
  }

  return patchSpec(m, {
    policies: Object.keys(updates).length > 0 ? updates : undefined,
  });
}

// ---- reflection (spec.reflection — presence-semantic block) ----
// Stream J.11 — whether the runtime reflect node runs. Unlike the optional
// ``policies`` sub-blocks (absent ⇒ backend default applies), ``reflection``'s
// PRESENCE IS the on/off switch: ``{}`` = on with defaults (budget 2 /
// deadline_s 30), absent OR explicit ``null`` = off — mirrors
// ``long_term``'s on/off semantics (``readMemoryOn``/``setMemoryOn``), but
// ``reflection`` is a TOP-LEVEL spec key (one level, not nested two levels
// like ``memory.long_term``).
export const readReflectionOn = (m: unknown): boolean =>
  (specOf(m).reflection ?? null) !== null;

// ``on``: an already-present block (incl. explicit ``null``, via ``?? {}``)
// is preserved unchanged / activated with defaults. ``off``: the whole
// ``reflection`` key is deleted via explicit ``undefined`` (js-yaml omits
// it) — existence IS this switch's semantics, so deleting is how you turn it
// off (there is no separate ``enabled`` flag to flip).
export function setReflectionOn(m: unknown, on: boolean): AgentManifest {
  if (!on) return patchSpec(m, { reflection: undefined });
  const existing = specOf(m).reflection;
  return patchSpec(m, { reflection: existing ?? {} });
}

export interface ReflectionTuningFields {
  budget?: number;
  deadlineS?: number;
}

// RAW reader — no default substitution; an unset field reads as ``undefined``
// (the caller/backend supplies the effective default, 2 / 30s).
export const readReflectionTuning = (m: unknown): ReflectionTuningFields => ({
  budget: specOf(m).reflection?.budget,
  deadlineS: specOf(m).reflection?.deadline_s,
});

// Merge a partial patch into ``spec.reflection`` preserving unknown sibling
// keys — mirrors ``patchMemoryBudgets``'s presence-block idiom, but simpler:
// reflection is a single top-level block (no two-level nesting). ``"key" in
// patch`` is a presence test (a key patched to ``undefined`` deletes it); an
// emptied-but-already-present block survives as ``{}`` (``mergeBlock ?? {}``)
// because presence — not content — is the on/off switch, so clearing the
// last tuning key must never silently turn reflection off. When reflection
// is absent OR explicit ``null`` and the patch nets out empty, no update is
// made (never materializes the block) — the UI render-guard keeps tuning
// controls hidden unless reflection is already ON, so this path shouldn't
// occur in practice. If it did (explicit-null reflection + a real-valued
// patch), behavior is still well-defined: it materializes ``{...patch}``
// (mirrors what ``patchMemoryBudgets`` does for an absent parent block).
export function patchReflectionTuning(
  m: unknown,
  patch: Partial<ReflectionTuningFields>,
): AgentManifest {
  const reflectionPatch: Record<string, unknown> = {};
  if ("budget" in patch) reflectionPatch.budget = patch.budget;
  if ("deadlineS" in patch) reflectionPatch.deadline_s = patch.deadlineS;
  if (Object.keys(reflectionPatch).length === 0) return patchSpec(m, {});

  const existing = specOf(m).reflection;
  const merged = mergeBlock(existing ?? undefined, reflectionPatch) ?? {};
  if (existing != null || Object.keys(merged).length > 0) {
    return patchSpec(m, { reflection: merged });
  }
  return patchSpec(m, {});
}

// ---- observability group (spec.cache.enabled + policies.trajectory_recording
// — config-page redesign v2, Task 8b erratum) ----
// Curated "触发器与可观测" group over two independent locations: the LLM
// response cache opt-out (CacheSpec, ``spec.cache`` — Stream K.K4 / Mini-ADR
// K-3), and trajectory recording (``policies.trajectory_recording`` — Stream
// L.L7: whether a finished run's full transcript gets serialized to object
// storage for quality evaluation and future fine-tuning; a privacy-sensitive
// agent should set this ``False`` to keep conversation content out of
// non-WORM storage). Task 4 removed trajectory recording's curated toggle as
// dead-opt-in; Task 8 found the field is actually read at run time
// (``agent_spec.py``'s ``PolicySpec.trajectory_recording`` /
// ``agent_factory.py``'s ``trajectory_enabled``), and this task (8b) restores
// the toggle here.
//
// NOTE the naming trap: ``ModelFields.cache_enabled`` (``model.cache_enabled``)
// is a DIFFERENT, pre-existing field — Anthropic prompt caching. This block's
// ``responseCacheEnabled`` is the LLM RESPONSE cache (``spec.cache.enabled``);
// the two live at different paths, are independently toggled, and must never
// be confused. Both ``spec.cache`` and ``policies.trajectory_recording`` have
// a backend ``default_factory``/default (an absent block ⇒ ``enabled: True``;
// an absent key ⇒ ``trajectory_recording: True``), so both fields follow the
// standard optional-block ``mergeBlock`` idiom: absent ≡ defaults, and an
// emptied block is dropped rather than kept as ``{}`` (dropping is harmless
// here — unlike the REQUIRED ``sandbox.network`` / ``sandbox.filesystem``
// blocks, or the presence-semantic ``memory.long_term`` / ``reflection``
// blocks). Readers are RAW — no default substitution. ``patchObservability``
// only touches the block(s) whose fields are present in ``patch``, and
// deletes a key whose patch value is ``undefined``.
export interface ObservabilityFields {
  responseCacheEnabled?: boolean;
  trajectoryRecording?: boolean;
}

export const readObservability = (m: unknown): ObservabilityFields => ({
  responseCacheEnabled: specOf(m).cache?.enabled,
  trajectoryRecording: specOf(m).policies?.trajectory_recording,
});

export function patchObservability(
  m: unknown,
  patch: Partial<ObservabilityFields>,
): AgentManifest {
  const spec = specOf(m);
  const updates: Record<string, unknown> = {};

  if ("responseCacheEnabled" in patch) {
    updates.cache = mergeBlock(
      spec.cache as Record<string, unknown> | undefined,
      { enabled: patch.responseCacheEnabled },
    );
  }

  if ("trajectoryRecording" in patch) {
    updates.policies = mergeBlock(spec.policies ?? undefined, {
      trajectory_recording: patch.trajectoryRecording,
    });
  }

  return patchSpec(m, updates);
}

// ---- run profile presets (config-page redesign v2 Task 6, cont'd) ----
// ``applyRunProfile`` writes all 18 managed fields via their EXISTING
// read/patch pairs (never touching the manifest directly) — the field-level
// "value === backend default → omit" convention already lives in
// ``patchLongTerm``/``patchMemoryBudgets``/``patchConsolidation``/
// ``patchRunBudget``/``patchContextGates``/``setDynamicWorkersOn``; this
// function only decides, per field, whether to pass the preset's value or
// ``undefined`` (by comparing against ``PROFILE_BACKEND_DEFAULTS``).
//
// Memory-off gating: presets tune knobs, they NEVER flip feature switches
// (spec §③ — the long-term-memory switch is unmanaged). ``long_term``'s
// PRESENCE is that switch (``readMemoryOn``), and ``patchLongTerm``
// materializes the block on first touch — so while memory is off, the 7
// long_term-backed fields are skipped entirely (not patched, not compared):
// ``applyRunProfile`` leaves ``memory`` untouched, and ``inferRunProfile``/
// ``countProfileDiff`` match on the remaining 11 applicable fields only.
export function applyRunProfile(m: unknown, profile: RunProfile): AgentManifest {
  const target = RUN_PROFILES[profile];
  // undefined ⇒ "leave/return this field to the backend default" (deletes
  // the key via the callee's own patch-undefined convention); otherwise the
  // preset's explicit value — even when it happens to equal the CURRENT
  // manifest value, so a re-apply is idempotent.
  const orDefault = <K extends keyof RunProfileValues>(
    key: K,
  ): RunProfileValues[K] | undefined =>
    target[key] === PROFILE_BACKEND_DEFAULTS[key] ? undefined : target[key];

  // long_term knobs — bypasses the individual setTopK/setVerifyReads/
  // setRewriteReads/setRecallMode/setAbstainThreshold setters (each accepts
  // only a definite value, not "delete this key") and instead patches
  // ``memory.long_term`` directly via the same private helper they use, so
  // a preset-default field is genuinely absent rather than merely holding
  // its default value in the manifest (the balanced-profile linchpin
  // invariant — see ``form_model_profiles.test.ts``).
  let next = asObj(m);
  if (readMemoryOn(m)) {
    next = patchLongTerm(next, {
      retrieve_top_k: orDefault("topK"),
      verify_reads: orDefault("verifyReads"),
      rewrite_reads: orDefault("rewriteReads"),
      recall_mode: orDefault("recallMode"),
      abstain_threshold: orDefault("abstainThreshold"),
    });
    next = patchMemoryBudgets(next, {
      injectionTokenBudget: orDefault("injectionTokenBudget"),
      correctionTokenBudget: orDefault("correctionTokenBudget"),
    });
  }
  next = patchConsolidation(next, {
    consolidationEnabled: orDefault("consolidationEnabled"),
  });
  next = patchRunBudget(next, {
    maxIterations: orDefault("maxIterations"),
    maxNoProgress: orDefault("maxNoProgress"),
  });
  next = patchContextGates(next, {
    prThresholdPct: orDefault("prThresholdPct"),
    prRecentKept: orDefault("prRecentKept"),
    wmThresholdPct: orDefault("wmThresholdPct"),
    wmMaxRecentTurns: orDefault("wmMaxRecentTurns"),
    ccThresholdPct: orDefault("ccThresholdPct"),
    ccHeadKeep: orDefault("ccHeadKeep"),
    ccTailKeep: orDefault("ccTailKeep"),
  });
  // setDynamicWorkersOn already implements "true (its backend default) →
  // drop the key, false → write it explicitly" internally, so it's called
  // directly with the preset's own value (no ``orDefault`` wrapper needed).
  next = setDynamicWorkersOn(next, target.dynamicWorkersOn);
  return next;
}

// Reads all 18 managed fields at their CURRENT effective value (stored value
// if present, else the backend default) — the comparison basis for both
// ``inferRunProfile`` and ``countProfileDiff``.
function effectiveProfileValues(m: unknown): RunProfileValues {
  const budgets = readMemoryBudgets(m);
  const consolidation = readConsolidation(m);
  const runBudget = readRunBudget(m);
  const gates = readContextGates(m);
  return {
    topK: readTopK(m) ?? PROFILE_BACKEND_DEFAULTS.topK,
    verifyReads: readVerifyReads(m),
    rewriteReads: readRewriteReads(m),
    recallMode: readRecallMode(m),
    abstainThreshold: readAbstainThreshold(m),
    injectionTokenBudget:
      budgets.injectionTokenBudget ?? PROFILE_BACKEND_DEFAULTS.injectionTokenBudget,
    correctionTokenBudget:
      budgets.correctionTokenBudget ?? PROFILE_BACKEND_DEFAULTS.correctionTokenBudget,
    consolidationEnabled:
      consolidation.consolidationEnabled ?? PROFILE_BACKEND_DEFAULTS.consolidationEnabled,
    maxIterations: runBudget.maxIterations ?? PROFILE_BACKEND_DEFAULTS.maxIterations,
    maxNoProgress: runBudget.maxNoProgress ?? PROFILE_BACKEND_DEFAULTS.maxNoProgress,
    prThresholdPct: gates.prThresholdPct ?? PROFILE_BACKEND_DEFAULTS.prThresholdPct,
    prRecentKept: gates.prRecentKept ?? PROFILE_BACKEND_DEFAULTS.prRecentKept,
    wmThresholdPct: gates.wmThresholdPct ?? PROFILE_BACKEND_DEFAULTS.wmThresholdPct,
    wmMaxRecentTurns:
      gates.wmMaxRecentTurns ?? PROFILE_BACKEND_DEFAULTS.wmMaxRecentTurns,
    ccThresholdPct: gates.ccThresholdPct ?? PROFILE_BACKEND_DEFAULTS.ccThresholdPct,
    ccHeadKeep: gates.ccHeadKeep ?? PROFILE_BACKEND_DEFAULTS.ccHeadKeep,
    ccTailKeep: gates.ccTailKeep ?? PROFILE_BACKEND_DEFAULTS.ccTailKeep,
    dynamicWorkersOn: readDynamicWorkersOn(m),
  };
}

const PROFILE_FIELD_KEYS = Object.keys(
  PROFILE_BACKEND_DEFAULTS,
) as (keyof RunProfileValues)[];

// The 7 long_term-backed fields ``applyRunProfile`` skips while memory is
// off (see its memory-off gating note) — infer/count must skip the same set
// or an applied preset would immediately read back as "custom".
const MEMORY_PROFILE_KEYS: readonly (keyof RunProfileValues)[] = [
  "topK",
  "verifyReads",
  "rewriteReads",
  "recallMode",
  "abstainThreshold",
  "injectionTokenBudget",
  "correctionTokenBudget",
];

const applicableProfileKeys = (m: unknown): (keyof RunProfileValues)[] =>
  readMemoryOn(m)
    ? PROFILE_FIELD_KEYS
    : PROFILE_FIELD_KEYS.filter((k) => !MEMORY_PROFILE_KEYS.includes(k));

/** Every applicable managed field (all 18, or the 11 non-memory ones while
 * memory is off) matches one preset exactly → that preset; else "custom". */
export function inferRunProfile(m: unknown): RunProfileState {
  const current = effectiveProfileValues(m);
  const keys = applicableProfileKeys(m);
  const match = RUN_PROFILE_IDS.find((profile) =>
    keys.every((k) => current[k] === RUN_PROFILES[profile][k]),
  );
  return match ?? "custom";
}

/** Count of applicable managed fields whose current effective value differs
 * from ``profile``'s target — the confirm dialog's "N settings will change". */
export function countProfileDiff(m: unknown, profile: RunProfile): number {
  const current = effectiveProfileValues(m);
  const target = RUN_PROFILES[profile];
  return applicableProfileKeys(m).filter((k) => current[k] !== target[k])
    .length;
}
