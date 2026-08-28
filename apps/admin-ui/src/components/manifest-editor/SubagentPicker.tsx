/**
 * Sub-agent tab — both delegation tracks in one place (2026-08-26 用户反馈:
 * 动态开关原先藏在安全分区的 network 子 tab,界面上找不到):
 *
 * - **动态子 Agent** (``dynamic_workers.enabled``, default-on): whether the
 *   parent's LLM gets ``spawn_worker`` to create ephemeral sub-agents at
 *   run time; concurrency/count bounds are platform-global settings.
 * - **静态名册** (``subagents``): each named row binds a tool name → a
 *   deployed agent ref the parent may delegate sub-tasks to (the parent's
 *   LLM sees each as a tool).
 *
 * Emits the FULL merged manifest via the form_model writers.
 */
import { useEffect, useState, type CSSProperties, type ReactNode } from "react";
import { Button, Input, InputNumber, Select, Switch, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { listAgents } from "../../api/agents";
import type { ModelCatalog } from "../../api/model_catalog";
import { FieldHelp } from "../FieldHelp";
import { FieldRow } from "./FieldRow";
import {
  readDynamicWorkersOn,
  readRunBudget,
  readSubagents,
  readWorkerBudget,
  readWorkerModel,
  setDynamicWorkersOn,
  setSubagents,
  setWorkerBudgetField,
  setWorkerModel,
  type SubAgentFields,
  type WorkerBudgetFields,
} from "./form_model";
import { ModelSelect } from "./widgets/ModelSelect";

const { Text } = Typography;

const SECTION: CSSProperties = { marginBottom: 24 };

function Heading({ children }: { children: ReactNode }) {
  return <h3 style={{ fontSize: 15, margin: "0 0 12px" }}>{children}</h3>;
}

interface SubagentPickerProps {
  formData: unknown;
  onChange: (data: unknown) => void;
  /** Model catalog for the worker-model override picker. */
  catalog?: ModelCatalog;
}

/** 步数上限高到这个值以上、且没设运行 Token 预算时,展示成本兜底软提示。 */
const TOKEN_HINT_ITERATIONS_THRESHOLD = 40;

const WORKER_BUDGET_ROWS: ReadonlyArray<{
  key: keyof WorkerBudgetFields;
  labelKey: string;
  max: number;
}> = [
  {
    key: "max_iterations",
    labelKey: "agent_form.worker_budget_max_iterations_label",
    max: 512,
  },
  {
    key: "max_concurrent",
    labelKey: "agent_form.worker_budget_max_concurrent_label",
    max: 64,
  },
  {
    key: "max_per_run",
    labelKey: "agent_form.worker_budget_max_per_run_label",
    max: 1024,
  },
];

function WorkerBudgetSection({
  budget,
  tokenBudget,
  onFieldChange,
}: {
  budget: WorkerBudgetFields;
  tokenBudget: number | undefined;
  onFieldChange: (key: keyof WorkerBudgetFields, value: number | null) => void;
}) {
  const { t } = useTranslation();
  const showTokenHint =
    (budget.max_iterations ?? 0) > TOKEN_HINT_ITERATIONS_THRESHOLD &&
    !tokenBudget;
  return (
    <div data-testid="af-worker-budget" style={{ margin: "0 0 24px" }}>
      <label style={{ display: "block", marginBottom: 4 }}>
        {t("agent_form.worker_budget_heading")}
        <FieldHelp
          text={t("agent_form.worker_budget_hint")}
          testId="af-worker-budget"
        />
      </label>
      <Text type="secondary" style={{ display: "block", marginBottom: 8, fontSize: 12 }}>
        {t("agent_form.worker_budget_hint")}
      </Text>
      {WORKER_BUDGET_ROWS.map(({ key, labelKey, max }) => (
        <div
          key={key}
          style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}
        >
          <span style={{ fontSize: 13 }}>{t(labelKey)}</span>
          <InputNumber
            min={1}
            max={max}
            value={budget[key] ?? null}
            placeholder={t("agent_form.worker_budget_platform_default")}
            aria-label={t(labelKey)}
            data-testid={`af-worker-budget-${key.replace(/_/g, "-")}`}
            onChange={(v) => onFieldChange(key, v ?? null)}
          />
        </div>
      ))}
      {showTokenHint && (
        <Text
          type="warning"
          style={{ display: "block", fontSize: 12 }}
          data-testid="af-worker-budget-token-hint"
        >
          {t("agent_form.worker_budget_token_hint")}
        </Text>
      )}
    </div>
  );
}

export function SubagentPicker({ formData, onChange, catalog }: SubagentPickerProps) {
  const { t } = useTranslation();
  const [agents, setAgents] = useState<string[]>([]);

  useEffect(() => {
    let alive = true;
    listAgents().then(
      (a) =>
        alive && setAgents((a?.items ?? []).map((x) => `${x.name}@${x.version}`)),
      () => {},
    );
    return () => {
      alive = false;
    };
  }, []);

  const subagents = readSubagents(formData);
  const toOptions = (values: string[]) =>
    values.map((v) => ({ label: v, value: v }));

  const patchSubagent = (i: number, patch: Partial<SubAgentFields>): void => {
    const next = subagents.map((row, idx) =>
      idx === i ? { ...row, ...patch } : row,
    );
    onChange(setSubagents(formData, next));
  };
  const addSubagent = (): void =>
    onChange(
      setSubagents(formData, [
        ...subagents,
        { name: "", agent_ref: "", description: "" },
      ]),
    );
  const removeSubagent = (i: number): void =>
    onChange(
      setSubagents(
        formData,
        subagents.filter((_, idx) => idx !== i),
      ),
    );

  const dynamicWorkersOn = readDynamicWorkersOn(formData);

  return (
    <section data-testid="af-subagents" style={SECTION}>
      {/* 动态轨 —— spawn_worker 开关。和静态名册同屏,一屏答完「预置了谁 +
          能不能临时造」;原先藏在安全分区 network 子 tab(2026-08-26 反馈)。 */}
      <FieldRow
        fieldId="dynamic_workers.enabled"
        label={t("agent_form.section_dynamic_workers")}
        brief={t("agent_form.dynamic_workers_hint")}
        help={t("agent_form.section_dynamic_workers_help")}
        isDefault={dynamicWorkersOn === true}
        onReset={() => onChange(setDynamicWorkersOn(formData, true))}
        resetHint="true"
      >
        <Switch
          checked={dynamicWorkersOn}
          aria-label={t("agent_form.section_dynamic_workers")}
          onChange={(on) => onChange(setDynamicWorkersOn(formData, on))}
        />
      </FieldRow>

      {/* Worker-model override — dynamic_workers.model. Unset = workers
          inherit the parent's model verbatim; set = a cheaper tier for
          fan-out work (no fallback chain — backend validator rejects one).
          Mirrors the vision-model picker's set/clear shape. */}
      {dynamicWorkersOn && (
        <div data-testid="af-worker-model" style={{ margin: "0 0 24px" }}>
          <label style={{ display: "block", marginBottom: 4 }}>
            {t("agent_form.worker_model_label")}
            <FieldHelp
              text={t("agent_form.worker_model_hint")}
              testId="af-worker-model"
            />
          </label>
          <Text type="secondary" style={{ display: "block", marginBottom: 8, fontSize: 12 }}>
            {t("agent_form.worker_model_hint")}
          </Text>
          <ModelSelect
            value={readWorkerModel(formData) ?? {}}
            catalog={catalog}
            onChange={(mdl) => onChange(setWorkerModel(formData, mdl))}
          />
          {readWorkerModel(formData) !== undefined && (
            <Button
              type="link"
              size="small"
              data-testid="af-worker-model-clear"
              style={{ paddingLeft: 0 }}
              onClick={() => onChange(setWorkerModel(formData, null))}
            >
              {t("agent_form.worker_model_clear")}
            </Button>
          )}
        </div>
      )}

      {/* 弹性 worker 预算 — per-agent budget requests. Blank = the platform
          default tier; a set value is clamped to the platform hard cap at
          run time (the backend owns the clamp, so no cap value is shown —
          tenant admins cannot read the platform config). */}
      {dynamicWorkersOn && (
        <WorkerBudgetSection
          budget={readWorkerBudget(formData)}
          tokenBudget={readRunBudget(formData).tokenBudget}
          onFieldChange={(key, value) =>
            onChange(setWorkerBudgetField(formData, key, value))
          }
        />
      )}

      <Heading>
        {t("agent_form.section_subagents")}
        <FieldHelp
          text={t("agent_form.section_subagents_help")}
          testId="af-subagents"
        />
      </Heading>
      <Text type="secondary" style={{ display: "block", marginBottom: 8 }}>
        {t("agent_form.subagents_hint")}
      </Text>
      {subagents.map((row, i) => (
        <div
          key={i}
          data-testid={`af-subagent-row-${i}`}
          style={{
            display: "flex",
            gap: 8,
            marginBottom: 8,
            alignItems: "center",
          }}
        >
          <Input
            style={{ width: 160 }}
            value={row.name ?? ""}
            data-testid={`af-subagent-name-${i}`}
            aria-label={t("agent_form.subagent_name")}
            placeholder={t("agent_form.subagent_name")}
            onChange={(e) => patchSubagent(i, { name: e.target.value })}
          />
          <Select
            style={{ width: 200 }}
            value={row.agent_ref || undefined}
            options={toOptions(agents)}
            data-testid={`af-subagent-ref-${i}`}
            aria-label={t("agent_form.subagent_ref")}
            placeholder={t("agent_form.subagent_ref")}
            onChange={(v: string) => patchSubagent(i, { agent_ref: v })}
          />
          <Input
            style={{ flex: 1 }}
            value={row.description ?? ""}
            data-testid={`af-subagent-desc-${i}`}
            aria-label={t("agent_form.subagent_description")}
            placeholder={t("agent_form.subagent_description")}
            onChange={(e) => patchSubagent(i, { description: e.target.value })}
          />
          <Button
            type="text"
            danger
            size="small"
            data-testid={`af-subagent-remove-${i}`}
            aria-label={t("agent_form.subagent_remove")}
            onClick={() => removeSubagent(i)}
          >
            {t("agent_form.subagent_remove")}
          </Button>
        </div>
      ))}
      <Button
        type="dashed"
        size="small"
        data-testid="af-subagent-add"
        onClick={addSubagent}
      >
        {t("agent_form.subagent_add")}
      </Button>
    </section>
  );
}
