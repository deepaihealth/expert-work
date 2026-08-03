/**
 * Quality-dashboard SDK — backed by ``/v1/quality`` (RT-5 / RT-ADR-26).
 *
 * Returns the **raw** payload (no ``{ success, data, error }`` envelope),
 * consistent with the control-plane ``api/quality.py`` (which mirrors
 * ``api/eval_runs.py``). So these calls go through ``apiClient`` directly and
 * read ``response.data``, NOT through an envelope unwrap.
 *
 * Scope (cross-tenant W3): both lists accept an optional ``tenantScope``
 * (``?tenant_id=``) so a switched-in system_admin reads the target tenant's
 * quality data; ``undefined`` falls through to the home tenant.
 */
import { apiClient, withTenantScope, type TenantScope } from "./client";

/** One LLM-judge verdict for a sampled production run. ``overall`` /
 *  ``dimensions`` are a subjective 1-5 rubric score, not ground truth. */
export interface QualityScore {
  id: number;
  /** Cross-tenant W4 — owning tenant in the ``tenant_id=*`` aggregate;
   *  absent on pre-W4 backends. */
  tenant_id?: string | null;
  agent_name: string;
  agent_version: string;
  /** For drill-down: link to the run / conversation in run_detail. */
  run_id: string;
  thread_id: string;
  overall: number;
  dimensions: Record<string, number>;
  rationale: string;
  judge_model: string;
  observed_at: string | null;
}

/** One raised drift alert (recent mean dropped below baseline). */
export interface QualityDriftAlert {
  id: number;
  /** Cross-tenant W4 — owning tenant in the ``tenant_id=*`` aggregate;
   *  absent on pre-W4 backends. */
  tenant_id?: string | null;
  agent_name: string;
  recent_mean: number;
  baseline_mean: number;
  /** Relative drop ``(baseline - recent) / baseline``. */
  drift_pct: number;
  recent_count: number;
  baseline_count: number;
  detected_at: string | null;
}

export interface QualityScoreList {
  items: QualityScore[];
}

export interface QualityDriftAlertList {
  items: QualityDriftAlert[];
}

export interface ListQualityScoresParams {
  tenantScope?: TenantScope;
  agentName?: string;
  windowH?: number;
  limit?: number;
}

export interface ListQualityDriftAlertsParams {
  tenantScope?: TenantScope;
  agentName?: string;
  windowH?: number;
  limit?: number;
}

/** GET /v1/quality/scores — the per-agent score series (newest first). */
export async function listQualityScores(
  params: ListQualityScoresParams = {},
): Promise<QualityScoreList> {
  const { tenantScope, agentName, windowH, limit } = params;
  const response = await apiClient.get<QualityScoreList>("/v1/quality/scores", {
    params: withTenantScope(
      { agent_name: agentName, window_h: windowH, limit },
      tenantScope,
    ),
  });
  return response.data;
}

/** GET /v1/quality/drift-alerts — raised drift alerts (newest first). */
export async function listQualityDriftAlerts(
  params: ListQualityDriftAlertsParams = {},
): Promise<QualityDriftAlertList> {
  const { tenantScope, agentName, windowH, limit } = params;
  const response = await apiClient.get<QualityDriftAlertList>(
    "/v1/quality/drift-alerts",
    {
      params: withTenantScope(
        { agent_name: agentName, window_h: windowH, limit },
        tenantScope,
      ),
    },
  );
  return response.data;
}
