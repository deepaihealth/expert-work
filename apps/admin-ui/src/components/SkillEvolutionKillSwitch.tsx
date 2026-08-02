/**
 * Skill-evolution kill-switch control — Stream SE (SE-8-5).
 *
 * Compact header control for the persistent emergency stop on the auto-promote
 * pipeline. A tenant admin toggles their tenant scope; a system_admin also gets
 * the global scope. Shows the effective halted state (tenant OR global). Engage
 * is confirmed (it degrades the whole auto channel to human review).
 */
import { useCallback, useEffect, useState } from "react";
import { App, Space, Switch, Tag, Tooltip, Typography } from "antd";
import { ShieldAlert, ShieldCheck } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  engageKillSwitch,
  getKillSwitch,
  releaseKillSwitch,
  type KillSwitchScope,
  type KillSwitchState,
} from "../api/skill-evolution";
import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { concreteTenantScope, useTenantScope } from "../tenant/TenantScopeContext";
import { useIsTenantSwitched } from "../tenant/useIsTenantSwitched";
import { ReadonlyTooltip } from "./ReadonlyTooltip";

const { Text } = Typography;

export function SkillEvolutionKillSwitch() {
  const { t } = useTranslation();
  const { message, modal } = App.useApp();
  const { identity } = useAuth();
  const isSystemAdmin = identity?.isSystemAdmin ?? false;
  const { apiTenantScope } = useTenantScope();
  // Cross-tenant W3 — 切入态只读:熔断启停是写操作。且读写租户不对称
  // (getKillSwitch 读目标租户、engage/release 只写归属租户),切入态下
  // 一点就是「看 A 租户的状态、改自家的开关」——必须置灰。读侧照常。
  const isTenantSwitched = useIsTenantSwitched();

  const [state, setState] = useState<KillSwitchState | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      // "*" maps to undefined: the endpoint types tenant_id as ``UUID | None``
      // and 422s a literal "*", which would hide the whole control (state
      // stays null) in the system_admin aggregate view.
      setState(await getKillSwitch(concreteTenantScope(apiTenantScope)));
    } catch {
      setState(null);
    }
  }, [apiTenantScope]);

  useEffect(() => {
    void load();
  }, [load]);

  const apply = useCallback(
    async (scope: KillSwitchScope, engage: boolean) => {
      setBusy(true);
      try {
        if (engage) {
          await engageKillSwitch({ scope });
          message.success(t("skill_evolution.kill_switch_engaged_toast"));
        } else {
          await releaseKillSwitch({ scope });
          message.success(t("skill_evolution.kill_switch_released_toast"));
        }
        await load();
      } catch (err) {
        message.error(err instanceof ApiError ? `${err.code}: ${err.message}` : "failed");
      } finally {
        setBusy(false);
      }
    },
    [message, t, load],
  );

  const onToggle = useCallback(
    (scope: KillSwitchScope, next: boolean) => {
      if (next) {
        modal.confirm({
          title: t("skill_evolution.kill_switch_confirm_title"),
          content: t("skill_evolution.kill_switch_confirm_body"),
          okButtonProps: { danger: true },
          okText: t("skill_evolution.kill_switch_engage"),
          onOk: () => apply(scope, true),
        });
      } else {
        void apply(scope, false);
      }
    },
    [modal, t, apply],
  );

  if (state === null) return null;

  const effective = state.effective_halted;

  return (
    <Space size={8} data-testid="skill-kill-switch">
      <Tooltip title={t("skill_evolution.kill_switch_hint")}>
        {effective ? (
          <Tag
            color="error"
            icon={<ShieldAlert size={11} strokeWidth={1.75} />}
            data-testid="skill-kill-switch-status"
          >
            {t("skill_evolution.kill_switch_halted")}
          </Tag>
        ) : (
          <Tag
            color="success"
            icon={<ShieldCheck size={11} strokeWidth={1.75} />}
            data-testid="skill-kill-switch-status"
          >
            {t("skill_evolution.kill_switch_active")}
          </Tag>
        )}
      </Tooltip>
      <Text type="secondary" style={{ fontSize: 11 }}>
        {t("skill_evolution.kill_switch_tenant_label")}
      </Text>
      <ReadonlyTooltip on={isTenantSwitched}>
        <Switch
          size="small"
          checked={state.tenant?.engaged ?? false}
          loading={busy}
          disabled={isTenantSwitched}
          onChange={(next) => onToggle("tenant", next)}
          aria-label={t("skill_evolution.kill_switch_tenant_aria")}
          data-testid="skill-kill-switch-tenant"
        />
      </ReadonlyTooltip>
      {isSystemAdmin && (
        <>
          <Text type="secondary" style={{ fontSize: 11 }}>
            {t("skill_evolution.kill_switch_global_label")}
          </Text>
          <ReadonlyTooltip on={isTenantSwitched}>
            <Switch
              size="small"
              checked={state.global?.engaged ?? false}
              loading={busy}
              disabled={isTenantSwitched}
              onChange={(next) => onToggle("global", next)}
              aria-label={t("skill_evolution.kill_switch_global_aria")}
              data-testid="skill-kill-switch-global"
            />
          </ReadonlyTooltip>
        </>
      )}
    </Space>
  );
}
