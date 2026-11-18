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
 * Fetching never surfaces an error: no permission, a restricted aggregate
 * view or a network failure all fall back to showing the raw id. Call the
 * hook once per page and pass the resolver down to the parts that need it.
 */
import { useCallback, useEffect, useState } from "react";

import type { TenantScope } from "../api/client";
import { listMembers, type TenantMember } from "../api/members";

/** ``GET /v1/members`` caps ``limit`` at 200 and pages by ``offset``; its
 *  ``total`` is the page length, so a short page marks the end. */
export const MEMBER_PAGE_SIZE = 200;
/** Paging guard (10 000 members) in case a backend ignores ``offset``. */
const MAX_MEMBER_PAGES = 50;

/** Returns the member's name for ``id``, or ``id`` itself when unknown. */
export type MemberNameOf = (id: string) => string;

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

async function fetchAllMembers(tenantScope: TenantScope): Promise<TenantMember[]> {
  let members: TenantMember[] = [];
  for (let page = 0; page < MAX_MEMBER_PAGES; page += 1) {
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
  const [names, setNames] = useState<ReadonlyMap<string, string>>(() => new Map());
  useEffect(() => {
    let cancelled = false;
    fetchAllMembers(tenantScope).then(
      (members) => {
        if (!cancelled) setNames(buildMemberNameMap(members));
      },
      () => {
        // Silent fallback — ids are shown as-is.
      },
    );
    return () => {
      cancelled = true;
    };
  }, [tenantScope]);
  return useCallback((id: string) => names.get(id) ?? id, [names]);
}
