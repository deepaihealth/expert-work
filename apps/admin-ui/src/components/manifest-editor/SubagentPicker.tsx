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
import { Button, Input, Select, Switch, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { listAgents } from "../../api/agents";
import type { ModelCatalog } from "../../api/model_catalog";
import { FieldHelp } from "../FieldHelp";
import { FieldRow } from "./FieldRow";
import {
  readDynamicWorkersOn,
  readSubagents,
  readWorkerModel,
  setDynamicWorkersOn,
  setSubagents,
  setWorkerModel,
  type SubAgentFields,
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
