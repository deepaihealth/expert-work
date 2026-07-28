/**
 * Platform delegation-gate config SDK — backed by
 * /v1/platform/delegation-config (perf phase2 PR3). system_admin-only,
 * platform-level. Caps how many concurrent sub-agent delegations a run may
 * have in flight at once. ``configured`` is the explicit platform override
 * (``null`` ⇒ unset, using the built-in default); ``effective`` is the
 * resolved capacity the agent runtime reads.
 */
import { getJson, putJson } from "./client";

export interface DelegationCapacity {
  max_concurrent_delegations: number;
}

export interface PlatformDelegationConfigView {
  /** Explicit platform override; ``null`` when unset (→ built-in default). */
  configured: DelegationCapacity | null;
  /** Resolved capacity (DB row if set, else the built-in default). */
  effective: DelegationCapacity;
}

export async function getPlatformDelegationConfig(): Promise<PlatformDelegationConfigView> {
  return getJson<PlatformDelegationConfigView>("/v1/platform/delegation-config");
}

export async function putPlatformDelegationConfig(
  capacity: DelegationCapacity,
): Promise<PlatformDelegationConfigView> {
  return putJson("/v1/platform/delegation-config", capacity);
}
