/**
 * TurnPlanCard — the plan snapshot a single turn produced, rendered inside
 * that turn (BUG-13 revision).
 *
 * Why per-turn rather than one card for the thread: ``plan`` is a
 * thread-level accumulating state, so a single card can only ever show the
 * LAST state. Hoisting it above the transcript put the conversation's final
 * checklist on top of its first turn — the reader sees a plan that did not
 * exist yet. Each turn's own event stream carries the snapshot it ended
 * with (``reducePlan`` over that turn's events), so the plan lives where it
 * was produced and the timeline stays honest.
 *
 * Deliberately NOT the session-level ``PlanCard``: that one owns edit mode
 * and a per-browser collapse flag under one localStorage key, and stamps a
 * fixed ``console-plan-card`` testid plus a fixed ``console-plan-body`` DOM
 * id — all three break once several instances share a page. This is the
 * read-only sibling; both render steps through ``PlanStepList`` so the two
 * cannot drift.
 */
import { useState, type ReactElement } from "react";
import { Button, Card, Space, Typography } from "antd";
import { ChevronDown, ChevronRight } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { ThreadPlan } from "../../api/plan";
import { PlanStepList, planProgress } from "./PlanStepList";

const { Text } = Typography;

export interface TurnPlanCardProps {
  /** ``null`` when this turn emitted no plan frame → renders nothing. */
  plan: ThreadPlan | null;
}

export function TurnPlanCard({ plan }: TurnPlanCardProps): ReactElement | null {
  const { t } = useTranslation();
  // Local, non-persisted: collapsing one turn's plan must not collapse
  // every other turn's (and must not outlive the page like the session
  // card's localStorage flag does).
  const [collapsed, setCollapsed] = useState(false);

  if (plan === null) return null;

  const { completed, inProgress, pending } = planProgress(plan);

  return (
    <Card data-testid="turn-plan-card" size="small">
      <Space size={8}>
        <Button
          type="text"
          size="small"
          icon={
            collapsed ? (
              <ChevronRight size={14} strokeWidth={1.75} />
            ) : (
              <ChevronDown size={14} strokeWidth={1.75} />
            )
          }
          onClick={() => setCollapsed(!collapsed)}
          data-testid="turn-plan-toggle"
          aria-label={t("console.plan_toggle")}
          aria-expanded={!collapsed}
        />
        <Text strong style={{ fontSize: 13 }}>
          {t("console.plan_title")}
        </Text>
        <Text
          type="secondary"
          style={{ fontSize: 12, fontWeight: 400 }}
          data-testid="turn-plan-progress"
        >
          {t("console.plan_progress", { done: completed, doing: inProgress, todo: pending })}
        </Text>
      </Space>
      {!collapsed && (
        <div style={{ marginTop: 8 }}>
          <PlanStepList plan={plan} />
        </div>
      )}
    </Card>
  );
}
