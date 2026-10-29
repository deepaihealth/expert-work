/**
 * SDK for a base's retrieval records — B-175 P2-PR4.
 *
 * ``/v1/knowledge/bases/{base}/retrieval-logs``: every knowledge search (an
 * agent's ``knowledge_search``, a console retrieval test) leaves one record —
 * the question, the parameters, how it ended, and each result's ids and four
 * scores; never document text. ``…/{id}/passages`` reads each result's text
 * as it is now. RAW responses (no envelope), like the rest of ``/v1/knowledge``.
 *
 * Gate: ``session:read`` (the conversation records' — viewers too), console
 * only, one concrete tenant per request (``tenant_id=*`` is refused, so
 * callers pass a concrete scope or none).
 */
import { apiClient, withTenantScope, type TenantScope } from "./client";
import type { KnowledgeHitDocument, PageParams } from "./knowledge";

/** How long records are kept — the backend's default
 *  (``EXPERT_WORK_RETENTION_KNOWLEDGE_RETRIEVAL_LOG_RETENTION_DAYS``, 30); the
 *  API does not report the configured value. */
export const RETRIEVAL_LOG_RETENTION_DAYS = 30;

/** Who searched: an agent run, a console retrieval test, or (reserved) the
 *  open API. */
export type RetrievalLogSource = "agent" | "console_test" | "open_api";

/** ``ok`` / ``no_hits`` / ``below_threshold`` as the search ended, or
 *  ``error`` when it raised (``flags.error`` names the exception type). */
export type RetrievalLogStatus = "ok" | "no_hits" | "below_threshold" | "error";

export interface RetrievalLogSummary {
  id: string;
  source: RetrievalLogSource;
  /** The question (cut to 2,000 characters). */
  query: string;
  status: RetrievalLogStatus | string;
  /** Over every base the search covered. */
  result_count: number;
  latency_ms: number;
  /** The agent run and its conversation (``source === "agent"``). */
  run_id: string | null;
  thread_id: string | null;
  /** The employee behind a console test. */
  actor_id: string | null;
  /** ``limit`` / ``method`` / ``score_threshold`` / ``rerank`` /
   *  ``rerank_score_threshold`` — ``null`` = the base's default. */
  params: Record<string, unknown>;
  /** Degradation flags, candidate counts, the rerank model … (the search
   *  outcome's fields), or ``{error}`` when it raised. */
  flags: Record<string, unknown>;
  created_at: string;
}

/** One result: ids, position, recall path and the best hit chunk's four
 *  scores — no text. */
export interface RetrievalLogResult {
  chunk_id: string | null;
  document_id: string | null;
  /** The parent row (「整块」) of the hit chunk; ``null`` when the chunk
   *  stands for itself. */
  parent_chunk_id: string | null;
  chunk_index: number | null;
  parent_index: number | null;
  recall_source: string | null;
  vector_score: number | null;
  keyword_score: number | null;
  fused_score: number | null;
  rerank_score: number | null;
  /** The document as it is now; ``null`` once deleted. */
  document: KnowledgeHitDocument | null;
}

export interface RetrievalLogDetail extends RetrievalLogSummary {
  /** Every base the search covered (a result may be another one's). */
  kb_ids: string[];
  results: RetrievalLogResult[];
}

export interface RetrievalLogsPage {
  logs: RetrievalLogSummary[];
  total: number;
  offset: number;
  limit: number;
}

/** One stored row's text as it is now. */
export interface LivePassageText {
  id: string;
  content: string;
  heading_path: string[];
  enabled: boolean;
}

/** A result's passage (its parent row, or the chunk standing for itself)
 *  and best hit chunk under it; ``null`` once gone. Aligned with ``results``. */
export interface LivePassage {
  passage: LivePassageText | null;
  hit: LivePassageText | null;
}

export interface RetrievalLogFilters {
  source?: RetrievalLogSource;
  status?: RetrievalLogStatus;
  /** Searches that came back empty (not ones that raised). */
  zeroHit?: boolean;
  /** ISO time, inclusive. */
  since?: string;
  /** ISO time, exclusive. */
  until?: string;
}

function logsPath(baseName: string): string {
  return `/v1/knowledge/bases/${encodeURIComponent(baseName)}/retrieval-logs`;
}

export async function listRetrievalLogs(
  baseName: string,
  filters: RetrievalLogFilters,
  page: PageParams,
  tenantScope?: TenantScope,
): Promise<RetrievalLogsPage> {
  const params: Record<string, unknown> = { limit: page.limit, offset: page.offset };
  if (filters.source) params.source = filters.source;
  if (filters.status) params.status = filters.status;
  if (filters.zeroHit) params.zero_hit = true;
  if (filters.since) params.since = filters.since;
  if (filters.until) params.until = filters.until;
  const response = await apiClient.get<RetrievalLogsPage>(logsPath(baseName), {
    params: withTenantScope(params, tenantScope),
  });
  return response.data;
}

export async function getRetrievalLog(
  baseName: string,
  logId: string,
  tenantScope?: TenantScope,
): Promise<RetrievalLogDetail> {
  const response = await apiClient.get<RetrievalLogDetail>(
    `${logsPath(baseName)}/${encodeURIComponent(logId)}`,
    { params: withTenantScope({}, tenantScope) },
  );
  return response.data;
}

/** Each result's text as it is now (the record keeps none). */
export async function getRetrievalLogPassages(
  baseName: string,
  logId: string,
  tenantScope?: TenantScope,
): Promise<LivePassage[]> {
  const response = await apiClient.get<{ passages: LivePassage[] }>(
    `${logsPath(baseName)}/${encodeURIComponent(logId)}/passages`,
    { params: withTenantScope({}, tenantScope) },
  );
  return response.data.passages;
}
