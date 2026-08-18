/**
 * Plan panel — Stream CM-8 (the plan UI channel, Mini-ADR CM-I5).
 *
 * Read view: the thread's current ``AgentState.plan`` (goal + ordered
 * steps with ○/◐/✓ status — colour + shape + text, per the design
 * philosophy's "黑白可读" rule) refreshed by the parent's poll tick.
 *
 * Edit mode: a structured form (goal input + per-step description /
 * status / add / remove), NOT raw JSON — the plan's shape is small and
 * known, so a form beats an editor. The Edit button is disabled while
 * the run is live (the backend enforces the same with 409 — CM-I3);
 * a tooltip explains why instead of a dead button.
 */
import { useCallback, useEffect, useMemo, useState, type ReactElement } from "react";
import { App, Button, Card, Empty, Space, Tooltip, Typography } from "antd";
import { Check, CircleDashed, ListChecks, LoaderCircle, Pencil, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../../api/client";
import { getThreadPlan, updateThreadPlan, type PlanStepStatus, type ThreadPlan } from "../../api/plan";
import { PlanEditForm, planDraftValid } from "../../components/console/PlanEditForm";

const { Text } = Typography;

const STATUS_ICON: Record<PlanStepStatus, ReactElement> = {
  pending: <CircleDashed size={14} strokeWidth={1.75} color="var(--ew-text-tertiary)" />,
  in_progress: <LoaderCircle size={14} strokeWidth={1.75} color="var(--ew-color-brand-500)" />,
  completed: <Check size={14} strokeWidth={1.75} color="var(--ew-color-success-500)" />,
};

interface PlanPanelProps {
  threadId: string;
  /** Latest run status — gates the Edit affordance while live. */
  runStatus: string | null;
  /** Parent poll tick — bump to trigger a silent re-fetch. */
  pollTick?: number;
  /** DI seams (Storybook / tests) — default to the real SDK. */
  fetchPlan?: typeof getThreadPlan;
  savePlan?: typeof updateThreadPlan;
}

export function PlanPanel({
  threadId,
  runStatus,
  pollTick = 0,
  fetchPlan = getThreadPlan,
  savePlan = updateThreadPlan,
}: PlanPanelProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();

  const [plan, setPlan] = useState<ThreadPlan | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<ThreadPlan | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const live = runStatus === "running" || runStatus === "pending" || runStatus === "queued";

  const refresh = useCallback(async () => {
    try {
      setPlan(await fetchPlan(threadId));
    } catch {
      // Best-effort panel — the run page still works without a plan.
    } finally {
      setLoaded(true);
    }
  }, [threadId, fetchPlan]);

  useEffect(() => {
    if (!editing) void refresh();
    // pollTick re-triggers the silent refresh on the parent's cadence.
  }, [refresh, pollTick, editing]);

  const completed = useMemo(
    () => (plan ? plan.steps.filter((s) => s.status === "completed").length : 0),
    [plan],
  );

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
    if (draft === null || !planDraftValid(draft)) return;
    setSubmitting(true);
    try {
      const stored = await savePlan(threadId, {
        goal: draft.goal.trim(),
        steps: draft.steps.map((s, idx) => ({
          id: s.id || String(idx + 1),
          description: s.description.trim(),
          status: s.status,
        })),
      });
      setPlan(stored);
      setEditing(false);
      setDraft(null);
      message.success(t("plan_panel.saved"));
    } catch (err) {
      const msg = err instanceof ApiError ? `${err.code}: ${err.message}` : String(err);
      message.error(msg);
    } finally {
      setSubmitting(false);
    }
  }, [draft, threadId, savePlan, message, t]);

  const editButton = (
    <Button
      size="small"
      icon={<Pencil size={12} strokeWidth={1.75} />}
      onClick={startEdit}
      disabled={live}
      data-testid="plan-edit"
    >
      {t("plan_panel.edit")}
    </Button>
  );

  return (
    <Card
      data-testid="plan-panel"
      size="small"
      style={{ marginTop: 16 }}
      title={
        <Space size={8}>
          <ListChecks size={16} strokeWidth={1.75} />
          <Text strong>{t("plan_panel.title")}</Text>
          {plan !== null && (
            <Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
              {t("plan_panel.progress", { done: completed, total: plan.steps.length })}
            </Text>
          )}
        </Space>
      }
      extra={
        editing ? (
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
        ) : live ? (
          <Tooltip title={t("plan_panel.locked_while_running")}>
            <span>{editButton}</span>
          </Tooltip>
        ) : (
          editButton
        )
      }
    >
      {editing && draft !== null ? (
        <PlanEditForm draft={draft} onChange={setDraft} />
      ) : plan === null ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={loaded ? t("plan_panel.no_plan") : t("common.loading")}
          data-testid="plan-empty"
        />
      ) : (
        <div data-testid="plan-read-view">
          <p style={{ margin: "0 0 10px", color: "var(--ew-text-secondary)" }}>{plan.goal}</p>
          <ol style={{ margin: 0, paddingLeft: 0, listStyle: "none" }}>
            {plan.steps.map((step) => (
              <li
                key={step.id}
                style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 0" }}
              >
                {STATUS_ICON[step.status]}
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
                <Text type="secondary" style={{ fontSize: 11 }}>
                  {t(`plan_panel.status_${step.status}`)}
                </Text>
              </li>
            ))}
          </ol>
        </div>
      )}
    </Card>
  );
}
