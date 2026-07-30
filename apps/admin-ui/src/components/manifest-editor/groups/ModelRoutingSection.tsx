/**
 * ModelRoutingSection — "模型与路由" (Model & Routing) group, Task 3 of PR6
 * (agent-config-page redesign). The group already had real content via the
 * existing "model" FormView section (provider/model picker, fallback chain,
 * the reflection-evaluator model picker, vision fallback) — this component
 * embeds that FormView FIRST, unchanged, then adds the one curated panel the
 * group didn't have a home for before: reflection self-assessment
 * (``spec.reflection`` — Stream J.11's reflect-node config). The
 * reflection-evaluator model picker (``routing.rules[when=reflection]``,
 * formerly its own unconditional FormView sub-section) now rides this panel
 * right after the switch, rendered only while reflection is ON — picking a
 * judge for a disabled reflection pass was the confusion the merge removes.
 * Clearing it keeps its meaning: no rule = reflection reuses the agent's own
 * model. config-page
 * redesign v2 Task 5 flattened this panel from a collapsible ``Collapse``
 * into a plain heading + always-visible switch — every sibling curated pane
 * that used to fold optional content behind a click now shows it directly
 * (FieldRow v2's "don't hide the knob" convention; the collapse's own
 * "don't fatigue" job is now just not having many rows to begin with).
 *
 * Unlike the two tuning fields inside it, the activation switch is hand-wired
 * directly against ``FieldRow`` rather than folded into a ``FieldDef``:
 * ``PolicyFieldList``'s switch semantics are "value === effectiveDefault →
 * delete the key" (the optional-sub-block idiom other curated panes' switches
 * use), but ``reflection`` is a PRESENCE-semantic block
 * (``readReflectionOn``/``setReflectionOn``) — turning it off must delete the
 * WHOLE block, not flip a boolean inside it. The two tuning ``FieldDef``s
 * (budget / deadline_s) render via ``PolicyFieldList`` only while reflection
 * is on — a budget/deadline over an absent ``reflection`` block is
 * meaningless (mirrors ``MemorySection``'s injection-budget render guard),
 * so they must be absent from the DOM (not merely hidden) while it's off,
 * including the explicit ``reflection: null`` case.
 *
 * A closing note flags the model-group fields that stay YAML-only for now:
 * the ``routing.rules`` planning rule, ``vision.fallbacks``, the
 * base_url/azure_* connection fields, and the deprecated ``api_key_ref``.
 */
import { useEffect, useState } from "react";
import { Button, Switch, Typography } from "antd";
import { useTranslation } from "react-i18next";

import type { ModelCatalog } from "../../../api/model_catalog";
import { FieldHelp } from "../../FieldHelp";
import { FieldRow } from "../FieldRow";
import { FormView } from "../FormView";
import { loadModelCatalog } from "../catalog";
import { ModelSelect } from "../widgets/ModelSelect";
import { PolicyFieldList, type FieldDef } from "./field_defs";
import {
  patchReflectionTuning,
  readReflectionEvaluator,
  readReflectionEvaluatorOn,
  readReflectionOn,
  readReflectionTuning,
  setReflectionEvaluator,
  setReflectionOn,
  type ReflectionTuningFields,
} from "../form_model";

const { Text } = Typography;

interface ModelRoutingSectionProps {
  formData: unknown;
  onChange: (data: unknown) => void;
}

// spec.reflection.{budget,deadline_s} — only meaningful while reflection is
// on (see the render guard in the component below).
const TUNING_DEFS: readonly FieldDef[] = [
  {
    fieldId: "reflection.budget",
    i18nKey: "model_group.rf_budget",
    valueKey: "budget",
    kind: "number",
    effectiveDefault: 2,
    min: 1,
  },
  {
    fieldId: "reflection.deadline_s",
    i18nKey: "model_group.rf_deadline",
    valueKey: "deadlineS",
    kind: "number",
    effectiveDefault: 30,
    min: 1,
    max: 600,
  },
];

export function ModelRoutingSection({
  formData,
  onChange,
}: ModelRoutingSectionProps) {
  const { t } = useTranslation();
  // The evaluator ModelSelect needs its own catalog — the embedded FormView
  // loads one internally but doesn't share it (same load/mock path though).
  const [catalog, setCatalog] = useState<ModelCatalog | undefined>(undefined);

  useEffect(() => {
    let alive = true;
    loadModelCatalog().then(
      (c) => {
        if (alive) setCatalog(c);
      },
      () => {
        /* catalog optional — ModelSelect degrades to a disabled/loading select */
      },
    );
    return () => {
      alive = false;
    };
  }, []);

  const reflectionOn = readReflectionOn(formData);
  const tuningValues = readReflectionTuning(formData) as Record<
    string,
    number | undefined
  >;

  const handleTuningPatch = (
    patch: Partial<ReflectionTuningFields>,
  ): void => {
    onChange(patchReflectionTuning(formData, patch));
  };

  return (
    <div data-testid="model-routing-section" style={{ maxWidth: 760 }}>
      <FormView
        formData={formData}
        onChange={onChange}
        sections={["model"]}
      />
      <Text strong style={{ display: "block", marginTop: 24, marginBottom: 12 }}>
        {t("model_group.panel_reflection")}
      </Text>
      <FieldRow
        fieldId="reflection"
        label={t("model_group.rf_enable_label")}
        brief={t("model_group.rf_enable_brief")}
        help={t("model_group.rf_enable_impact")}
        isDefault={!reflectionOn}
      >
        <Switch
          checked={reflectionOn}
          aria-label={t("model_group.rf_enable_label")}
          onChange={(on) => onChange(setReflectionOn(formData, on))}
        />
      </FieldRow>
      {reflectionOn && (
        <>
          {/* Reflection-evaluator model (routing.rules[when=reflection]) —
            only meaningful while reflection is on, same guard as the tuning
            rows below. Unset = reflection reuses the agent's own model. */}
          <div data-testid="af-reflection-evaluator" style={{ marginBottom: 12 }}>
            <label style={{ display: "block", marginBottom: 4 }}>
              {t("agent_form.section_reflection_evaluator")}
              <FieldHelp
                text={t("agent_form.section_reflection_evaluator_help")}
                testId="af-reflection-evaluator"
              />
            </label>
            <Text
              type="secondary"
              style={{ display: "block", marginBottom: 8, fontSize: 12 }}
            >
              {t("agent_form.reflection_evaluator_hint")}
            </Text>
            <ModelSelect
              value={readReflectionEvaluator(formData) ?? {}}
              catalog={catalog}
              onChange={(mdl) =>
                onChange(setReflectionEvaluator(formData, mdl))
              }
            />
            {readReflectionEvaluatorOn(formData) && (
              <Button
                type="link"
                size="small"
                data-testid="af-reflection-evaluator-clear"
                style={{ paddingLeft: 0 }}
                onClick={() =>
                  onChange(setReflectionEvaluator(formData, null))
                }
              >
                {t("agent_form.reflection_evaluator_clear")}
              </Button>
            )}
          </div>
          <PolicyFieldList
            defs={TUNING_DEFS}
            values={tuningValues}
            onPatch={handleTuningPatch}
          />
        </>
      )}
      <Text
        type="secondary"
        data-testid="model-yaml-note"
        style={{ display: "block", marginTop: 16 }}
      >
        {t("model_group.yaml_note")}
      </Text>
    </div>
  );
}
