/**
 * B-316 — show member names instead of raw user ids.
 *
 * Records carry two kinds of user id, both resolvable from the members list:
 *
 * - who did something (``created_by`` / ``actor_id`` / ``decided_by`` /
 *   ``updated_by``) is the request principal's subject, i.e. the OIDC
 *   ``sub`` — that is ``TenantMember.keycloak_user_id``;
 * - per-user tables (conversation / memory ``user_id``) carry the
 *   ``tenant_user.id`` — that is ``TenantMember.subject_id``.
 *
 * ``TenantMember.id`` is mapped too (the original AgentsList behaviour). All
 * three are UUIDs of the same member, so the keys never collide.
 *
 * Only tenant admins and system admins fetch: ``GET /v1/members`` needs
 * ``user:read``, and a denied call writes an ``auth:login_failed`` / DENIED
 * audit row — every page view by an operator / viewer would leave a
 * misleading "login failed" entry. Non-admins therefore keep seeing raw ids;
 * giving everyone names needs a separate backend endpoint that returns names
 * only (no emails) — left as follow-up.
 *
 * Fetching never surfaces an error: a restricted aggregate view or a network
 * failure falls back to showing the raw id. Call the hook once per page and
 * pass the resolver down to the parts that need it.
 */
import { useCallback, useEffect, useState } from "react";

import type { TenantScope } from "../api/client";
import { listMembers, type TenantMember } from "../api/members";
import { useAuth, type AuthIdentity } from "../auth/AuthContext";

/** ``GET /v1/members`` caps ``limit`` at 200 and pages by ``offset``; its
 *  ``total`` is the page length, so a short page marks the end. */
export const MEMBER_PAGE_SIZE = 200;
/** Paging guard (10 000 members) in case a backend ignores ``offset``. */
export const MAX_MEMBER_PAGES = 50;

/** Returns the member's name for ``id``, or ``id`` itself when unknown. */
export type MemberNameOf = (id: string) => string;

interface LoadedNames {
  /** The scope these names were fetched for — a different scope's names are
   *  never shown, so a scope switch starts from raw ids. */
  scope: TenantScope;
  names: ReadonlyMap<string, string>;
}

export function buildMemberNameMap(members: readonly TenantMember[]): ReadonlyMap<string, string> {
  return new Map(
    members.flatMap((m) => {
      const label = m.display_name || m.email;
      const ids = [m.id, m.subject_id, m.keycloak_user_id].filter(
        (id): id is string => typeof id === "string" && id.length > 0,
      );
      return ids.map((id) => [id, label] as const);
    }),
  );
}

function canReadMembers(identity: AuthIdentity | null): boolean {
  return identity !== null && (identity.isSystemAdmin || identity.roles.includes("admin"));
}

/** Resolves to ``null`` when cancelled between pages. */
async function fetchAllMembers(
  tenantScope: TenantScope,
  isCancelled: () => boolean,
): Promise<TenantMember[] | null> {
  let members: TenantMember[] = [];
  for (let page = 0; page < MAX_MEMBER_PAGES; page += 1) {
    if (isCancelled()) return null;
    const { items } = await listMembers({
      tenantScope,
      limit: MEMBER_PAGE_SIZE,
      offset: page * MEMBER_PAGE_SIZE,
    });
    members = [...members, ...items];
    if (items.length < MEMBER_PAGE_SIZE) break;
  }
  return members;
}

export function useMemberNames(tenantScope: TenantScope): MemberNameOf {
  const enabled = canReadMembers(useAuth().identity);
  const [loaded, setLoaded] = useState<LoadedNames | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    fetchAllMembers(tenantScope, () => cancelled).then(
      (members) => {
        if (!cancelled && members !== null) {
          setLoaded({ scope: tenantScope, names: buildMemberNameMap(members) });
        }
      },
      () => {
        // Silent fallback — ids are shown as-is.
      },
    );
    return () => {
      cancelled = true;
    };
  }, [tenantScope, enabled]);

  const names = enabled && loaded !== null && loaded.scope === tenantScope ? loaded.names : null;
  return useCallback((id: string) => names?.get(id) ?? id, [names]);
}
