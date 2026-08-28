/**
 * RunBudgetSection — pilot "运行预算与超时" (Run Budget & Timeouts) group,
 * Task 6 of the agent-config-page redesign (PR1). The first ``budget`` group
 * pane that renders real controls instead of the group-pending hint: seven
 * knobs that live in three different manifest locations
 * (workflow.max_iterations+type / policies.max_no_progress+run_deadline_s+
 * token_budget / top-level spec.stream_deadline_s+idle_timeout_s) behind one
 * screen.
 *
 * Rendering itself is delegated to ``PolicyFieldList`` (Task 1 of the
 * config-page redesign v2's one-row-per-field ``FieldRow`` renderer over the
 * same FieldDef config-array pattern) — ``RUN_BUDGET_DEFS`` below is the data
 * table, split into two subheaded groups (``STEP_DEFS`` for the
 * step/flow-shaped knobs, ``TIME_DEFS`` for everything measured in seconds or
 * tokens) purely for scanability; this component only wires the
 * ``readRunBudget``/``patchRunBudget`` pair (form_model.ts) to each list.
 *
 * Clearing an InputNumber (antd emits ``null``) reverts that field to the
 * platform default: the patch carries an explicit ``undefined``, which
 * ``patchRunBudget`` treats as "delete this key" (dropping the parent block
 * too when that empties it) rather than writing the default value into the
 * manifest. A select field follows the same rule (selecting back to
 * ``effectiveDefault`` → the patch carries ``undefined`` → key deleted) —
 * see ``workflow.type`` below, PR8 Task 1 (field_defs.tsx's select kind,
 * PR3 Task 1).
 *
 * ``workflow``'s other two YAML-only keys (``early_stop``/``builder``) pass
 * schema validation but the runtime never reads them; the closing note that
 * used to flag this here is gone (config-page redesign v2 Task 1 — FieldRow
 * v2 drops always-visible footnotes in favor of the ⓘ help popover), leaving
 * them untouched and undocumented on this screen — authoring them by hand
 * remains harmless.
 */
import { Typography } from "antd";
import { useTranslation } from "react-i18next";

import { PolicyFieldList, type FieldDef } from "./field_defs";
import {
  patchRunBudget,
  readRunBudget,
  type RunBudgetFields,
} from "../form_model";
import { PlanFirstToggle } from "../widgets/PlanFirstToggle";

const { Text } = Typography;

interface RunBudgetSectionProps {
  formData: unknown;
  onChange: (data: unknown) => void;
}

// Display-layer effective defaults (agent_spec.py WorkflowSpec/PolicySpec/
// AgentSpecBody) live in each def's ``effectiveDefault`` — a stored value of
// ``undefined`` means the backend applies these. i18nKey mirrors the existing
// ``run_budget.*`` locale keys (already four-key complete per field).
const RUN_BUDGET_DEFS: readonly FieldDef[] = [
  {
    fieldId: "workflow.max_iterations",
    i18nKey: "run_budget.max_iterations",
    valueKey: "maxIterations",
    kind: "number",
    effectiveDefault: 30,
    min: 1,
  },
  {
    fieldId: "workflow.type",
    i18nKey: "run_budget.wf_type",
    valueKey: "workflowType",
    kind: "select",
    effectiveDefault: "react",
    options: ["react", "plan_execute", "custom"],
    optionLabelKey: "run_budget.wf_type_opt",
  },
  {
    fieldId: "policies.max_no_progress",
    i18nKey: "run_budget.max_no_progress",
    valueKey: "maxNoProgress",
    kind: "number",
    effectiveDefault: 0,
    min: 0,
  },
  {
    fieldId: "policies.run_deadline_s",
    i18nKey: "run_budget.run_deadline",
    valueKey: "runDeadlineS",
    kind: "number",
    effectiveDefault: 0,
    min: 0,
    max: 86400,
  },
  {
    fieldId: "policies.token_budget",
    i18nKey: "run_budget.token_budget",
    valueKey: "tokenBudget",
    kind: "number",
    effectiveDefault: 0,
    min: 0,
  },
  {
    fieldId: "spec.stream_deadline_s",
    i18nKey: "run_budget.stream_deadline",
    valueKey: "streamDeadlineS",
    kind: "number",
    effectiveDefault: 180,
    min: 0,
    max: 3600,
  },
  {
    fieldId: "spec.idle_timeout_s",
    i18nKey: "run_budget.idle_timeout",
    valueKey: "idleTimeoutS",
    kind: "number",
    effectiveDefault: 45,
    min: 0,
    max: 600,
  },
];

// STEP_DEFS = the step/flow-shaped knobs; TIME_DEFS = everything else
// (seconds/tokens), rendered under their own subhead for scanability.
const STEP_DEFS: readonly FieldDef[] = RUN_BUDGET_DEFS.filter((d) =>
  ["workflow.max_iterations", "workflow.type", "policies.max_no_progress"].includes(d.fieldId),
);
const TIME_DEFS: readonly FieldDef[] = RUN_BUDGET_DEFS.filter(
  (d) => !STEP_DEFS.includes(d),
);

export function RunBudgetSection({ formData, onChange }: RunBudgetSectionProps) {
  const { t } = useTranslation();
  const budget = readRunBudget(formData) as Record<
    string,
    number | string | undefined
  >;

  const handlePatch = (patch: Partial<RunBudgetFields>): void => {
    onChange(patchRunBudget(formData, patch));
  };

  return (
    <div data-testid="run-budget-section" style={{ maxWidth: 760 }}>
      <Text strong style={{ display: "block", marginBottom: 8 }}>
        {t("run_budget.subhead_steps")}
      </Text>
      <PolicyFieldList defs={STEP_DEFS} values={budget} onPatch={handlePatch} />
      {/* B-35 — plan_first 开关带联动确认 Modal,不走 FieldDef 表
          (需要 Modal 交互 + 三字段一次写入,FieldRow 覆盖不了)。 */}
      <PlanFirstToggle formData={formData} onChange={onChange} />
      <Text strong style={{ display: "block", margin: "16px 0 8px" }}>
        {t("run_budget.subhead_time")}
      </Text>
      <PolicyFieldList defs={TIME_DEFS} values={budget} onPatch={handlePatch} />
    </div>
  );
}
