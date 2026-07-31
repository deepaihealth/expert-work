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

import { useAuth } from "../auth/AuthContext";

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

export function TenantScopeProvider({ children }: { children: ReactNode }) {
  const { identity } = useAuth();
  const isSystemAdmin = identity?.isSystemAdmin ?? false;

  const [scope, setScopeState] = useState<TenantScopeValue>(() => {
    const stored = readStored();
    if (stored !== null) return stored;
    return SCOPE_HOME;
  });

  // Demote stale scope when a non-system-admin lands with a cached
  // ``"*"`` value (eg. operator left a JWT in localStorage from a
  // previous system-admin session on this machine).
  //
  // Gate on ``serverResolved``: until ``/v1/me`` returns, the optimistic
  // identity reports ``isSystemAdmin=false``, which would wrongly demote a
  // real system_admin who reloaded on the ``"*"`` scope (scope flashes /
  // is lost). Only demote once the server truth confirms a non-admin.
  useEffect(() => {
    if (identity?.serverResolved && !identity.isSystemAdmin && scope === SCOPE_ALL) {
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
    if (
      identity?.serverResolved &&
      identity.isSystemAdmin &&
      identity.homeIsPlatform &&
      scope === SCOPE_HOME
    ) {
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
    // beat later, which is why a refresh "fixes" it.) A confirmed admin's page
    // fetches key on apiTenantScope, so they re-fetch when it flips to "*".
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

export function useTenantScope(): TenantScopeContextValue {
  const ctx = useContext(TenantScopeContext);
  if (ctx === null) {
    throw new Error("useTenantScope() must be called inside <TenantScopeProvider>");
  }
  return ctx;
}
