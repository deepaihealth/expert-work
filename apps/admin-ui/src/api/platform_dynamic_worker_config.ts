/**
 * Platform dynamic-worker limits SDK — backed by
 * /v1/platform/dynamic-worker-config (B3 PR2). system_admin-only,
 * platform-level. Two tiers of guardrails for the ``dynamic_worker`` tool
 * (spawn_worker) — 弹性 worker 预算: the ``max_*`` *default* tier applies to
 * agents whose manifest doesn't ask, the ``cap_max_*`` *hard-cap* tier is
 * the ceiling a per-agent ``dynamic_workers.max_*`` request is clamped to.
 * ``configured`` is the explicit platform override (``null`` ⇒ unset, using
 * the process's env-default settings snapshot); ``effective`` is the
 * resolved limits the agent build reads.
 */
import { getJson, putJson } from "./client";

export interface DynamicWorkerLimits {
  max_concurrent: number;
  max_per_run: number;
  max_iterations: number;
  cap_max_concurrent: number;
  cap_max_per_run: number;
  cap_max_iterations: number;
}

export interface PlatformDynamicWorkerConfigView {
  /** Explicit platform override; ``null`` when unset (→ env default). */
  configured: DynamicWorkerLimits | null;
  /** Resolved limits (DB row if set, else the env default). */
  effective: DynamicWorkerLimits;
}

export async function getPlatformDynamicWorkerConfig(): Promise<PlatformDynamicWorkerConfigView> {
  return getJson<PlatformDynamicWorkerConfigView>("/v1/platform/dynamic-worker-config");
}

export async function putPlatformDynamicWorkerConfig(
  limits: DynamicWorkerLimits,
): Promise<PlatformDynamicWorkerConfigView> {
  return putJson("/v1/platform/dynamic-worker-config", limits);
}
