/**
 * TenantSwitcher — Stream H.1b (Stream N integration) + PR 2a (i18n).
 *
 * Topbar dropdown that drives :class:`TenantScopeContext`:
 *
 *   - tenant_admin: only their home tenant is shown (the switcher is
 *     effectively a read-only label; we still render the dropdown for
 *     visual consistency with system_admin).
 *   - system_admin: home tenant + "All tenants" (PR 2b of H.1b wires a
 *     server-side tenant list for "Switch to specific tenant…").
 *
 * The control-plane enforces this server-side via ``ensure_tenant_scope``
 * — selecting "All tenants" merely puts ``"*"`` on the wire; an
 * impersonated tenant_admin still gets 403 ``CROSS_TENANT_FORBIDDEN``.
 */
import { Select, Tag } from "antd";
import { Globe2, Building2 } from "lucide-react";
import { startTransition, useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { listTenants, type TenantSummary } from "../api/tenants";
import { useAuth } from "../auth/AuthContext";
import {
  SCOPE_ALL,
  SCOPE_HOME,
  useTenantScope,
  type TenantScopeValue,
} from "../tenant/TenantScopeContext";
import { defaultPathForScope } from "./navModel";

interface ScopeOption {
  value: TenantScopeValue;
  label: string;
  hint?: string;
}

export function TenantSwitcher() {
  const { t } = useTranslation();
  const { identity } = useAuth();
  const { scope, setScope } = useTenantScope();
  const navigate = useNavigate();

  const isSystemAdmin = identity?.isSystemAdmin ?? false;

  // Switching scope is a perspective switch — the current page's context
  // rarely survives it (a tenant's agent detail 404s under "*"; platform
  // governance pages don't exist in a tenant's IA). Land on the new
  // perspective's first sidebar entry instead of staying put.
  const handleChange = useCallback(
    (next: TenantScopeValue) => {
      if (next === scope) return;
      // One commit for the pair — see the comment in
      // ``SettingsTenants.manage``. Here the navigation target happens to
      // equal what ``useScopeRedirect`` would bounce to, so a split commit
      // merely double-navigates instead of landing somewhere wrong; that is
      // luck, not design, and it is why no test caught the same shape here.
      startTransition(() => {
        setScope(next);
        navigate(defaultPathForScope(next, isSystemAdmin));
      });
    },
    [scope, setScope, navigate, isSystemAdmin],
  );

  const [tenants, setTenants] = useState<TenantSummary[]>([]);

  useEffect(() => {
    if (!isSystemAdmin) return;
    let alive = true;
    listTenants().then(
      (rows) => {
        // Drop the synthetic platform tenant — it has no workspace and the
        // platform level is the ``*`` scope, not a switchable peer row.
        if (alive) setTenants(rows.filter((r) => !r.is_platform));
      },
      () => {
        /* non-fatal: switcher still offers home + all */
      },
    );
    return () => {
      alive = false;
    };
  }, [isSystemAdmin]);

  const homeLabel = identity?.homeTenantId
    ? `${t("tenant.home_label_prefix")} · ${identity.homeTenantId.slice(0, 8)}…`
    : t("tenant.home_tenant");

  // Hide the home tenant when it's the synthetic platform tenant: it has no
  // workspace, and the platform level is the "*" scope, not a peer row
  // (Stream ACCT). A dual-role admin homes to a real tenant ⇒ home stays.
  const hideHome = identity?.homeIsPlatform ?? false;

  const options: ScopeOption[] = hideHome
    ? []
    : [
        {
          value: SCOPE_HOME,
          label: homeLabel,
          hint: t("tenant.your_tenant"),
        },
      ];
  if (isSystemAdmin) {
    options.push({
      value: SCOPE_ALL,
      label: t("tenant.all_tenants"),
      hint: t("tenant.system_admin_hint"),
    });
    for (const tnt of tenants) {
      if (tnt.tenant_id === identity?.homeTenantId) continue;
      options.push({
        value: tnt.tenant_id,
        label: tnt.display_name,
        hint: `${tnt.tenant_id.slice(0, 8)}…`,
      });
    }
  }

  return (
    <Select<TenantScopeValue>
      data-testid="tenant-switcher"
      aria-label={t("tenant.home_tenant")}
      value={scope}
      onChange={handleChange}
      style={{ minWidth: 220 }}
      size="middle"
      labelInValue={false}
      optionLabelProp="label"
      disabled={options.length === 1}
      options={options.map((o) => ({
        value: o.value,
        label: (
          <span
            data-testid={`tenant-switcher-option-${o.value}`}
            style={{ display: "inline-flex", alignItems: "center", gap: 8 }}
          >
            {o.value === SCOPE_ALL ? <Globe2 size={14} /> : <Building2 size={14} />}
            <span>{o.label}</span>
            {o.value === SCOPE_ALL && (
              <Tag color="purple" style={{ marginLeft: 4 }}>
                {t("tenant.cross_tag")}
              </Tag>
            )}
            {o.hint && (
              <span style={{ color: "var(--ew-text-tertiary)", fontSize: 11 }}>
                {o.hint}
              </span>
            )}
          </span>
        ),
      }))}
    />
  );
}
