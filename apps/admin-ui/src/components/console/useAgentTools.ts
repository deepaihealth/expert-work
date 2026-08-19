/**
 * useAgentTools — lazy, session-cached fetch of an agent's tool schemas
 * (PR-A.3 §十.2 Schema tab). `enabled` flips on the moment a tool /
 * `update_plan` record is selected in the trajectory view; once the fetch
 * lands (`"ready"`) it's never refetched for the same `(agentName,
 * agentVersion)` pair — only an identity change or an explicit `reload()`
 * re-arms it. Tenant scope taken the same way as `useRunTrace.ts:60`.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { getAgentTools, type AgentToolSchema } from "../../api/agents";
import { concreteTenantScope, useTenantScope } from "../../tenant/TenantScopeContext";

export type ToolSchemaStatus = "idle" | "loading" | "ready" | "error";

export interface ToolSchemaState {
  status: ToolSchemaStatus;
  byName: ReadonlyMap<string, AgentToolSchema>;
  /** Re-arms a fresh fetch — works from `"ready"` / `"error"` too. */
  reload: () => void;
}

export function useAgentTools(args: {
  agentName: string;
  agentVersion: string;
  enabled: boolean;
}): ToolSchemaState {
  const { agentName, agentVersion, enabled } = args;
  const { apiTenantScope } = useTenantScope();

  const [status, setStatus] = useState<ToolSchemaStatus>("idle");
  const [byName, setByName] = useState<ReadonlyMap<string, AgentToolSchema>>(new Map());
  const [nonce, setNonce] = useState(0);

  // A new agent identity — including tenant scope, since a system_admin's
  // ``apiTenantScope`` flipping (home → "*" → a specific tenant UUID) is
  // also "different data" (fix round 1, Important #2; TenantScopeContext.tsx
  // :122-133) — invalidates whatever's cached. Tracked via ref (not state)
  // so the single effect below can both detect the change and fetch for the
  // new identity in the same pass, instead of waiting for a second render
  // that no dependency would actually trigger.
  const identity = `${agentName}@${agentVersion}@${String(apiTenantScope)}`;
  const identityRef = useRef(identity);

  useEffect(() => {
    let cancelled = false;
    const identityChanged = identityRef.current !== identity;
    identityRef.current = identity;
    if (identityChanged) setByName(new Map());
    const idle = identityChanged || status === "idle";
    if (!enabled || !idle) {
      // Reset to idle when: identity changed (stale data), or a fetch that
      // was still in flight is now being abandoned because `enabled`
      // flipped off mid-`"loading"` — otherwise `status` gets stuck on
      // `"loading"` forever (no further `enabled` toggle can re-arm it,
      // since the `idle` check above would keep reading the stale
      // `"loading"` value) with no retry affordance on that tab (fix round
      // 1, Critical #1).
      if (identityChanged || (!enabled && status === "loading")) setStatus("idle");
      return () => {
        cancelled = true;
      };
    }
    setStatus("loading");
    void getAgentTools(agentName, agentVersion, concreteTenantScope(apiTenantScope))
      .then((data) => {
        if (cancelled) return;
        setByName(new Map(data.items.map((item) => [item.name, item])));
        setStatus("ready");
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        console.warn("useAgentTools: getAgentTools failed", err);
        setStatus("error");
      });
    return () => {
      cancelled = true;
    };
  }, [agentName, agentVersion, enabled, nonce, apiTenantScope]);

  const reload = useCallback(() => {
    setStatus("idle");
    setNonce((n) => n + 1);
  }, []);

  return { status, byName, reload };
}
