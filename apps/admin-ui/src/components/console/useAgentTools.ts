/**
 * useAgentTools — lazy, session-cached fetch of an agent's tool schemas
 * (PR-A.3 §十.2 Schema tab). `enabled` flips on the moment a tool /
 * `update_plan` record is selected in the trajectory view; once the fetch
 * lands (`"ready"`) it's never refetched for the same `(agentName,
 * agentVersion, tenant scope)` identity — only an identity change or an
 * explicit `reload()` re-arms it. Tenant scope taken the same way as
 * `useRunTrace.ts:60`.
 */
import { useCallback, useEffect, useState } from "react";

import { getAgentTools, type AgentToolSchema } from "../../api/agents";
import { concreteTenantScope, useTenantScope } from "../../tenant/TenantScopeContext";

export type ToolSchemaStatus = "idle" | "loading" | "ready" | "error";

export interface ToolSchemaState {
  status: ToolSchemaStatus;
  byName: ReadonlyMap<string, AgentToolSchema>;
  /** Re-arms a fresh fetch — works from `"ready"` / `"error"` too. */
  reload: () => void;
}

const EMPTY: ReadonlyMap<string, AgentToolSchema> = new Map();

/** Everything the hook knows is keyed by the identity it was fetched for —
 *  one state object, so status and data can never disagree about *which*
 *  agent they describe. */
interface Cached {
  identity: string;
  status: ToolSchemaStatus;
  byName: ReadonlyMap<string, AgentToolSchema>;
}

export function useAgentTools(args: {
  agentName: string;
  agentVersion: string;
  enabled: boolean;
}): ToolSchemaState {
  const { agentName, agentVersion, enabled } = args;
  const { apiTenantScope } = useTenantScope();

  // Identity includes tenant scope: a system_admin's ``apiTenantScope``
  // flipping (home → "*" → a specific tenant UUID) is also "different data"
  // (TenantScopeContext.tsx:122-133).
  const identity = `${agentName}@${agentVersion}@${String(apiTenantScope)}`;
  const [cache, setCache] = useState<Cached>({ identity, status: "idle", byName: EMPTY });
  const [nonce, setNonce] = useState(0);

  // Derived at render, not in an effect: the frame in which the identity
  // changes already reads as idle + empty, so the previous agent's map is
  // never shown (not even for one commit) against the new agent's rows
  // (PR-A.3 final review, Minor 5).
  const current: Cached =
    cache.identity === identity ? cache : { identity, status: "idle", byName: EMPTY };

  useEffect(() => {
    let cancelled = false;
    if (!enabled || current.status !== "idle") {
      // Two ways a stale `"loading"` could otherwise survive in `cache` with
      // no request behind it, and pin the tab on the spinner (no retry
      // affordance there — fix round 1, Critical #1): (1) `enabled` flipped
      // off mid-`"loading"`; (2) identity and `enabled` flipped together
      // (thread switch), leaving the OLD identity's `"loading"` entry in
      // place for when that identity comes back. Reset to idle for the
      // current identity in both cases.
      if (cache.identity !== identity || (!enabled && current.status === "loading")) {
        setCache({ identity, status: "idle", byName: EMPTY });
      }
      return () => {
        cancelled = true;
      };
    }
    setCache({ identity, status: "loading", byName: EMPTY });
    void getAgentTools(agentName, agentVersion, concreteTenantScope(apiTenantScope))
      .then((data) => {
        if (cancelled) return;
        setCache({
          identity,
          status: "ready",
          byName: new Map(data.items.map((item) => [item.name, item])),
        });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        console.warn("useAgentTools: getAgentTools failed", err);
        setCache({ identity, status: "error", byName: EMPTY });
      });
    return () => {
      cancelled = true;
    };
    // `current.status` is read from this render's closure on purpose: the
    // effect re-arms only on identity / enabled / nonce changes, never on
    // its own status transitions.
  }, [agentName, agentVersion, enabled, nonce, apiTenantScope]);

  const reload = useCallback(() => {
    setCache({ identity, status: "idle", byName: EMPTY });
    setNonce((n) => n + 1);
  }, [identity]);

  return { status: current.status, byName: current.byName, reload };
}
