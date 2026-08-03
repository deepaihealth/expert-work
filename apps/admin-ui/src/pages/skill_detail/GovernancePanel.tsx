/**
 * Skill governance panel — Stream SE (SE-8-4).
 *
 * Surfaces the self-evolution governance for one skill on the detail page:
 * visibility / owner / fork lineage, plus the agent_private→tenant promote
 * flow (propose → review → approve/reject). Eval-evidence + lineage graph +
 * kill-switch live elsewhere (SE-8-5). ``archive`` is the existing status
 * select on the page, not duplicated here.
 */
import { useCallback, useEffect, useState } from "react";
import { App, Button, Card, Space, Tag, Typography } from "antd";
import { Check, GitFork, Lock, Send, Users, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../../api/client";
import type { SkillRecord } from "../../api/skills";
import {
  approvePromote,
  listPromoteRequests,
  rejectPromote,
  requestPromote,
  type PromoteRequest,
} from "../../api/skill-evolution";
import { ReadonlyTooltip } from "../../components/ReadonlyTooltip";

const { Text } = Typography;

interface GovernancePanelProps {
  skill: SkillRecord;
  isAdmin: boolean;
  /** Refetch the parent skill (visibility flips on approve). */
  onChanged: () => void | Promise<void>;
  /** Cross-tenant W4(D2)— 权威读口径:URL ``?tenant_id=`` 原样透传优先;
   *  无 URL 参数时取 ambient scope("*" 折叠成 undefined),由 SkillDetail
   *  统一下传。 */
  readScope: string | undefined;
  /** Cross-tenant W4(D2)— 只读态(切入态 ∪ "*" 聚合深链外租户读),由
   *  SkillDetail 统一判定下传;写控件一律置灰。 */
  readonly: boolean;
}

function errMessage(err: unknown): string {
  if (err instanceof ApiError) return `${err.code}: ${err.message}`;
  if (err instanceof Error) return err.message;
  return "failed";
}

export function GovernancePanel({
  skill,
  isAdmin,
  onChanged,
  readScope,
  readonly,
}: GovernancePanelProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  // Cross-tenant W4(D2)— pending 列表读带 readScope 透传(切入态/聚合跳转
  // 下读对租户)。提议/批准/驳回是写操作,promote 写链路仍不带 scope(会拿
  // 目标租户的 skill.id 打归属租户端点)→ 切入态与 "*" 聚合深链(外租户读)
  // 一律由页面下传的 readonly 置灰。
  const [pending, setPending] = useState<PromoteRequest | null>(null);
  const [busy, setBusy] = useState(false);

  const loadPending = useCallback(async () => {
    try {
      const list = await listPromoteRequests({ status: "pending", tenantScope: readScope });
      setPending(list.items.find((r) => r.skill_id === skill.id) ?? null);
    } catch {
      // Best-effort: the panel still renders its static facts without the queue.
      setPending(null);
    }
  }, [skill.id, readScope]);

  useEffect(() => {
    void loadPending();
  }, [loadPending]);

  const onPropose = useCallback(async () => {
    if (skill.latest_version === null || skill.latest_version < 1) {
      message.error(t("skill_evolution.no_version_to_propose"));
      return;
    }
    setBusy(true);
    try {
      await requestPromote(skill.id, { skill_version: skill.latest_version });
      message.success(t("skill_evolution.proposed_toast"));
      await loadPending();
    } catch (err) {
      message.error(errMessage(err));
    } finally {
      setBusy(false);
    }
  }, [skill.id, skill.latest_version, message, t, loadPending]);

  const onDecide = useCallback(
    async (approve: boolean) => {
      if (pending === null) return;
      setBusy(true);
      try {
        if (approve) {
          await approvePromote(pending.id);
          message.success(t("skill_evolution.approved_toast"));
        } else {
          await rejectPromote(pending.id);
          message.success(t("skill_evolution.rejected_toast"));
        }
        await loadPending();
        await onChanged();
      } catch (err) {
        message.error(errMessage(err));
      } finally {
        setBusy(false);
      }
    },
    [pending, message, t, loadPending, onChanged],
  );

  const visibility = skill.visibility ?? "tenant";

  return (
    <Card
      size="small"
      title={t("skill_evolution.governance_title")}
      style={{ marginBottom: 16 }}
      data-testid="skill-governance-panel"
    >
      <Space direction="vertical" size={10} style={{ width: "100%" }}>
        <Space size={8} wrap>
          {visibility === "agent_private" ? (
            <Tag icon={<Lock size={11} strokeWidth={1.75} />} data-testid="skill-visibility-badge">
              {t("skill_evolution.visibility_agent_private")}
            </Tag>
          ) : (
            <Tag
              icon={<Users size={11} strokeWidth={1.75} />}
              color="cyan"
              data-testid="skill-visibility-badge"
            >
              {t("skill_evolution.visibility_tenant")}
            </Tag>
          )}
          {skill.created_by_agent_name != null && skill.created_by_agent_name !== "" && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t("skill_evolution.owner")}: {skill.created_by_agent_name}
            </Text>
          )}
          {skill.forked_from != null && (
            <Tag icon={<GitFork size={11} strokeWidth={1.75} />} bordered={false}>
              {t("skill_evolution.forked_from")}
            </Tag>
          )}
        </Space>

        {visibility === "agent_private" && pending === null && (
          <ReadonlyTooltip on={readonly}>
            <Button
              icon={<Send size={13} strokeWidth={1.75} />}
              loading={busy}
              disabled={readonly}
              onClick={onPropose}
              data-testid="skill-propose-button"
            >
              {t("skill_evolution.propose_to_tenant")}
            </Button>
          </ReadonlyTooltip>
        )}

        {pending !== null && (
          <Space size={8} wrap data-testid="skill-pending-promotion">
            <Tag color="gold">{t("skill_evolution.pending_tenant_promotion")}</Tag>
            {isAdmin && (
              <>
                <ReadonlyTooltip on={readonly}>
                  <Button
                    size="small"
                    type="primary"
                    icon={<Check size={13} strokeWidth={2} />}
                    loading={busy}
                    disabled={readonly}
                    onClick={() => void onDecide(true)}
                    data-testid="skill-approve-button"
                  >
                    {t("skill_evolution.approve")}
                  </Button>
                </ReadonlyTooltip>
                <ReadonlyTooltip on={readonly}>
                  <Button
                    size="small"
                    danger
                    icon={<X size={13} strokeWidth={2} />}
                    loading={busy}
                    disabled={readonly}
                    onClick={() => void onDecide(false)}
                    data-testid="skill-reject-button"
                  >
                    {t("skill_evolution.reject")}
                  </Button>
                </ReadonlyTooltip>
              </>
            )}
          </Space>
        )}
      </Space>
    </Card>
  );
}
