/**
 * VariablesForm — one input row per Jinja prompt variable, lifted verbatim
 * out of ``PlaygroundTab.tsx`` (调试台重设计 PR-A Task 7; see
 * ``playground-vars`` / ``playground-var-<name>`` testids there).
 *
 * ``readOnly``/tenant-switch state is NOT read here — the parent decides
 * whether inputs are ``disabled`` and passes it down as a plain prop.
 */
import type { JSX } from "react";
import { Input, Typography } from "antd";
import { useTranslation } from "react-i18next";

const { Text } = Typography;

export interface PromptVariable {
  name: string;
  description?: string;
  required?: boolean;
}

/** 纯函数:必填且值为空/全空白的变量名,按声明顺序。导出给 Composer / PlaygroundTab。 */
export function missingRequired(
  vars: readonly PromptVariable[],
  values: Readonly<Record<string, string>>,
): string[] {
  return vars
    .filter((v) => v.required !== false)
    .filter((v) => (values[v.name] ?? "").trim() === "")
    .map((v) => v.name);
}

export interface VariablesFormProps {
  variables: readonly PromptVariable[];
  values: Readonly<Record<string, string>>;
  onChange: (name: string, value: string) => void;
  disabled: boolean;
}

export function VariablesForm({
  variables,
  values,
  onChange,
  disabled,
}: VariablesFormProps): JSX.Element | null {
  const { t } = useTranslation();
  if (variables.length === 0) return null;
  return (
    <div data-testid="playground-vars" style={{ marginBottom: 8 }}>
      <Text
        type="secondary"
        style={{ fontSize: 12, display: "block", marginBottom: 4 }}
      >
        {t("playground.prompt_vars_label")}
      </Text>
      {variables.map((v) => (
        <div
          key={v.name}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            marginBottom: 4,
          }}
        >
          <Text style={{ width: 140, fontSize: 12 }} className="mono">
            {v.name}
          </Text>
          {v.required !== false && (
            <Text type="danger" style={{ fontSize: 11 }}>
              {t("console.vars_required_mark")}
            </Text>
          )}
          {v.description && (
            <Text type="secondary" style={{ fontSize: 11 }}>
              {v.description}
            </Text>
          )}
          <Input
            size="small"
            value={values[v.name] ?? ""}
            placeholder={v.description ?? v.name}
            aria-label={`${t("playground.prompt_vars_label")}: ${v.name}`}
            data-testid={`playground-var-${v.name}`}
            disabled={disabled}
            onChange={(e) => onChange(v.name, e.target.value)}
          />
        </div>
      ))}
    </div>
  );
}
