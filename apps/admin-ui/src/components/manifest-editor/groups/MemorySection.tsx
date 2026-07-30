/**
 * MemorySection — "记忆" (Memory) group, config-page redesign v2 Task 2. The
 * group used to embed the "memory" FormView section wholesale (on/off
 * toggle, retrieve_top_k, write-back + a nested "Advanced" panel for
 * verify-reads/write_min_importance/reconcile_writes/recall_mode/
 * rewrite_reads/abstain_threshold) and bolt two curated panels on top
 * (injection budgets, background consolidation). That FormView section is
 * now gone entirely — every field lives here, laid out as three ``FieldRow``
 * (v2) sub-tabs instead of one long scroll:
 *
 * - **basic** — the long-term-memory on/off switch (a hand-wired ``FieldRow``
 *   over a PRESENCE-semantic block, mirrors ``ModelRoutingSection``'s
 *   reflection switch: ``isDefault={!memoryOn}``, no reset button — flipping
 *   the switch itself IS the "reset") plus ``retrieve_top_k`` / ``write_back``.
 * - **retrieval** — the six read/write tuning knobs
 *   (verify_reads/write_min_importance/reconcile_writes/recall_mode/
 *   rewrite_reads/abstain_threshold). The whole TAB is disabled
 *   (``items[].disabled``) while memory is off — the on/off switch lives on
 *   the "basic" tab, so there's no path to these controls while memory is
 *   off in the first place; disabling the tab nav (rather than hiding each
 *   row) is enough.
 * - **budget** — the two curated panels from before (injection budgets,
 *   background consolidation), unchanged: injection budgets only render
 *   while memory is on (a budget over an absent ``long_term`` block is
 *   meaningless — same reasoning as ``top_k``/``write_back`` below);
 *   consolidation is a control-plane job independent of memory on/off, so it
 *   (and its aux-model note) always renders.
 *
 * Every tab's ``children`` is ``forceRender: true`` so all three panes stay
 * mounted regardless of which is active — tests can query any tab's fields
 * via ``[data-field-id]`` without first clicking into it (mirrors this
 * component's own pre-existing Collapse ``forceRender`` convention).
 *
 * ``top_k``/``write_back`` are gated on ``memoryOn`` for the same reason the
 * injection-budget panel is: every ``long_term`` knob setter
 * (``setTopK``/``setWriteBack``/etc., form_model.ts's ``patchLongTerm``)
 * materializes ``long_term: {}`` the moment it's called, so rendering them
 * while memory is off would silently reactivate memory the instant either
 * field is touched.
 */
import { useState } from "react";
import { Collapse, InputNumber, Select, Switch, Tabs, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { FieldRow } from "../FieldRow";
import { PolicyFieldList, type FieldDef } from "./field_defs";
import {
  patchConsolidation,
  patchMemoryBudgets,
  readAbstainThreshold,
  readConsolidation,
  readMemoryBudgets,
  readMemoryOn,
  readReconcileWrites,
  readRecallMode,
  readRewriteReads,
  readTopK,
  readVerifyReads,
  readWriteBack,
  readWriteMinImportance,
  setAbstainThreshold,
  setMemoryOn,
  setReconcileWrites,
  setRecallMode,
  setRewriteReads,
  setTopK,
  setVerifyReads,
  setWriteBack,
  setWriteMinImportance,
  type ConsolidationFields,
  type MemoryBudgetFields,
} from "../form_model";

const { Text } = Typography;

interface MemorySectionProps {
  formData: unknown;
  onChange: (data: unknown) => void;
  /** #2 — controlled sub-tab, lifted to ManifestEditor so the selection
   *  survives this pane unmounting on a group switch. Falls back to local
   *  state when rendered standalone (tests). */
  activeSubTab?: string;
  onSubTabChange?: (key: string) => void;
}

// ① spec.memory.long_term.{injection,correction}_token_budget — the two
// token ceilings around recalled-memory injection. Unchanged by Task 2.
const BUDGET_DEFS: readonly FieldDef[] = [
  {
    fieldId: "memory.long_term.injection_token_budget",
    i18nKey: "memory_group.inj_budget",
    valueKey: "injectionTokenBudget",
    kind: "number",
    effectiveDefault: 2000,
    min: 100,
    max: 100000,
  },
  {
    fieldId: "memory.long_term.correction_token_budget",
    i18nKey: "memory_group.corr_budget",
    valueKey: "correctionTokenBudget",
    kind: "number",
    effectiveDefault: 500,
    min: 0,
    max: 100000,
  },
];

// ② policies.memory_consolidation.enabled — the background consolidator's
// per-agent master switch. Unchanged by Task 2.
const CONSOLIDATION_DEFS: readonly FieldDef[] = [
  {
    fieldId: "policies.memory_consolidation.enabled",
    i18nKey: "memory_group.consolidation",
    valueKey: "consolidationEnabled",
    kind: "switch",
    effectiveDefault: true,
  },
];

export function MemorySection({
  formData,
  onChange,
  activeSubTab,
  onSubTabChange,
}: MemorySectionProps) {
  const { t } = useTranslation();
  const [fallbackSubTab, setFallbackSubTab] = useState("basic");
  const subTab = activeSubTab ?? fallbackSubTab;
  const handleSubTabChange = (key: string): void => {
    setFallbackSubTab(key);
    onSubTabChange?.(key);
  };
  const memoryOn = readMemoryOn(formData);
  const budgetValues = readMemoryBudgets(formData) as Record<
    string,
    number | undefined
  >;
  const consolidationValues = readConsolidation(formData) as Record<
    string,
    boolean | undefined
  >;

  const handleBudgetPatch = (patch: Partial<MemoryBudgetFields>): void => {
    onChange(patchMemoryBudgets(formData, patch));
  };
  const handleConsolidationPatch = (
    patch: Partial<ConsolidationFields>,
  ): void => {
    onChange(patchConsolidation(formData, patch));
  };

  const topK = readTopK(formData) ?? 5;
  const writeBack = readWriteBack(formData);
  const verifyReads = readVerifyReads(formData);
  const writeMinImportance = readWriteMinImportance(formData);
  const reconcileWrites = readReconcileWrites(formData);
  const recallMode = readRecallMode(formData);
  const rewriteReads = readRewriteReads(formData);
  const abstainThreshold = readAbstainThreshold(formData);

  return (
    <div data-testid="memory-section" style={{ maxWidth: 760 }}>
      <Tabs
        size="small"
        activeKey={subTab}
        onChange={handleSubTabChange}
        items={[
          {
            key: "basic",
            label: t("memory_group.tab_basic"),
            forceRender: true,
            children: (
              <div data-testid="memory-tab-basic">
                <FieldRow
                  fieldId="memory.long_term"
                  label={t("memory_group.on_label")}
                  brief={t("memory_group.on_brief")}
                  help={t("memory_group.on_impact")}
                  isDefault={!memoryOn}
                >
                  <Switch
                    checked={memoryOn}
                    aria-label={t("memory_group.on_label")}
                    onChange={(on) => onChange(setMemoryOn(formData, on))}
                  />
                </FieldRow>
                {memoryOn && (
                  <>
                    <FieldRow
                      fieldId="memory.long_term.retrieve_top_k"
                      label={t("memory_group.topk_label")}
                      brief={t("memory_group.topk_brief")}
                      help={t("memory_group.topk_impact")}
                      isDefault={topK === 5}
                      onReset={() => onChange(setTopK(formData, 5))}
                      resetHint="5"
                    >
                      <InputNumber
                        min={1}
                        value={topK}
                        aria-label={t("memory_group.topk_label")}
                        onChange={(v) => onChange(setTopK(formData, v ?? 5))}
                      />
                    </FieldRow>
                    <FieldRow
                      fieldId="memory.long_term.write_back"
                      label={t("memory_group.write_back_label")}
                      brief={t("memory_group.write_back_brief")}
                      help={t("memory_group.write_back_impact")}
                      isDefault={writeBack === true}
                      onReset={() => onChange(setWriteBack(formData, true))}
                      resetHint="true"
                    >
                      <Switch
                        checked={writeBack}
                        aria-label={t("memory_group.write_back_label")}
                        onChange={(on) => onChange(setWriteBack(formData, on))}
                      />
                    </FieldRow>
                  </>
                )}
              </div>
            ),
          },
          {
            key: "retrieval",
            label: t("memory_group.tab_retrieval"),
            disabled: !memoryOn,
            forceRender: true,
            children: (
              <div data-testid="memory-tab-retrieval">
                <FieldRow
                  fieldId="memory.long_term.verify_reads"
                  label={t("memory_group.verify_reads_label")}
                  brief={t("memory_group.verify_reads_brief")}
                  help={t("memory_group.verify_reads_impact")}
                  isDefault={verifyReads === true}
                  onReset={() => onChange(setVerifyReads(formData, true))}
                  resetHint="true"
                >
                  <Switch
                    checked={verifyReads}
                    aria-label={t("memory_group.verify_reads_label")}
                    onChange={(on) => onChange(setVerifyReads(formData, on))}
                  />
                </FieldRow>
                <FieldRow
                  fieldId="memory.long_term.write_min_importance"
                  label={t("memory_group.write_min_importance_label")}
                  brief={t("memory_group.write_min_importance_brief")}
                  help={t("memory_group.write_min_importance_impact")}
                  isDefault={writeMinImportance === 0.3}
                  onReset={() =>
                    onChange(setWriteMinImportance(formData, 0.3))
                  }
                  resetHint="0.3"
                >
                  <InputNumber
                    min={0}
                    max={1}
                    step={0.05}
                    value={writeMinImportance}
                    aria-label={t("memory_group.write_min_importance_label")}
                    onChange={(v) =>
                      onChange(setWriteMinImportance(formData, v ?? 0.3))
                    }
                  />
                </FieldRow>
                <FieldRow
                  fieldId="memory.long_term.reconcile_writes"
                  label={t("memory_group.reconcile_writes_label")}
                  brief={t("memory_group.reconcile_writes_brief")}
                  help={t("memory_group.reconcile_writes_impact")}
                  isDefault={reconcileWrites === true}
                  onReset={() => onChange(setReconcileWrites(formData, true))}
                  resetHint="true"
                >
                  <Switch
                    checked={reconcileWrites}
                    aria-label={t("memory_group.reconcile_writes_label")}
                    onChange={(on) =>
                      onChange(setReconcileWrites(formData, on))
                    }
                  />
                </FieldRow>
                <FieldRow
                  fieldId="memory.long_term.recall_mode"
                  label={t("memory_group.recall_mode_label")}
                  brief={t("memory_group.recall_mode_brief")}
                  help={t("memory_group.recall_mode_impact")}
                  isDefault={recallMode === "per_session"}
                  onReset={() =>
                    onChange(setRecallMode(formData, "per_session"))
                  }
                  resetHint="per_session"
                >
                  <Select
                    style={{ width: 200 }}
                    value={recallMode}
                    aria-label={t("memory_group.recall_mode_label")}
                    onChange={(v) => onChange(setRecallMode(formData, v))}
                    options={[
                      {
                        value: "per_session",
                        label: t("memory_group.recall_mode_per_session"),
                      },
                      {
                        value: "per_turn",
                        label: t("memory_group.recall_mode_per_turn"),
                      },
                    ]}
                  />
                </FieldRow>
                <FieldRow
                  fieldId="memory.long_term.rewrite_reads"
                  label={t("memory_group.rewrite_reads_label")}
                  brief={t("memory_group.rewrite_reads_brief")}
                  help={t("memory_group.rewrite_reads_impact")}
                  isDefault={rewriteReads === false}
                  onReset={() => onChange(setRewriteReads(formData, false))}
                  resetHint="false"
                >
                  <Switch
                    checked={rewriteReads}
                    aria-label={t("memory_group.rewrite_reads_label")}
                    onChange={(on) => onChange(setRewriteReads(formData, on))}
                  />
                </FieldRow>
                <FieldRow
                  fieldId="memory.long_term.abstain_threshold"
                  label={t("memory_group.abstain_threshold_label")}
                  brief={t("memory_group.abstain_threshold_brief")}
                  help={t("memory_group.abstain_threshold_impact")}
                  isDefault={abstainThreshold === 0}
                  onReset={() => onChange(setAbstainThreshold(formData, 0))}
                  resetHint="0"
                >
                  <InputNumber
                    min={0}
                    max={1}
                    step={0.05}
                    value={abstainThreshold}
                    aria-label={t("memory_group.abstain_threshold_label")}
                    onChange={(v) =>
                      onChange(setAbstainThreshold(formData, v ?? 0))
                    }
                  />
                </FieldRow>
              </div>
            ),
          },
          {
            key: "budget",
            label: t("memory_group.tab_budget"),
            forceRender: true,
            children: (
              <div data-testid="memory-tab-budget">
                <Collapse
                  defaultActiveKey={[]}
                  items={[
                    ...(memoryOn
                      ? [
                          {
                            key: "injection",
                            label: t("memory_group.panel_injection"),
                            forceRender: true,
                            children: (
                              <PolicyFieldList
                                defs={BUDGET_DEFS}
                                values={budgetValues}
                                onPatch={handleBudgetPatch}
                              />
                            ),
                          },
                        ]
                      : []),
                    {
                      key: "consolidation",
                      label: t("memory_group.panel_consolidation"),
                      forceRender: true,
                      children: (
                        <>
                          <PolicyFieldList
                            defs={CONSOLIDATION_DEFS}
                            values={consolidationValues}
                            onPatch={handleConsolidationPatch}
                          />
                          <Text
                            type="secondary"
                            style={{ display: "block", marginTop: 8 }}
                          >
                            {t("memory_group.aux_model_note")}
                          </Text>
                        </>
                      ),
                    },
                  ]}
                />
              </div>
            ),
          },
        ]}
      />
    </div>
  );
}
