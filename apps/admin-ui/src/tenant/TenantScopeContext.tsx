/**
 * Tenant scope context — Stream H.1b (Stream N integration).
 *
 * The current tenant scope is one of:
 *
 *   - ``home`` — caller's home tenant (default for everyone).
 *   - ``"*"`` — cross-tenant aggregate (system_admin only; the
 *     ``ensure_tenant_scope`` middleware on the control-plane rejects
 *     anything else with 403 ``CROSS_TENANT_FORBIDDEN``).
 *   - specific UUID — system_admin "switch to tenant X" view; also
 *     audited server-side.
 *
 * Pages call :func:`useTenantScope` and thread ``scope`` through the
 * SDK via :func:`withTenantScope` from ``api/client``. Persisting the
 * scope to sessionStorage keeps the choice across reloads inside one
 * tab but isolates from other admin sessions.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { useAuth, type AuthIdentity } from "../auth/AuthContext";

export const SCOPE_HOME = "home" as const;
export const SCOPE_ALL = "*" as const;

/** ``"home"`` ⇒ omit ``tenant_id`` on the wire; ``"*"`` ⇒ cross-tenant;
 *  any UUID ⇒ specific tenant switch. */
export type TenantScopeValue = typeof SCOPE_HOME | typeof SCOPE_ALL | string;

/** Collapse the ``"*"`` aggregate to ``undefined`` for endpoints whose
 *  ``tenant_id`` is typed ``UUID | None`` server-side — they 422 a literal
 *  ``"*"``. A concrete UUID switch passes through; the aggregate falls back
 *  to the caller's home tenant (the pre-scope-threading behavior). */
export function concreteTenantScope(scope: undefined | string): string | undefined {
  return scope === SCOPE_ALL ? undefined : scope;
}

interface TenantScopeContextValue {
  scope: TenantScopeValue;
  setScope: (next: TenantScopeValue) => void;
  /** Translated for the SDK: ``undefined`` for home, ``"*"`` for all,
   *  or the UUID for a specific tenant. */
  apiTenantScope: undefined | string;
}

const TenantScopeContext = createContext<TenantScopeContextValue | null>(null);

const STORAGE_KEY = "expert_work.admin.tenantScope";

function readStored(): TenantScopeValue | null {
  if (typeof window === "undefined") return null;
  const value = window.sessionStorage.getItem(STORAGE_KEY);
  if (value === null) return null;
  if (value === SCOPE_HOME || value === SCOPE_ALL) return value;
  return value;
}

function writeStored(scope: TenantScopeValue): void {
  if (typeof window === "undefined") return;
  window.sessionStorage.setItem(STORAGE_KEY, scope);
}

/** /v1/me says non-admin, but a non-home scope is parked — the provider
 *  demotes it to home. */
function needsDemotion(identity: AuthIdentity | null, scope: TenantScopeValue): boolean {
  return Boolean(identity?.serverResolved && !identity.isSystemAdmin && scope !== SCOPE_HOME);
}

/** /v1/me says platform-homed system_admin on the (hidden) home scope — the
 *  provider promotes it to ``"*"``. */
function needsPromotion(identity: AuthIdentity | null, scope: TenantScopeValue): boolean {
  return Boolean(
    identity?.serverResolved &&
      identity.isSystemAdmin &&
      identity.homeIsPlatform &&
      scope === SCOPE_HOME,
  );
}

export function TenantScopeProvider({ children }: { children: ReactNode }) {
  const { identity } = useAuth();
  const isSystemAdmin = identity?.isSystemAdmin ?? false;

  const [scope, setScopeState] = useState<TenantScopeValue>(() => {
    const stored = readStored();
    if (stored !== null) return stored;
    return SCOPE_HOME;
  });

  // Demote stale scope when a non-system-admin lands with a cached
  // non-home value — ``"*"`` or any residual specific-tenant UUID (eg.
  // operator left a JWT in localStorage from a previous system-admin
  // session on this machine, or an admin got demoted mid-session). A
  // residual UUID would otherwise flag ``useIsTenantSwitched`` forever
  // and lock every write control greyed (Cross-tenant W3).
  //
  // Gate on ``serverResolved``: until ``/v1/me`` returns, the optimistic
  // identity reports ``isSystemAdmin=false``, which would wrongly demote a
  // real system_admin who reloaded on a non-home scope (scope flashes /
  // is lost). Only demote once the server truth confirms a non-admin.
  useEffect(() => {
    if (needsDemotion(identity, scope)) {
      setScopeState(SCOPE_HOME);
      writeStored(SCOPE_HOME);
    }
  }, [identity, scope]);

  // Promote a platform-homed system_admin off the (hidden) home scope. Their
  // home is the synthetic platform tenant, which the TenantSwitcher omits — so
  // ``"home"`` would leave the switcher showing a value that isn't an option.
  // The platform level is the ``"*"`` scope; land there. Gate on
  // ``serverResolved``: ``homeIsPlatform`` is only authoritative post-/v1/me.
  useEffect(() => {
    if (needsPromotion(identity, scope)) {
      setScopeState(SCOPE_ALL);
      writeStored(SCOPE_ALL);
    }
  }, [identity, scope]);

  const setScope = useCallback(
    (next: TenantScopeValue) => {
      if (next === SCOPE_ALL && !isSystemAdmin) {
        // Defense in depth: UI should never offer "*" to a non-admin,
        // but if some path slips through, refuse silently rather than
        // calling the API and getting a 403.
        return;
      }
      setScopeState(next);
      writeStored(next);
    },
    [isSystemAdmin],
  );

  const apiTenantScope = useMemo<undefined | string>(() => {
    if (scope === SCOPE_HOME) return undefined;
    // A non-home scope ("*" cross-tenant, or a specific tenant UUID) is
    // system_admin-only: the control-plane 403s any non-home scope for a
    // non-admin (CROSS_TENANT_FORBIDDEN / TENANT_NOT_ALLOWED). Only put it on
    // the wire once /v1/me has CONFIRMED the caller is a system_admin —
    // otherwise a stale scope left in sessionStorage by a prior admin session
    // races ahead of identity resolution and fires e.g. ?tenant_id=* → a
    // transient 403. (The demotion effect above corrects the persisted scope a
    // beat later, which is why a refresh "fixes" it.) The interim home value
    // must not reach a page either: TenantScopeGate holds the routed pages
    // back until it is settled (B-183).
    if (!(identity?.serverResolved && identity.isSystemAdmin)) return undefined;
    return scope;
  }, [scope, identity]);

  const value = useMemo<TenantScopeContextValue>(
    () => ({ scope, setScope, apiTenantScope }),
    [scope, setScope, apiTenantScope],
  );

  return (
    <TenantScopeContext.Provider value={value}>{children}</TenantScopeContext.Provider>
  );
}

/** Whether the scope pages would read with right now is about to change. */
function scopeSettling(
  identity: AuthIdentity | null,
  identitySettled: boolean,
  scope: TenantScopeValue,
): boolean {
  // Before /v1/me answers, ``apiTenantScope`` reports home whatever was
  // persisted (see above) — a non-home scope can only go on the wire after.
  // Home itself needs no wait: that's every tenant user's optimistic paint.
  if (!identitySettled) return scope !== SCOPE_HOME;
  // /v1/me is in and an effect above is about to move the scope: hold for
  // that one commit.
  return needsDemotion(identity, scope) || needsPromotion(identity, scope);
}

/**
 * Holds the routed pages back while the tenant scope is still settling
 * (B-183). Without it, a reload on a switched-in scope mounted every page
 * on home first (``apiTenantScope`` waits for /v1/me) and re-read once the
 * scope flipped; a page with no stale-response guard then showed whichever
 * answer landed last — e.g. the knowledge detail page stuck on a home-tenant
 * 404. Behind the gate a page mounts once, on the scope it should read with.
 *
 *   - persisted non-home scope, /v1/me pending → placeholder until it
 *     answers. If /v1/me fails (non-401) the gate opens on home, exactly as
 *     before; a 401 ends the session.
 *   - home scope → no wait. The one case that still reads before /v1/me is
 *     a platform-homed system_admin's first load in a tab (nothing persisted
 *     yet, indistinguishable from a tenant user until /v1/me): when the
 *     promotion to ``"*"`` lands the page is unmounted and mounted afresh,
 *     so the home answer has nowhere to land.
 *
 * Mounted inside the Shell: the chrome reads no tenant-scoped data on mount.
 */
export function TenantScopeGate({ children }: { children: ReactNode }) {
  const { identity, identitySettled } = useAuth();
  const { scope } = useTenantScope();
  if (scopeSettling(identity, identitySettled, scope)) {
    return (
      <div
        aria-hidden
        data-testid="tenant-scope-settling"
        style={{ minHeight: 120, padding: 24, opacity: 0.4 }}
      />
    );
  }
  return <>{children}</>;
}

export function useTenantScope(): TenantScopeContextValue {
  const ctx = useContext(TenantScopeContext);
  if (ctx === null) {
    throw new Error("useTenantScope() must be called inside <TenantScopeProvider>");
  }
  return ctx;
}
