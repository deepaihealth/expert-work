/**
 * PlanFirstToggle — B-35 PR-2(结构化执行开关 + 联动确认 Modal).
 *
 * spec: docs/superpowers/specs/2026-08-28-plan-first-execution-design.md §3。
 *
 * 开启不直接写值:先弹确认 Modal,明示两条硬联动(workflow.type →
 * plan_execute / 动态子智能体必须开启)+ 五条建议检查项(token 成本
 * ~15×、max_iterations、运行时限、reflection 协同、触发器同样生效);
 * 确认后 ``enablePlanFirst`` **一次** onChange 写入三字段(配置历史单
 * 版本可 diff)。关闭直接写:仅删 execution_mode,**不回退**
 * workflow.type / dynamic_workers(用户开启后可能手动调过),出提示。
 *
 * 后端是双层防护的另一层:AgentSpec 校验器对不一致组合 422 硬拒
 * (绕过 UI 直改 YAML 的用户得到明确报错而非被偷改配置)。
 */
import { App, Space, Switch, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { FieldRow } from "../FieldRow";
import {
  disablePlanFirst,
  enablePlanFirst,
  readDynamicWorkersOn,
  readRunBudget,
} from "../form_model";

const { Text } = Typography;

interface PlanFirstToggleProps {
  formData: unknown;
  onChange: (data: unknown) => void;
}

export function PlanFirstToggle({ formData, onChange }: PlanFirstToggleProps) {
  const { t } = useTranslation();
  const { modal, message } = App.useApp();

  const budget = readRunBudget(formData);
  const enabled = budget.executionMode === "plan_first";

  const handleToggle = (next: boolean): void => {
    if (!next) {
      onChange(disablePlanFirst(formData));
      message.info(t("run_budget.plan_first_off_notice"));
      return;
    }
    const workflowType = budget.workflowType ?? "react";
    const workersOn = readDynamicWorkersOn(formData);
    const changes: string[] = [];
    if (workflowType !== "plan_execute") {
      changes.push(
        t("run_budget.plan_first_modal_change_wf", { from: workflowType }),
      );
    }
    if (!workersOn) {
      changes.push(t("run_budget.plan_first_modal_change_dw"));
    }
    modal.confirm({
      title: t("run_budget.plan_first_modal_title"),
      width: 560,
      okText: t("run_budget.plan_first_modal_ok"),
      onOk: () => onChange(enablePlanFirst(formData)),
      content: (
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          {changes.length > 0 && (
            <>
              <Text strong>{t("run_budget.plan_first_modal_will_change")}</Text>
              <ul style={{ margin: 0, paddingLeft: 20 }}>
                {changes.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            </>
          )}
          <Text strong>{t("run_budget.plan_first_modal_checklist")}</Text>
          <ul style={{ margin: 0, paddingLeft: 20 }}>
            <li>{t("run_budget.plan_first_modal_check_token")}</li>
            <li>{t("run_budget.plan_first_modal_check_iterations")}</li>
            <li>{t("run_budget.plan_first_modal_check_deadline")}</li>
            <li>{t("run_budget.plan_first_modal_check_reflection")}</li>
            <li>{t("run_budget.plan_first_modal_check_trigger")}</li>
          </ul>
        </Space>
      ),
    });
  };

  // FieldRow 同款一行式布局(2026-08-28 用户反馈:原独立块无 ⓘ/无「已
  // 自定义」/开关孤悬最右,和邻近字段行风格断裂)。「恢复默认」= 关闭。
  return (
    <div data-testid="plan-first-toggle">
      <FieldRow
        fieldId="workflow.execution_mode"
        label={t("run_budget.plan_first_label")}
        brief={t("run_budget.plan_first_brief")}
        help={t("run_budget.plan_first_impact")}
        isDefault={!enabled}
        onReset={() => handleToggle(false)}
        resetHint={t("run_budget.plan_first_default")}
      >
        <Switch
          checked={enabled}
          aria-label={t("run_budget.plan_first_label")}
          onChange={handleToggle}
        />
      </FieldRow>
    </div>
  );
}
