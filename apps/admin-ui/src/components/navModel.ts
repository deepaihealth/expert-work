/**
 * Navigation IA model — scope-driven, platform/tenant separated.
 *
 * See ``docs/design/admin-ui-nav-ia.md``. The sidebar is a function of
 * ``(scope, isSystemAdmin)``:
 *
 *   - a concrete tenant scope → workspace + tenant-settings groups
 *     (the tenant's agents + its settings); never the platform group.
 *   - the cross-tenant ``"*"`` scope (platform level) → platform group
 *     only (platform governance + read-only cross-tenant overview).
 *
 * Gating is nav-level noise reduction; the control-plane RBAC /
 * ``is_system_admin`` checks remain the real gate (defense in depth).
 *
 * This is the single source of truth shared by ``Sidebar``,
 * ``CommandPalette`` and the scope-switch redirect so the three never
 * drift.
 */
import { SCOPE_ALL, type TenantScopeValue } from "../tenant/TenantScopeContext";

export type NavGroup = "workspace" | "tenant-settings" | "platform" | "global";

export interface NavEntry {
  /** Menu item key (stable; used for selection + routing). */
  key: string;
  /** i18n key under ``nav.*`` for the menu label. */
  labelKey: string;
  /** Route the entry navigates to. */
  path: string;
  /** Which IA group the entry belongs to. */
  group: NavGroup;
  /** Wrap the label in the pending-approval badge (Runs/Approvals). */
  badge?: boolean;
  /** Hide from the sidebar for non-admin tenant members (tenant ``admin``
   *  role or system_admin). Nav-level noise reduction only — the page and
   *  the backend remain the real gate (defense in depth). */
  adminOnly?: boolean;
}

/** A. Workspace — the tenant's agent operations. */
export const WORKSPACE_ITEMS: readonly NavEntry[] = [
  {
    key: "agents",
    labelKey: "nav.agents",
    path: "/agents",
    group: "workspace",
  },
  {
    key: "conversations",
    labelKey: "nav.conversations",
    path: "/conversations",
    group: "workspace",
  },
  {
    key: "users",
    labelKey: "nav.users",
    path: "/users",
    group: "workspace",
    adminOnly: true,
  },
  {
    key: "approvals",
    labelKey: "nav.approvals",
    path: "/approvals",
    group: "workspace",
    badge: true,
  },
  {
    key: "curation",
    labelKey: "nav.curation",
    path: "/curation",
    group: "workspace",
  },
  { key: "eval", labelKey: "nav.eval", path: "/eval-runs", group: "workspace" },
  {
    key: "memory",
    labelKey: "nav.memory",
    path: "/memory",
    group: "workspace",
  },
  // "artifacts" left the top level (conversation-centric IA M3) — the
  // governance surface now lives on the user detail's Artifacts tab.
  {
    key: "knowledge",
    labelKey: "nav.knowledge",
    path: "/knowledge",
    group: "workspace",
  },
  {
    key: "skills",
    labelKey: "nav.skills",
    path: "/skills",
    group: "workspace",
  },
  {
    key: "agent-template-marketplace",
    labelKey: "nav.agent_template_marketplace",
    path: "/agent-template-marketplace",
    group: "workspace",
  },
  {
    key: "triggers",
    labelKey: "nav.triggers",
    path: "/triggers",
    group: "workspace",
  },
  {
    key: "webhooks",
    labelKey: "nav.webhooks",
    path: "/webhooks",
    group: "workspace",
  },
];

/** B. Tenant settings — managed for the current tenant. */
export const TENANT_SETTINGS_ITEMS: readonly NavEntry[] = [
  {
    key: "settings-members",
    labelKey: "nav.members",
    path: "/settings/members",
    group: "tenant-settings",
  },
  {
    key: "settings-credentials",
    labelKey: "nav.credentials",
    path: "/settings/credentials",
    group: "tenant-settings",
  },
  {
    key: "settings-api-keys",
    labelKey: "nav.api_keys",
    path: "/settings/api-keys",
    group: "tenant-settings",
  },
  {
    key: "settings-service-accounts",
    labelKey: "nav.service_accounts",
    path: "/settings/service-accounts",
    group: "tenant-settings",
  },
  {
    key: "settings-mcp-servers",
    labelKey: "nav.mcp_servers",
    path: "/settings/mcp-servers",
    group: "tenant-settings",
  },
  {
    key: "settings-mcp-oauth",
    labelKey: "nav.mcp_oauth",
    path: "/settings/mcp-oauth",
    group: "tenant-settings",
  },
  {
    key: "settings-audit",
    labelKey: "nav.audit",
    path: "/settings/audit",
    group: "tenant-settings",
  },
  {
    key: "settings-egress-audit",
    labelKey: "nav.egress_audit",
    path: "/settings/egress-audit",
    group: "tenant-settings",
  },
  {
    key: "settings-usage",
    labelKey: "nav.usage",
    path: "/settings/usage",
    group: "tenant-settings",
  },
  {
    key: "settings-quality",
    labelKey: "nav.quality",
    path: "/settings/quality",
    group: "tenant-settings",
  },
];

/** C. Platform governance — system_admin, platform level only. */
export const PLATFORM_ITEMS: readonly NavEntry[] = [
  {
    key: "settings-tenants",
    labelKey: "nav.tenants",
    path: "/settings/tenants",
    group: "platform",
  },
  {
    key: "settings-platform-users",
    labelKey: "nav.platform_users",
    path: "/settings/platform-users",
    group: "platform",
  },
  {
    key: "settings-platform",
    labelKey: "nav.platform_credentials",
    path: "/settings/platform",
    group: "platform",
  },
  {
    key: "settings-mcp-catalog",
    labelKey: "nav.mcp_catalog",
    path: "/settings/mcp-catalog",
    group: "platform",
  },
  {
    key: "settings-agent-templates",
    labelKey: "nav.agent_templates",
    path: "/settings/agent-templates",
    group: "platform",
  },
  {
    key: "settings-platform-skills",
    labelKey: "nav.platform_skills",
    path: "/settings/platform-skills",
    group: "platform",
  },
  {
    key: "settings-rate-card",
    labelKey: "nav.rate_card",
    path: "/settings/rate-card",
    group: "platform",
  },
  {
    key: "settings-chargeback",
    labelKey: "nav.chargeback",
    path: "/settings/billing-chargeback",
    group: "platform",
  },
  // Observability hub — platform-ops entry to Langfuse / Grafana / Tempo
  // (system_admin only; those dashboards have no per-tenant isolation).
  {
    key: "settings-observability",
    labelKey: "nav.observability",
    path: "/settings/observability",
    group: "platform",
  },
  // Keycloak console — platform-ops entry to the self-hosted IAM admin console
  // (system_admin only; Keycloak manages every tenant's users, no isolation).
  {
    key: "settings-keycloak",
    labelKey: "nav.keycloak",
    path: "/settings/keycloak",
    group: "platform",
  },
  // Read-only cross-tenant overview — reuses the members page under the
  // ``"*"`` scope (already renders read-only there). §2-C / §8 point 2.
  {
    key: "platform-members-all",
    labelKey: "nav.members_all_tenants",
    path: "/settings/members",
    group: "platform",
  },
];

/** D. Global — visible regardless of tenant/platform scope or role (the
 *  in-app handbook: usage docs for everyone, ops docs for system_admin —
 *  gated inside the page itself, not by nav scope). */
export const GLOBAL_ITEMS: readonly NavEntry[] = [
  {
    key: "handbook",
    labelKey: "nav.handbook",
    path: "/handbook",
    group: "global",
  },
];

export const ALL_NAV_ENTRIES: readonly NavEntry[] = [
  ...WORKSPACE_ITEMS,
  ...TENANT_SETTINGS_ITEMS,
  ...PLATFORM_ITEMS,
  ...GLOBAL_ITEMS,
];

/** ``true`` when the caller is operating at the platform level. */
export function isPlatformScope(scope: TenantScopeValue): boolean {
  return scope === SCOPE_ALL;
}

/**
 * Scope-specific groups only — excludes ``global`` (the handbook), which
 * is always visible and must NOT influence scope-based landing/defaults.
 * Kept separate from :func:`visibleGroups` so ``defaultPathForScope``'s
 * ``[0]`` pick below can never resolve to the handbook (§review 2026-08-11:
 * ``visibleGroups("*", false)[0]`` used to become ``"global"`` once
 * ``global`` was appended, sending ``defaultPathForScope("*", false)`` to
 * ``/handbook`` instead of its ``/agents`` fallback — a combination that's
 * genuinely reachable via a stale sessionStorage ``"*"`` scope from a prior
 * admin session, since ``TenantScopeContext``'s stored-scope read seeds
 * ``useState`` directly, bypassing the ``setScope`` gate).
 */
function scopedGroups(
  scope: TenantScopeValue,
  isSystemAdmin: boolean,
): NavGroup[] {
  return isPlatformScope(scope)
    ? isSystemAdmin
      ? ["platform"]
      : []
    : ["workspace", "tenant-settings"];
}

/**
 * Visible groups for ``(scope, isSystemAdmin)``.
 *
 *   - platform scope → platform group (with a ``isSystemAdmin`` belt-and
 *     -braces guard; the switcher only offers ``"*"`` to admins anyway).
 *   - any concrete tenant → workspace + tenant-settings.
 *   - ``global`` (the handbook) is appended in every case — it isn't
 *     scope-specific. Used for Sidebar rendering and
 *     :func:`pathAllowedForScope`; NOT used to pick the default landing
 *     path (see :func:`scopedGroups`/:func:`defaultPathForScope`).
 */
export function visibleGroups(
  scope: TenantScopeValue,
  isSystemAdmin: boolean,
): NavGroup[] {
  return [...scopedGroups(scope, isSystemAdmin), "global"];
}

/**
 * Landing route after a scope switch — the first sidebar entry of the
 * NEW scope's first visible SCOPE-SPECIFIC group (``global`` excluded —
 * see :func:`scopedGroups`). Admin and tenant IAs differ too much to
 * preserve page context across a perspective switch (product call,
 * 2026-08-02): "*" lands on tenant governance, a concrete tenant lands
 * on its agents; when no scope-specific group is visible (eg. a stale
 * "*" scope on a non-admin), the ``/agents`` fallback below applies.
 */
export function defaultPathForScope(
  scope: TenantScopeValue,
  isSystemAdmin: boolean,
): string {
  const group = scopedGroups(scope, isSystemAdmin)[0];
  const first = ALL_NAV_ENTRIES.find((e) => e.group === group);
  return first?.path ?? "/agents";
}

/** Whether ``path`` belongs to a nav group visible under ``(scope,
 *  isSystemAdmin)``. A path can live in several groups (``/settings/members``
 *  is both a tenant-settings entry and the platform "all tenants" overview) —
 *  it is allowed if ANY of them is visible. Paths outside the nav model
 *  (login, OAuth callbacks…) are always allowed. */
export function pathAllowedForScope(
  path: string,
  scope: TenantScopeValue,
  isSystemAdmin: boolean,
): boolean {
  const groups = visibleGroups(scope, isSystemAdmin);
  const matches = ALL_NAV_ENTRIES.filter(
    (e) => path === e.path || path.startsWith(`${e.path}/`),
  );
  if (matches.length === 0) return true;
  return matches.some((e) => groups.includes(e.group));
}

/** Which group a route path belongs to (longest-prefix wins so eg.
 *  ``/settings/platform-users`` doesn't match ``/settings/platform``
 *  before the more specific entry). */
export function groupForPath(path: string): NavGroup | null {
  const match = [...ALL_NAV_ENTRIES]
    .filter((e) => path === e.path || path.startsWith(`${e.path}/`))
    .sort((a, b) => b.path.length - a.path.length)[0];
  return match?.group ?? null;
}
