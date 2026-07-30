/**
 * ContextGatesSection — "上下文与压缩" (Context & Compression) group, Task 3
 * of the agent-config-page redesign (PR2), reshaped again by config-page
 * redesign v2's own Task 3. Visualizes the three sequential context gates
 * (PolicySpec.tool_result_prune → .working_memory → .context_compression)
 * plus the sibling tool-output-budget master switch.
 *
 * Structure: an intro ``Text`` explaining the three-gate order, then the
 * tool-output-budget master switch (a single-field ``PolicyFieldList``) as
 * its own row ABOVE the Tabs — it's a sibling "policies" block, not a fourth
 * gate in the sequence, so it doesn't belong inside the ①②③ tab strip. Below
 * that, an antd ``Tabs`` (``size="small"``, mirrors ``MemorySection``'s
 * sub-tab precedent) with one tab per gate — prune (①, default active) /
 * window (②) / compress (③) — each rendering its ``PolicyFieldList`` (Task
 * 1's declarative one-row-per-field renderer). Unlike ``MemorySection``,
 * these tabs are NOT ``forceRender``: a hidden tab's fields simply aren't
 * mounted, matching antd Tabs' default lazy-mount behavior — there's no
 * cross-tab visibility requirement here (every write already goes through
 * the same ``handlePatch``/``readContextGates`` pair regardless of which tab
 * is open), so the extra mounted-but-hidden DOM has no payoff.
 *
 * Each group's field array is a ``FieldDef`` table (Task 1's declarative
 * pattern, same as ``RunBudgetSection``); this component only wires the
 * ``readContextGates``/``patchContextGates`` pair (form_model.ts, Task 2) to
 * it. ``kind: "number"`` fields with ``effectiveDefault: null`` (max_turns /
 * max_tokens) render as an empty InputNumber when unset — unset IS the
 * feature-off state (a coarse legacy cap that predates the three gates), not
 * "falls back to a numeric default".
 */
import { useState } from "react";
import { Tabs, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { PolicyFieldList, type FieldDef } from "./field_defs";
import {
  patchContextGates,
  readContextGates,
  type ContextGatesFields,
} from "../form_model";

const { Text } = Typography;

interface ContextGatesSectionProps {
  formData: unknown;
  onChange: (data: unknown) => void;
  /** #2 — controlled sub-tab, lifted to ManifestEditor so the selection
   *  survives this pane unmounting on a group switch. Falls back to local
   *  state when rendered standalone (tests). */
  activeSubTab?: string;
  onSubTabChange?: (key: string) => void;
}

// ① tool_result_prune — the cheapest, first-line gate: collapses old tool
// results into a reference. i18nKey prefix ``pr_`` mirrors ContextGatesFields'
// ``pr*`` value keys.
const TOOL_RESULT_PRUNE_DEFS: readonly FieldDef[] = [
  {
    fieldId: "policies.tool_result_prune.enabled",
    i18nKey: "context_gates.pr_enabled",
    valueKey: "prEnabled",
    kind: "switch",
    effectiveDefault: true,
  },
  {
    fieldId: "policies.tool_result_prune.threshold_pct",
    i18nKey: "context_gates.pr_threshold_pct",
    valueKey: "prThresholdPct",
    kind: "percent",
    effectiveDefault: 0.7,
  },
  {
    fieldId: "policies.tool_result_prune.recent_tool_results_kept",
    i18nKey: "context_gates.pr_recent_kept",
    valueKey: "prRecentKept",
    kind: "number",
    effectiveDefault: 4,
    min: 0,
  },
];

// ② working_memory — the second gate: trims to first turn + N recent turns,
// no LLM call. i18nKey prefix ``wm_``.
const WORKING_MEMORY_DEFS: readonly FieldDef[] = [
  {
    fieldId: "policies.working_memory.enabled",
    i18nKey: "context_gates.wm_enabled",
    valueKey: "wmEnabled",
    kind: "switch",
    effectiveDefault: true,
  },
  {
    fieldId: "policies.working_memory.threshold_pct",
    i18nKey: "context_gates.wm_threshold_pct",
    valueKey: "wmThresholdPct",
    kind: "percent",
    effectiveDefault: 0.7,
  },
  {
    fieldId: "policies.working_memory.max_recent_turns",
    i18nKey: "context_gates.wm_max_recent_turns",
    valueKey: "wmMaxRecentTurns",
    kind: "number",
    effectiveDefault: 20,
    min: 1,
  },
  {
    fieldId: "policies.working_memory.keep_first_turn",
    i18nKey: "context_gates.wm_keep_first_turn",
    valueKey: "wmKeepFirstTurn",
    kind: "switch",
    effectiveDefault: true,
  },
];

// ③ context_compression — the last gate: an LLM summarizes the dropped
// middle. i18nKey prefix ``cc_``. ``max_turns``/``max_tokens`` are the old
// coarse pre-three-gates cap (effectiveDefault null = unset means off, never
// a numeric fallback).
const CONTEXT_COMPRESSION_DEFS: readonly FieldDef[] = [
  {
    fieldId: "policies.context_compression.enabled",
    i18nKey: "context_gates.cc_enabled",
    valueKey: "ccEnabled",
    kind: "switch",
    effectiveDefault: true,
  },
  {
    fieldId: "policies.context_compression.threshold_pct",
    i18nKey: "context_gates.cc_threshold_pct",
    valueKey: "ccThresholdPct",
    kind: "percent",
    effectiveDefault: 0.7,
  },
  {
    fieldId: "policies.context_compression.head_keep",
    i18nKey: "context_gates.cc_head_keep",
    valueKey: "ccHeadKeep",
    kind: "number",
    effectiveDefault: 4,
    min: 0,
  },
  {
    fieldId: "policies.context_compression.tail_keep",
    i18nKey: "context_gates.cc_tail_keep",
    valueKey: "ccTailKeep",
    kind: "number",
    effectiveDefault: 6,
    min: 0,
  },
  {
    fieldId: "policies.context_compression.flush_before_compaction",
    i18nKey: "context_gates.cc_flush_before_compaction",
    valueKey: "ccFlushBeforeCompaction",
    kind: "switch",
    effectiveDefault: true,
  },
  {
    fieldId: "policies.context_compression.max_passes",
    i18nKey: "context_gates.cc_max_passes",
    valueKey: "ccMaxPasses",
    kind: "number",
    effectiveDefault: 3,
    min: 1,
  },
  {
    fieldId: "policies.context_compression.max_turns",
    i18nKey: "context_gates.cc_max_turns",
    valueKey: "ccMaxTurns",
    kind: "number",
    effectiveDefault: null,
    min: 1,
  },
  {
    fieldId: "policies.context_compression.max_tokens",
    i18nKey: "context_gates.cc_max_tokens",
    valueKey: "ccMaxTokens",
    kind: "number",
    effectiveDefault: null,
    min: 1,
  },
  {
    fieldId: "policies.context_compression.pressure_feedback",
    i18nKey: "context_gates.cc_pressure_feedback",
    valueKey: "ccPressureFeedback",
    kind: "switch",
    effectiveDefault: true,
  },
  {
    fieldId: "policies.context_compression.pressure_warn_pct",
    i18nKey: "context_gates.cc_pressure_warn_pct",
    valueKey: "ccPressureWarnPct",
    kind: "percent",
    effectiveDefault: 0.75,
  },
];

// ④ tool_output_budget — sibling of the three gates (not itself a context
// gate, but the same "policies" block family): per-agent master switch for
// the tool-output-budget feature. i18nKey prefix ``budget_``.
const TOOL_OUTPUT_BUDGET_DEFS: readonly FieldDef[] = [
  {
    fieldId: "policies.tool_output_budget.enabled",
    i18nKey: "context_gates.budget_enabled",
    valueKey: "budgetEnabled",
    kind: "switch",
    effectiveDefault: true,
  },
];

export function ContextGatesSection({
  formData,
  onChange,
  activeSubTab,
  onSubTabChange,
}: ContextGatesSectionProps) {
  const { t } = useTranslation();
  const [fallbackSubTab, setFallbackSubTab] = useState("prune");
  const subTab = activeSubTab ?? fallbackSubTab;
  const handleSubTabChange = (key: string): void => {
    setFallbackSubTab(key);
    onSubTabChange?.(key);
  };
  const gates = readContextGates(formData);
  const values = gates as Record<string, number | boolean | undefined>;

  const handlePatch = (patch: Partial<ContextGatesFields>): void => {
    onChange(patchContextGates(formData, patch));
  };

  return (
    <div data-testid="context-gates-section" style={{ maxWidth: 760 }}>
      <Text type="secondary" style={{ display: "block", marginBottom: 16 }}>
        {t("context_gates.group_intro")}
      </Text>
      <PolicyFieldList
        defs={TOOL_OUTPUT_BUDGET_DEFS}
        values={values}
        onPatch={handlePatch}
      />
      <Tabs
        size="small"
        activeKey={subTab}
        onChange={handleSubTabChange}
        items={[
          {
            key: "prune",
            label: t("context_gates.panel_tool_result_prune"),
            children: (
              <PolicyFieldList
                defs={TOOL_RESULT_PRUNE_DEFS}
                values={values}
                onPatch={handlePatch}
              />
            ),
          },
          {
            key: "window",
            label: t("context_gates.panel_working_memory"),
            children: (
              <PolicyFieldList
                defs={WORKING_MEMORY_DEFS}
                values={values}
                onPatch={handlePatch}
              />
            ),
          },
          {
            key: "compress",
            label: t("context_gates.panel_context_compression"),
            children: (
              <PolicyFieldList
                defs={CONTEXT_COMPRESSION_DEFS}
                values={values}
                onPatch={handlePatch}
              />
            ),
          },
        ]}
      />
    </div>
  );
}
