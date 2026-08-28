/**
 * PlanStepList — a plan's goal line + step checklist, shared by the console
 * shell's session-level ``PlanCard`` and the per-turn ``TurnPlanCard``.
 *
 * Extracted verbatim from ``PlanCard``'s read view so the two renderers
 * cannot drift; the DOM it emits is byte-identical to what ``PlanCard``
 * inlined before (its ``plan-read-view`` testid and structure are kept by
 * the caller).
 */
import type { ReactElement } from "react";
import { Tag, Typography } from "antd";
import { Check, CircleDashed, LoaderCircle } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { PlanStepStatus, ThreadPlan } from "../../api/plan";

const { Text } = Typography;

export const PLAN_STATUS_ICON: Record<PlanStepStatus, ReactElement> = {
  pending: <CircleDashed size={14} strokeWidth={1.75} color="var(--ew-text-tertiary)" />,
  in_progress: <LoaderCircle size={14} strokeWidth={1.75} color="var(--ew-color-brand-500)" />,
  completed: <Check size={14} strokeWidth={1.75} color="var(--ew-color-success-500)" />,
};

/** Per-status counts, for the "N done · M doing · K todo" progress label. */
export function planProgress(plan: ThreadPlan): {
  completed: number;
  inProgress: number;
  pending: number;
} {
  const completed = plan.steps.filter((s) => s.status === "completed").length;
  const inProgress = plan.steps.filter((s) => s.status === "in_progress").length;
  return { completed, inProgress, pending: plan.steps.length - completed - inProgress };
}

export function PlanStepList({ plan }: { plan: ThreadPlan }): ReactElement {
  const { t } = useTranslation();
  return (
    <>
      <p style={{ margin: "0 0 10px", color: "var(--ew-text-secondary)" }}>{plan.goal}</p>
      <ol style={{ margin: 0, paddingLeft: 0, listStyle: "none" }}>
        {plan.steps.map((step) => (
          <li
            key={step.id}
            style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 0" }}
          >
            {PLAN_STATUS_ICON[step.status]}
            <span
              style={{
                color:
                  step.status === "completed"
                    ? "var(--ew-text-tertiary)"
                    : "var(--ew-text-primary)",
                textDecoration: step.status === "completed" ? "line-through" : "none",
                fontSize: 13,
              }}
            >
              {step.description}
            </span>
            {/* B-35 — delegate-marked steps run through worker sub-agents
                under plan_first; legacy payloads without the field = inline. */}
            {step.execution === "delegate" && (
              <Tag
                color="blue"
                style={{ marginInlineEnd: 0, fontSize: 11, lineHeight: "16px" }}
                data-testid="plan-step-delegate-badge"
              >
                {t("plan_panel.execution_delegate_badge")}
              </Tag>
            )}
            <Text type="secondary" style={{ fontSize: 11 }}>
              {t(`plan_panel.status_${step.status}`)}
            </Text>
          </li>
        ))}
      </ol>
    </>
  );
}
