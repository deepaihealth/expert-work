/**
 * VariablesForm — one input row per Jinja prompt variable, lifted verbatim
 * out of ``PlaygroundTab.tsx`` (调试台重设计 PR-A Task 7; see
 * ``playground-vars`` / ``playground-var-<name>`` testids there).
 *
 * BUG-7 redesign: the original single flex row squeezed name + required
 * mark + description + input together — long descriptions crushed the
 * "必填" mark into a vertical wrap and inputs never lined up. Now a
 * two-column grid (label | input) with the required mark as a red ``*``.
 * The whole section folds behind a header (count + missing count) so a
 * long variable list doesn't bury the composer; it starts open when a
 * required value is still missing.
 *
 * 侧栏重设计(规格 C)—— the form now lives in the 380px 运行设置侧栏:
 * 必填组渲染在前,选填收进「更多 N 项(选填)」折叠(默认收起;有值时
 * 打开,预填/恢复带回的值不能被藏住);变量说明从 placeholder(输入后
 * 就看不见)挪到 label 行尾的 ``FieldHelp`` 问号 tooltip,placeholder 留空
 * (变量 spec 没有示例字段)。多列 grid 样式保留 —— 380px 下自然回单列。
 *
 * ``readOnly``/tenant-switch state is NOT read here — the parent decides
 * whether inputs are ``disabled`` and passes it down as a plain prop.
 */
import { useEffect, useState, type JSX } from "react";
import { Input, Typography } from "antd";
import { ChevronDown, ChevronRight } from "lucide-react";
import { useTranslation } from "react-i18next";

import { FieldHelp } from "../FieldHelp";

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

/** ④ 反馈 — 多列自适应 grid(宽容器两三列,380px 侧栏自然回单列)+ 40vh
 *  封顶内滚(同 PromptVariablesEditor 变量列表的内滚形态)。 */
const GRID_STYLE: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))",
  columnGap: 16,
  rowGap: 6,
  maxHeight: "40vh",
  overflowY: "auto",
};

const FOLD_BUTTON_STYLE: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  width: "100%",
  padding: "2px 0",
  background: "none",
  border: "none",
  cursor: "pointer",
  textAlign: "left",
};

export function VariablesForm({
  variables,
  values,
  onChange,
  disabled,
}: VariablesFormProps): JSX.Element | null {
  const { t } = useTranslation();
  // Open when something required is still blank, folded when the form is
  // satisfied. Filling the last field must NOT yank the section shut (no
  // auto-close), but a 0→missing transition MUST re-open it: the parent
  // resets ``values`` in place on 新建会话 / resume without remounting, and
  // a manifest edit can add a required variable — folded-with-missing means
  // send is disabled for inputs that aren't on screen.
  const hasMissing = missingRequired(variables, values).length > 0;
  const [open, setOpen] = useState(hasMissing);
  useEffect(() => {
    if (hasMissing) setOpen(true);
  }, [hasMissing]);
  // 规格 C — 选填组默认收起;某个选填项**有值**时打开(同上,只在
  // 无值→有值的转变时强制,手动收起后不反复弹开)。草稿预填 / 恢复会话
  // 带回的值都不能被折叠藏住。
  const optionalVars = variables.filter((v) => v.required === false);
  const hasOptionalValue = optionalVars.some(
    (v) => (values[v.name] ?? "").trim() !== "",
  );
  const [optionalOpen, setOptionalOpen] = useState(hasOptionalValue);
  useEffect(() => {
    if (hasOptionalValue) setOptionalOpen(true);
  }, [hasOptionalValue]);
  if (variables.length === 0) return null;

  const requiredVars = variables.filter((v) => v.required !== false);
  const missing = missingRequired(variables, values).length;
  const Chevron = open ? ChevronDown : ChevronRight;
  const OptionalChevron = optionalOpen ? ChevronDown : ChevronRight;

  const renderCell = (v: PromptVariable): JSX.Element => (
    <div
      key={v.name}
      data-testid={`playground-var-cell-${v.name}`}
      style={{
        display: "grid",
        gridTemplateColumns: "max-content minmax(0, 1fr)",
        columnGap: 12,
        alignItems: "center",
      }}
    >
      <label
        htmlFor={`playground-var-${v.name}`}
        title={v.name}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 4,
          whiteSpace: "nowrap",
        }}
      >
        <Text
          className="mono"
          // Cap the label column: an author-controlled 50-char
          // variable name must ellipsize, not starve the input.
          style={{
            fontSize: 12,
            maxWidth: 220,
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
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
        {(v.description ?? "") !== "" && (
          <FieldHelp text={v.description ?? ""} testId={`playground-var-${v.name}`} />
        )}
      </label>
      <Input
        id={`playground-var-${v.name}`}
        size="small"
        value={values[v.name] ?? ""}
        aria-label={`${t("playground.prompt_vars_label")}: ${v.name}`}
        aria-required={v.required !== false}
        data-testid={`playground-var-${v.name}`}
        disabled={disabled}
        onChange={(e) => onChange(v.name, e.target.value)}
      />
    </div>
  );

  return (
    <div data-testid="playground-vars" style={{ marginBottom: 8 }}>
      {/* No aria-label here — it would override the visible content as the
          accessible name and mute the count + missing badge for screen
          readers; the button text is the name, aria-expanded is the state. */}
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        data-testid="playground-vars-toggle"
        style={{ ...FOLD_BUTTON_STYLE, marginBottom: open ? 8 : 0 }}
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
        <>
          {requiredVars.length > 0 && (
            <div data-testid="playground-vars-grid" style={GRID_STYLE}>
              {requiredVars.map(renderCell)}
            </div>
          )}
          {optionalVars.length > 0 && (
            <>
              <button
                type="button"
                onClick={() => setOptionalOpen((o) => !o)}
                aria-expanded={optionalOpen}
                data-testid="playground-vars-optional-toggle"
                style={{
                  ...FOLD_BUTTON_STYLE,
                  marginTop: requiredVars.length > 0 ? 4 : 0,
                  marginBottom: optionalOpen ? 6 : 0,
                }}
              >
                <OptionalChevron
                  size={14}
                  strokeWidth={1.5}
                  style={{ color: "var(--ew-text-secondary)" }}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {t("console.vars_optional_fold", { count: optionalVars.length })}
                </Text>
              </button>
              {optionalOpen && (
                <div data-testid="playground-vars-grid-optional" style={GRID_STYLE}>
                  {optionalVars.map(renderCell)}
                </div>
              )}
            </>
          )}
        </>
      )}
    </div>
  );
}
