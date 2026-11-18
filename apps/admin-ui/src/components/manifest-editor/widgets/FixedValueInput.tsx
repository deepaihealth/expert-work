/**
 * 参数绑定里「固定值」那一格(B-127 文本框 + B-318 下拉)。从 McpToolPicker 拆出来,
 * 主文件因此留在 800 行以内。
 *
 *  - 工具给这个参数声明了可选值(``paramOptionsOf``)→ 下拉:显示标题、写回值;
 *  - 没有 → 原来的文本框,空值 / 首尾空格标红(``fixedValueValid``,后端同样会拒)。
 *
 * 当前值不在可选值里(老配置、YAML 手写)时,下拉照样显示这个原值,标红并列出可选值:
 * **不清掉、不自动改**,人重新选了才写回。前端不挡保存,后端保存时会拒(B-318)。
 */
import { Input, Select, Typography } from "antd";
import { useTranslation } from "react-i18next";

import {
  formatParamOptions,
  optionLabel,
  type ParamOption,
} from "../../../utils/mcp_param_options";
import { fixedValueValid } from "./mcp_arg_bindings";

const { Text } = Typography;

export interface FixedValueInputProps {
  tool: string;
  param: string;
  value: string;
  /** 工具声明的可选值;``null`` = 没有,用文本框。 */
  options: ParamOption[] | null;
  onChange: (value: string) => void;
}

export function FixedValueInput({ tool, param, value, options, onChange }: FixedValueInputProps) {
  const { t } = useTranslation();
  const testId = `af-mcp-bind-fixed-${tool}-${param}`;
  const hintId = `af-mcp-bind-fixed-hint-${tool}-${param}`;
  const ariaLabel = t("agent_form.mcp_bind_fixed_input", { param });
  // 提示挂到控件上(aria-describedby + aria-invalid),读屏念得到「为什么标红」。
  const describedBy = (hint: string | null) => (hint === null ? undefined : hintId);

  let control;
  let hint: string | null;
  if (options === null) {
    hint = fixedValueValid(value) ? null : t("agent_form.mcp_bind_fixed_invalid");
    control = (
      <Input
        size="small"
        style={{ width: 200 }}
        data-testid={testId}
        aria-label={ariaLabel}
        aria-invalid={hint !== null}
        aria-describedby={describedBy(hint)}
        placeholder={t("agent_form.mcp_bind_fixed_placeholder")}
        // 空值 / 首尾空格后端会拒(ArgBindingSpec),当场标红而不是等保存 422。
        status={hint === null ? undefined : "error"}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  } else {
    const known = options.some((o) => o.value === value);
    hint =
      value === ""
        ? t("agent_form.mcp_bind_fixed_pick")
        : known
          ? null
          : t("agent_form.mcp_bind_fixed_not_allowed", {
              value,
              options: formatParamOptions(options),
            });
    control = (
      <Select
        size="small"
        style={{ width: 200 }}
        data-testid={testId}
        aria-label={ariaLabel}
        aria-invalid={hint !== null}
        aria-describedby={describedBy(hint)}
        placeholder={t("agent_form.mcp_bind_fixed_pick")}
        status={hint === null ? undefined : "error"}
        value={value === "" ? undefined : value}
        onChange={(next: string) => onChange(next)}
        // 不在选项里的值 antd 原样显示(不会被吞掉),标红 + 下面的提示说清楚。
        options={options.map((o) => ({ value: o.value, label: optionLabel(o) }))}
      />
    );
  }
  return (
    <>
      {control}
      {hint !== null && (
        <Text type="danger" id={hintId} data-testid={hintId} style={{ fontSize: 12 }}>
          {hint}
        </Text>
      )}
    </>
  );
}
