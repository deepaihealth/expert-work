/**
 * Auth context — Stream H.1b PR 2a.
 *
 * Identity resolution is now server-authoritative:
 *
 *   1. The UI bootstraps an optimistic ``AuthIdentity`` synchronously
 *      from the JWT payload (or the API key prefix) so the first paint
 *      doesn't flash.
 *   2. As soon as the provider mounts (or after :func:`login`) it calls
 *      ``GET /v1/me`` and replaces the identity with the server's view.
 *      That gives us:
 *        - real ``is_system_admin`` for API key callers (the bearer
 *          itself is opaque, so client-side decode cannot determine
 *          this);
 *        - real ``is_system_admin`` for JWT callers when the role
 *          binding is platform-scope (Stream N augments the principal
 *          after the JWT verifies, so the JWT claim alone is not
 *          authoritative);
 *        - real home ``tenant_id`` for API key callers.
 *
 * If ``/v1/me`` returns 401 we treat the token as invalid and log out.
 * Any other failure leaves the optimistic identity in place — the user
 * still authenticates on every API call, the UI just shows less
 * accurate hints until the next refresh.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import {
  getStoredToken,
  setStoredToken,
  setUnauthorizedHandler,
  ApiError,
} from "../api/client";
import { getMe, ALL_TENANTS, type MeResponse } from "../api/me";
import { registerEvents, signOut as oidcSignOut } from "./oidc";

export interface AuthIdentity {
  /** ``"jwt"`` for OIDC tokens, ``"api_key"`` for expert_work bearer keys,
   *  ``"mtls"`` for internal service-to-service callers. */
  kind: "jwt" | "api_key" | "mtls";
  /** Subject id (UUID for users / service accounts; opaque for keys
   *  until ``/v1/me`` resolves). */
  subject: string;
  /** ``user`` / ``service_account`` / ``service`` (mTLS). */
  subjectType: "user" | "service_account" | "service";
  /** Home tenant. ``null`` only during the brief bootstrap window when
   *  the token is an opaque API key and ``/v1/me`` hasn't returned. */
  homeTenantId: string | null;
  roles: readonly string[];
  isSystemAdmin: boolean;
  /** ``true`` when the home tenant is the synthetic platform tenant — the
   *  TenantSwitcher hides it (platform level is the ``"*"`` scope, not a peer
   *  row). Only authoritative once ``serverResolved``; the optimistic decode
   *  cannot know it, so it reports ``false``. (Stream ACCT.) */
  homeIsPlatform: boolean;
  /** Short string for the user menu — JWT subject head or API key prefix. */
  displayName: string;
  /** ``true`` once :func:`getMe` has replaced the optimistic identity
   *  with the server-truth view (kept across a silent renew of the same
   *  subject). Components that gate destructive actions on
   *  ``isSystemAdmin`` should wait for this. */
  serverResolved: boolean;
}

export interface AuthState {
  status: "loading" | "anonymous" | "authenticated";
  identity: AuthIdentity | null;
  token: string | null;
  /** ``true`` once this session's ``/v1/me`` has answered — resolved, or
   *  failed with anything but 401 (the optimistic identity then stays, see
   *  the module docstring). Lets a consumer that must not act on the
   *  optimistic identity wait for the server without hanging when
   *  ``/v1/me`` is down. A silent renew of the same subject keeps it. */
  identitySettled: boolean;
}

interface AuthContextValue extends AuthState {
  login(token: string): void;
  logout(): void;
  /** Force a re-fetch of ``/v1/me`` — eg. after a role binding edit
   *  refreshes the caller's effective system_admin status. */
  refreshIdentity(): Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function decodeJwtPayload(token: string): Record<string, unknown> | null {
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    const payload = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const padded = payload.padEnd(payload.length + ((4 - (payload.length % 4)) % 4), "=");
    if (typeof atob !== "function") {
      return null;
    }
    return JSON.parse(atob(padded));
  } catch {
    return null;
  }
}

function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((v): v is string => typeof v === "string");
}

function isSubjectType(value: string | null): value is AuthIdentity["subjectType"] {
  return value === "user" || value === "service_account" || value === "service";
}

/** Optimistic identity from local decode — replaced by :func:`getMe`. */
function optimisticIdentityFromToken(token: string): AuthIdentity {
  if (token.startsWith("aforge_pat_") || token.startsWith("expert_work_")) {
    return {
      kind: "api_key",
      subject: token.slice(0, 16),
      subjectType: "service_account",
      homeTenantId: null,
      roles: [],
      isSystemAdmin: false,
      homeIsPlatform: false,
      displayName: `${token.slice(0, 12)}…`,
      serverResolved: false,
    };
  }
  const payload = decodeJwtPayload(token);
  if (payload === null) {
    return {
      kind: "jwt",
      subject: "?",
      subjectType: "user",
      homeTenantId: null,
      roles: [],
      isSystemAdmin: false,
      homeIsPlatform: false,
      displayName: "anonymous",
      serverResolved: false,
    };
  }
  const subject = asString(payload.sub) ?? "?";
  const roles = asStringArray(payload.roles);
  const rawSubType = asString(payload.sub_type);
  const subjectType: AuthIdentity["subjectType"] = isSubjectType(rawSubType)
    ? rawSubType
    : "user";
  const human =
    asString(payload.email) ??
    asString(payload.preferred_username) ??
    asString(payload.name);
  return {
    kind: "jwt",
    subject,
    subjectType,
    homeTenantId: asString(payload.tenant_id),
    roles,
    isSystemAdmin: roles.includes("system_admin"),
    homeIsPlatform: false,
    displayName: human ?? (subject.length > 12 ? `${subject.slice(0, 8)}…` : subject),
    serverResolved: false,
  };
}

/** Identity built from the server's ``/v1/me`` response — the
 *  authoritative version. */
function identityFromMe(me: MeResponse, token: string): AuthIdentity {
  const isApiKey = token.startsWith("aforge_pat_") || token.startsWith("expert_work_");
  const kind: AuthIdentity["kind"] = isApiKey
    ? "api_key"
    : me.auth_method === "mtls"
      ? "mtls"
      : "jwt";
  const displayName = isApiKey
    ? `${token.slice(0, 12)}…`
    : (me.email ??
      (me.subject_id.length > 12 ? `${me.subject_id.slice(0, 8)}…` : me.subject_id));
  return {
    kind,
    subject: me.subject_id,
    subjectType: me.subject_type,
    homeTenantId: me.tenant_id,
    roles: me.roles,
    isSystemAdmin: me.is_system_admin,
    homeIsPlatform: me.home_is_platform,
    displayName,
    serverResolved: true,
  };
}

const ANONYMOUS: AuthState = {
  status: "anonymous",
  identity: null,
  token: null,
  identitySettled: false,
};

/** A session that just got its token: the optimistic decode until
 *  ``/v1/me`` answers. */
function newSession(token: string): AuthState {
  return {
    status: "authenticated",
    identity: optimisticIdentityFromToken(token),
    token,
    identitySettled: false,
  };
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({
    status: "loading",
    identity: null,
    token: null,
    identitySettled: false,
  });

  // The token doesn't authenticate any more — drop the local session. The
  // ProtectedRoute redirect to /login then preserves the current path in
  // ``state.from`` so a re-login lands back where the user was.
  const clearSession = useCallback(() => {
    setStoredToken(null);
    setState(ANONYMOUS);
  }, []);

  const resolveServerIdentity = useCallback(
    async (token: string) => {
      try {
        const me = await getMe();
        setState((prev) => {
          // The token may have rotated by the time the fetch returns;
          // discard the response if it would clobber a newer session.
          if (prev.token !== token) return prev;
          return {
            status: "authenticated",
            identity: identityFromMe(me, token),
            token,
            identitySettled: true,
          };
        });
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) {
          clearSession();
          return;
        }
        // Other errors leave the optimistic identity in place; the user
        // still re-authenticates on every API call. The answer is in all
        // the same — nothing should keep waiting for a better one.
        setState((prev) =>
          prev.token !== token || prev.identitySettled
            ? prev
            : { ...prev, identitySettled: true },
        );
      }
    },
    [clearSession],
  );

  // ② 会话过期(2026-08 反馈)— 任意业务端点 401 都走同一条清会话流程,
  // 不再只弹「请求失败」横幅。client 是纯模块吃不了 context,所以在这里
  // 注册回调;并发 401 的去抖在 client 侧(见 setUnauthorizedHandler 注释)。
  useEffect(() => {
    setUnauthorizedHandler(clearSession);
    return () => setUnauthorizedHandler(null);
  }, [clearSession]);

  useEffect(() => {
    const stored = getStoredToken();
    if (stored) {
      setState(newSession(stored));
      void resolveServerIdentity(stored);
    } else {
      setState(ANONYMOUS);
    }
  }, [resolveServerIdentity]);

  // OIDC integration — when a silent renew completes, swap the token in
  // place; when the access token expires without a renew, log out.
  // No-op when OIDC isn't configured.
  useEffect(() => {
    const unsubscribe = registerEvents({
      onUserLoaded: (idToken) => {
        setStoredToken(idToken);
        setState((prev) => {
          // A renew of the same subject keeps the identity /v1/me gave us
          // while the re-check runs. Dropping it back to the optimistic
          // decode would flip ``apiTenantScope`` to home and back, so every
          // switched-in page re-read twice per renew — and a page without a
          // stale-response guard could keep the wrong answer (B-183).
          // Anything else (no session yet, another subject) starts over.
          const sameSubject =
            prev.identity !== null &&
            prev.identity.subject === optimisticIdentityFromToken(idToken).subject;
          return sameSubject
            ? { ...prev, status: "authenticated", token: idToken }
            : newSession(idToken);
        });
        void resolveServerIdentity(idToken);
      },
      onExpired: () => {
        setStoredToken(null);
        setState(ANONYMOUS);
      },
      onSilentRenewError: () => {
        // Soft failure — keep the existing token, surface only via
        // structured logs. Hard failure arrives via ``onExpired``.
      },
    });
    return unsubscribe;
  }, [resolveServerIdentity]);

  const login = useCallback(
    (token: string) => {
      setStoredToken(token);
      setState(newSession(token));
      void resolveServerIdentity(token);
    },
    [resolveServerIdentity],
  );

  const logout = useCallback(() => {
    setStoredToken(null);
    setState(ANONYMOUS);
    // Best-effort IdP-side end-session; the local session is already
    // cleared either way, so a failed remote signout doesn't strand the
    // user as half-logged-in.
    void oidcSignOut().catch(() => {});
  }, []);

  const refreshIdentity = useCallback(async () => {
    const token = state.token;
    if (token !== null) {
      await resolveServerIdentity(token);
    }
  }, [resolveServerIdentity, state.token]);

  const value = useMemo<AuthContextValue>(
    () => ({ ...state, login, logout, refreshIdentity }),
    [state, login, logout, refreshIdentity],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error("useAuth() must be called inside <AuthProvider>");
  }
  return value;
}

export { ALL_TENANTS };
export {
  optimisticIdentityFromToken as _identityFromTokenForTests,
  identityFromMe as _identityFromMeForTests,
};
