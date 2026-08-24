/**
 * VariablesForm — one input row per Jinja prompt variable, lifted verbatim
 * out of ``PlaygroundTab.tsx`` (调试台重设计 PR-A Task 7; see
 * ``playground-vars`` / ``playground-var-<name>`` testids there).
 *
 * BUG-7 redesign: the original single flex row squeezed name + required
 * mark + description + input together — long descriptions crushed the
 * "必填" mark into a vertical wrap and inputs never lined up. Now a
 * two-column grid (label | input) with the required mark as a red ``*``
 * and the description living in the input's placeholder + a hover title
 * on the label. The whole section folds behind a header (count + missing
 * count) so a long variable list doesn't bury the composer; it starts
 * open when a required value is still missing.
 *
 * ``readOnly``/tenant-switch state is NOT read here — the parent decides
 * whether inputs are ``disabled`` and passes it down as a plain prop.
 */
import { Fragment, useState, type JSX } from "react";
import { Input, Typography } from "antd";
import { ChevronDown, ChevronRight } from "lucide-react";
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
  // Initial-only: open when something required is still blank, folded when
  // the form is already satisfied. Deliberately NOT re-derived on later
  // renders — filling the last field must not yank the section shut.
  const [open, setOpen] = useState(
    () => missingRequired(variables, values).length > 0,
  );
  if (variables.length === 0) return null;

  const missing = missingRequired(variables, values).length;
  const Chevron = open ? ChevronDown : ChevronRight;

  return (
    <div data-testid="playground-vars" style={{ marginBottom: 8 }}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-label={t("console.vars_toggle")}
        data-testid="playground-vars-toggle"
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          width: "100%",
          padding: "2px 0",
          marginBottom: open ? 8 : 0,
          background: "none",
          border: "none",
          cursor: "pointer",
          textAlign: "left",
        }}
      >
        <Chevron size={14} strokeWidth={1.5} style={{ color: "var(--ew-text-secondary)" }} />
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t("playground.prompt_vars_label")} ({variables.length})
        </Text>
        {missing > 0 && (
          <Text type="danger" style={{ fontSize: 12 }} data-testid="playground-vars-missing">
            · {t("console.vars_missing_count", { count: missing })}
          </Text>
        )}
      </button>
      {open && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "max-content minmax(0, 1fr)",
            columnGap: 12,
            rowGap: 6,
            alignItems: "center",
          }}
        >
          {variables.map((v) => (
            <Fragment key={v.name}>
              <label
                htmlFor={`playground-var-${v.name}`}
                title={v.description ?? v.name}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 4,
                  whiteSpace: "nowrap",
                }}
              >
                <Text style={{ fontSize: 12 }} className="mono">
                  {v.name}
                </Text>
                {v.required !== false && (
                  <Text
                    type="danger"
                    aria-hidden
                    title={t("console.vars_required_mark")}
                    style={{ fontSize: 12, lineHeight: 1 }}
                  >
                    *
                  </Text>
                )}
              </label>
              <Input
                id={`playground-var-${v.name}`}
                size="small"
                value={values[v.name] ?? ""}
                placeholder={v.description ?? v.name}
                aria-label={`${t("playground.prompt_vars_label")}: ${v.name}`}
                aria-required={v.required !== false}
                data-testid={`playground-var-${v.name}`}
                disabled={disabled}
                onChange={(e) => onChange(v.name, e.target.value)}
              />
            </Fragment>
          ))}
        </div>
      )}
    </div>
  );
}
