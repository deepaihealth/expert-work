/**
 * PlanEditForm — the plan's structured edit form (goal input + per-step
 * description / status / add / remove), extracted from PlanPanel.tsx so
 * the console shell's PlanCard (Task 8) can share it. A form beats raw
 * JSON here — the plan's shape is small and known (Stream CM-8).
 *
 * Purely controlled, and holds no buttons of its own — callers own
 * Save / Cancel / disabled-while-running around it.
 */
import { useCallback } from "react";
import { Button, Input, Select, Space } from "antd";
import { Plus, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { PlanStep, PlanStepStatus, ThreadPlan } from "../../api/plan";

export interface PlanEditFormProps {
  draft: ThreadPlan;
  onChange: (next: ThreadPlan) => void;
}

/** Save is blocked until the goal and every step description are non-blank. */
export function planDraftValid(d: ThreadPlan | null): boolean {
  return (
    d !== null &&
    d.goal.trim().length > 0 &&
    d.steps.length > 0 &&
    d.steps.every((s) => s.description.trim().length > 0)
  );
}

export function PlanEditForm({ draft, onChange }: PlanEditFormProps) {
  const { t } = useTranslation();

  const patchStep = useCallback(
    (idx: number, patch: Partial<PlanStep>) => {
      onChange({
        ...draft,
        steps: draft.steps.map((s, i) => (i === idx ? { ...s, ...patch } : s)),
      });
    },
    [draft, onChange],
  );

  return (
    <div data-testid="plan-edit-form">
      <Input
        value={draft.goal}
        onChange={(e) => onChange({ ...draft, goal: e.target.value })}
        placeholder={t("plan_panel.goal_placeholder")}
        style={{ marginBottom: 12 }}
        data-testid="plan-goal-input"
      />
      {draft.steps.map((step, idx) => (
        <Space.Compact key={idx} block style={{ marginBottom: 8 }}>
          <Input
            value={step.description}
            onChange={(e) => patchStep(idx, { description: e.target.value })}
            placeholder={t("plan_panel.step_placeholder")}
            data-testid={`plan-step-input-${idx}`}
          />
          <Select<PlanStepStatus>
            value={step.status}
            onChange={(status) => patchStep(idx, { status })}
            style={{ width: 150 }}
            options={[
              { value: "pending", label: t("plan_panel.status_pending") },
              { value: "in_progress", label: t("plan_panel.status_in_progress") },
              { value: "completed", label: t("plan_panel.status_completed") },
            ]}
            data-testid={`plan-step-status-${idx}`}
          />
          <Button
            icon={<Trash2 size={13} strokeWidth={1.75} />}
            onClick={() => onChange({ ...draft, steps: draft.steps.filter((_, i) => i !== idx) })}
            aria-label={t("plan_panel.remove_step")}
            data-testid={`plan-step-remove-${idx}`}
          />
        </Space.Compact>
      ))}
      <Button
        size="small"
        icon={<Plus size={12} strokeWidth={1.75} />}
        onClick={() =>
          onChange({
            ...draft,
            steps: [
              ...draft.steps,
              { id: String(draft.steps.length + 1), description: "", status: "pending" },
            ],
          })
        }
        data-testid="plan-add-step"
      >
        {t("plan_panel.add_step")}
      </Button>
    </div>
  );
}
