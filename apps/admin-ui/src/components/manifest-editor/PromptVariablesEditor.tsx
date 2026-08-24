/**
 * Dynamic-Prompt — the Jinja toggle + declared-variable editor for the system
 * prompt. When Jinja is on, the ``template`` is rendered per-run with the run
 * request's ``inputs``; the declared variables are the contract those inputs
 * are validated against. ``trusted`` decides whether a value renders verbatim
 * or is spotlight-fenced as DATA (default trusted — an owner-set posture).
 * Every control emits the FULL merged manifest via the form_model writers.
 */
import { type CSSProperties, type ReactNode } from "react";
import { Button, Input, Switch, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { FieldHelp } from "../FieldHelp";
import {
  readPromptJinja,
  readPromptVariables,
  setPromptJinja,
  setPromptVariables,
  type PromptVariableFields,
} from "./form_model";

const { Text } = Typography;

const SECTION: CSSProperties = { marginBottom: 24 };

function Heading({ children }: { children: ReactNode }) {
  return <h3 style={{ fontSize: 15, margin: "0 0 12px" }}>{children}</h3>;
}

interface PromptVariablesEditorProps {
  formData: unknown;
  onChange: (data: unknown) => void;
}

export function PromptVariablesEditor({
  formData,
  onChange,
}: PromptVariablesEditorProps) {
  const { t } = useTranslation();
  const jinja = readPromptJinja(formData);
  const variables = readPromptVariables(formData);

  const patchVar = (i: number, patch: Partial<PromptVariableFields>): void => {
    const next = variables.map((row, idx) =>
      idx === i ? { ...row, ...patch } : row,
    );
    onChange(setPromptVariables(formData, next));
  };
  const addVar = (): void =>
    onChange(
      setPromptVariables(formData, [
        ...variables,
        { name: "", trusted: true, required: true, description: "" },
      ]),
    );
  const removeVar = (i: number): void =>
    onChange(
      setPromptVariables(
        formData,
        variables.filter((_, idx) => idx !== i),
      ),
    );

  return (
    <section data-testid="af-prompt-vars" style={SECTION}>
      <Heading>
        {t("agent_form.section_prompt_vars")}
        <FieldHelp
          text={t("agent_form.section_prompt_vars_help")}
          testId="af-prompt-vars"
        />
      </Heading>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          marginBottom: 8,
        }}
      >
        <Switch
          checked={jinja}
          data-testid="af-prompt-jinja"
          aria-label={t("agent_form.prompt_jinja_label")}
          onChange={(on) => onChange(setPromptJinja(formData, on))}
        />
        <Text>{t("agent_form.prompt_jinja_label")}</Text>
      </div>
      <Text type="secondary" style={{ display: "block", marginBottom: 12 }}>
        {t("agent_form.prompt_jinja_hint")}
      </Text>

      {jinja && (
        <>
          {variables.length > 0 && (
            <Text
              type="secondary"
              data-testid="af-prompt-vars-count"
              style={{ display: "block", marginBottom: 6, fontSize: 12 }}
            >
              {t("agent_form.prompt_vars_count", { n: variables.length })}
            </Text>
          )}
          {/* BUG-4:变量一多把整页撑成一面墙 —— 列表内滚,添加按钮留在
              容器外恒可见。 */}
          <div
            data-testid="af-prompt-vars-scroll"
            style={{ maxHeight: "40vh", overflowY: "auto", marginBottom: 8 }}
          >
          {variables.map((row, i) => (
            <div
              key={i}
              data-testid={`af-prompt-var-row-${i}`}
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
                data-testid={`af-prompt-var-name-${i}`}
                aria-label={t("agent_form.prompt_var_name")}
                placeholder={t("agent_form.prompt_var_name")}
                onChange={(e) => patchVar(i, { name: e.target.value })}
              />
              <span style={{ display: "flex", alignItems: "center", gap: 4 }}>
                <Switch
                  size="small"
                  checked={row.trusted !== false}
                  data-testid={`af-prompt-var-trusted-${i}`}
                  aria-label={t("agent_form.prompt_var_trusted")}
                  onChange={(on) => patchVar(i, { trusted: on })}
                />
                <Text type="secondary">
                  {t("agent_form.prompt_var_trusted")}
                </Text>
              </span>
              <span style={{ display: "flex", alignItems: "center", gap: 4 }}>
                <Switch
                  size="small"
                  checked={row.required !== false}
                  data-testid={`af-prompt-var-required-${i}`}
                  aria-label={t("agent_form.prompt_var_required")}
                  onChange={(on) => patchVar(i, { required: on })}
                />
                <Text type="secondary">
                  {t("agent_form.prompt_var_required")}
                </Text>
              </span>
              <Input
                style={{ flex: 1 }}
                value={row.description ?? ""}
                data-testid={`af-prompt-var-desc-${i}`}
                aria-label={t("agent_form.prompt_var_description")}
                placeholder={t("agent_form.prompt_var_description")}
                onChange={(e) => patchVar(i, { description: e.target.value })}
              />
              <Button
                type="text"
                danger
                size="small"
                data-testid={`af-prompt-var-remove-${i}`}
                aria-label={t("agent_form.prompt_var_remove")}
                onClick={() => removeVar(i)}
              >
                {t("agent_form.prompt_var_remove")}
              </Button>
            </div>
          ))}
          </div>
          <Button
            type="dashed"
            size="small"
            data-testid="af-prompt-var-add"
            onClick={addVar}
          >
            {t("agent_form.prompt_var_add")}
          </Button>
        </>
      )}
    </section>
  );
}
