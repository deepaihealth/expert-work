/**
 * PlanCard — the console shell's task card (Stream CM-8 UI channel).
 *
 * Presentation only: the caller (``usePlanCard``) owns the plan's
 * three-source precedence; this renders whatever ``plan`` it is given,
 * plus a local edit-mode draft built on ``PlanEditForm`` (originally
 * shared with ``PlanPanel.tsx``; PR-B retired that file, so this is now
 * ``PlanEditForm``'s only consumer). No plan yet → render nothing, so the
 * shell doesn't reserve space for an empty card.
 *
 * The collapse toggle persists per-browser under
 * ``expert_work.console.planCollapsed`` ("1" / "0") — the same
 * per-browser localStorage convention the console's other panel toggles
 * use.
 */
import { useCallback, useState, type ReactElement } from "react";
import { App, Button, Card, Space, Tooltip, Typography } from "antd";
import { Check, ChevronDown, ChevronRight, Pencil, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../../api/client";
import type { ThreadPlan } from "../../api/plan";
import { PlanEditForm, planDraftValid } from "./PlanEditForm";
import { PlanStepList, planProgress } from "./PlanStepList";

const { Text } = Typography;

const COLLAPSED_KEY = "expert_work.console.planCollapsed";

export interface PlanCardProps {
  plan: ThreadPlan | null;
  loaded: boolean;
  /** run in progress → editing is locked + a tooltip explains why. */
  running: boolean;
  /** Conversation history / read-only views — no edit affordance. */
  readOnly?: boolean;
  onSave?: (next: ThreadPlan) => Promise<void>;
}

export function PlanCard(props: PlanCardProps): ReactElement | null {
  const { plan, running, readOnly = false, onSave } = props;
  const { t } = useTranslation();
  const { message } = App.useApp();

  const [collapsed, setCollapsed] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    return window.localStorage.getItem(COLLAPSED_KEY) === "1";
  });
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<ThreadPlan | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const setCollapsedPersistent = useCallback((next: boolean) => {
    setCollapsed(next);
    if (typeof window !== "undefined") {
      window.localStorage.setItem(COLLAPSED_KEY, next ? "1" : "0");
    }
  }, []);

  const startEdit = useCallback(() => {
    setDraft(
      plan
        ? { goal: plan.goal, steps: plan.steps.map((s) => ({ ...s })) }
        : { goal: "", steps: [] },
    );
    setEditing(true);
  }, [plan]);

  const cancelEdit = useCallback(() => {
    setDraft(null);
    setEditing(false);
  }, []);

  const save = useCallback(async () => {
    if (draft === null || !planDraftValid(draft) || onSave === undefined) return;
    setSubmitting(true);
    try {
      await onSave({
        goal: draft.goal.trim(),
        steps: draft.steps.map((s, idx) => ({
          id: s.id || String(idx + 1),
          description: s.description.trim(),
          status: s.status,
        })),
      });
      setEditing(false);
      setDraft(null);
    } catch (err) {
      const msg = err instanceof ApiError ? `${err.code}: ${err.message}` : String(err);
      message.error(msg);
    } finally {
      setSubmitting(false);
    }
  }, [draft, onSave, message]);

  if (plan === null) return null;

  const { completed, inProgress, pending } = planProgress(plan);

  const editButton = (
    <Button
      size="small"
      icon={<Pencil size={12} strokeWidth={1.75} />}
      onClick={startEdit}
      disabled={running}
      data-testid="plan-edit"
    >
      {t("plan_panel.edit")}
    </Button>
  );

  return (
    <Card
      data-testid="console-plan-card"
      size="small"
      title={
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
            onClick={() => setCollapsedPersistent(!collapsed)}
            data-testid="console-plan-toggle"
            aria-label={t("console.plan_toggle")}
            aria-expanded={!collapsed}
            aria-controls="console-plan-body"
          />
          <Text strong>{t("console.plan_title")}</Text>
          <Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
            {t("console.plan_progress", { done: completed, doing: inProgress, todo: pending })}
          </Text>
        </Space>
      }
      extra={
        readOnly ? null : editing ? (
          <Space size={8}>
            <Button
              size="small"
              icon={<X size={12} strokeWidth={1.75} />}
              onClick={cancelEdit}
              disabled={submitting}
              data-testid="plan-cancel-edit"
            >
              {t("plan_panel.cancel")}
            </Button>
            <Button
              size="small"
              type="primary"
              icon={<Check size={12} strokeWidth={1.75} />}
              loading={submitting}
              disabled={!planDraftValid(draft)}
              onClick={() => void save()}
              data-testid="plan-save"
            >
              {t("plan_panel.save")}
            </Button>
          </Space>
        ) : running ? (
          <Tooltip title={t("plan_panel.locked_while_running")}>
            <span>{editButton}</span>
          </Tooltip>
        ) : (
          editButton
        )
      }
    >
      {!collapsed && (
        <div id="console-plan-body">
          {editing && draft !== null ? (
            <PlanEditForm draft={draft} onChange={setDraft} />
          ) : (
            <div data-testid="plan-read-view">
              <PlanStepList plan={plan} />
            </div>
          )}
        </div>
      )}
    </Card>
  );
}
