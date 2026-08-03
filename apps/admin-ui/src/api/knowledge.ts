/**
 * SDK for ``/v1/knowledge`` — Stream H.7 + KB commercial uplift.
 *
 * Every endpoint is RAW (no envelope) → ``apiClient`` directly, never
 * ``getJson`` ([memory:envelope-vs-raw-contract-check]).
 *
 * Tenant semantics (Mini-ADR H-19, revised by cross-tenant W3): knowledge
 * bases are tenant-scoped shared assets (not per-user). The read endpoints
 * accept an optional ``tenantScope`` (``?tenant_id=``) so a switched-in
 * system_admin reads the target tenant's bases; ``undefined`` falls through
 * to the caller's home tenant as before.
 */
import { apiClient, withTenantScope, type TenantScope } from "./client";

/** Document ingestion lifecycle — mirrors the backend ``DocumentStatus``. */
export type DocumentStatus = "pending" | "processing" | "ready" | "failed";

/** Per-base recall strategy — mirrors the backend ``RetrievalMethod``. */
export type RetrievalMethod = "vector" | "keyword" | "hybrid";

/** Per-base retrieval defaults (surfaced so they are not hardcoded). */
export interface RetrievalConfig {
  top_k: number;
  /** Minimum vector similarity to keep a hit; ``null`` disables the cutoff. */
  score_threshold: number | null;
  method: RetrievalMethod;
  rerank_enabled: boolean;
}

export interface KnowledgeBaseStats {
  document_count: number;
  chunk_count: number;
}

export interface KnowledgeBase {
  id: string;
  name: string;
  /** Cross-tenant W4 — owning tenant in the ``tenant_id=*`` aggregate;
   *  absent on pre-W4 backends. */
  tenant_id?: string | null;
  chunk_max_tokens: number;
  chunk_overlap_tokens: number;
  created_at: string | null;
  // ── commercial-uplift fields (optional so list rows + KnowledgePicker
  //    stubs keep compiling; the backend always returns them). ──
  description?: string | null;
  created_by?: string | null;
  updated_at?: string | null;
  retrieval_config?: RetrievalConfig;
  embedding_provider?: string | null;
  embedding_model?: string | null;
  /** The recorded embedding model differs from the live platform model. */
  needs_reindex?: boolean;
  /** A re-index is in flight (the base is re-embedding its chunk text). */
  reindexing?: boolean;
  stats?: KnowledgeBaseStats;
}

export interface KnowledgeDocument {
  id: string;
  filename: string;
  status: DocumentStatus;
  /** Failure detail when ``status === "failed"``. */
  error: string | null;
  chunk_count: number;
  /** Ingestion attempts so far (durability). */
  attempts?: number;
  created_at: string | null;
  updated_at: string | null;
}

/** One chunk in the segment-preview list (embedding intentionally omitted). */
export interface KnowledgeChunk {
  id: string;
  chunk_index: number;
  content: string;
}

export interface ChunksPage {
  chunks: KnowledgeChunk[];
  total: number;
  offset: number;
  limit: number;
}

/** One ranked hit from the retrieval ("hit testing") endpoint. */
export interface RetrievalTestResult {
  content: string;
  /** ``filename#chunk_index`` source attribution. */
  source: string;
  filename: string;
  chunk_index: number;
  /** Vector cosine similarity in [0, 1]; ``null`` for keyword-only hits. */
  score: number | null;
  /** Which recall path surfaced it: ``vector`` | ``keyword`` | ``both``. */
  recall_source: string | null;
}

export interface RetrievalTestResponse {
  query: string;
  results: RetrievalTestResult[];
  count: number;
}

/** Update payload — only supplied keys are applied; ``null`` clears a
 *  nullable field. ``name`` is intentionally absent (rename is unsupported). */
export interface UpdateBaseBody {
  description?: string | null;
  chunk_max_tokens?: number;
  chunk_overlap_tokens?: number;
  retrieval_top_k?: number;
  retrieval_score_threshold?: number | null;
  retrieval_method?: RetrievalMethod;
  rerank_enabled?: boolean;
}

export interface RetrievalTestBody {
  query: string;
  top_k?: number;
  method?: RetrievalMethod;
  score_threshold?: number | null;
  rerank?: boolean;
}

/** Upload whitelist — mirrors the backend's SUPPORTED_EXTENSIONS
 *  (``knowledge/parsing.py``); used for the Upload ``accept`` attr and
 *  a pre-flight check so unsupported files fail before the request. */
export const SUPPORTED_DOCUMENT_EXTENSIONS = [
  ".pdf",
  ".docx",
  ".pptx",
  ".xlsx",
  ".md",
  ".markdown",
  ".txt",
  ".html",
  ".htm",
  ".csv",
] as const;

export function isSupportedDocument(filename: string): boolean {
  const dot = filename.lastIndexOf(".");
  if (dot < 0) return false;
  const ext = filename.slice(dot).toLowerCase();
  return (SUPPORTED_DOCUMENT_EXTENSIONS as readonly string[]).includes(ext);
}

export async function listBases(tenantScope?: TenantScope): Promise<KnowledgeBase[]> {
  const response = await apiClient.get<{ bases: KnowledgeBase[] }>("/v1/knowledge/bases", {
    params: withTenantScope({}, tenantScope),
  });
  return response.data.bases;
}

export async function getBase(
  name: string,
  tenantScope?: TenantScope,
): Promise<KnowledgeBase> {
  const response = await apiClient.get<KnowledgeBase>(
    `/v1/knowledge/bases/${encodeURIComponent(name)}`,
    { params: withTenantScope({}, tenantScope) },
  );
  return response.data;
}

export async function createBase(params: {
  name: string;
  description?: string;
  chunkMaxTokens?: number;
  chunkOverlapTokens?: number;
  retrievalTopK?: number;
  retrievalScoreThreshold?: number | null;
  retrievalMethod?: RetrievalMethod;
  rerankEnabled?: boolean;
}): Promise<KnowledgeBase> {
  const response = await apiClient.post<KnowledgeBase>("/v1/knowledge/bases", {
    name: params.name,
    description: params.description,
    chunk_max_tokens: params.chunkMaxTokens,
    chunk_overlap_tokens: params.chunkOverlapTokens,
    retrieval_top_k: params.retrievalTopK,
    retrieval_score_threshold: params.retrievalScoreThreshold,
    retrieval_method: params.retrievalMethod,
    rerank_enabled: params.rerankEnabled,
  });
  return response.data;
}

export async function updateBase(name: string, body: UpdateBaseBody): Promise<KnowledgeBase> {
  const response = await apiClient.patch<KnowledgeBase>(
    `/v1/knowledge/bases/${encodeURIComponent(name)}`,
    body,
  );
  return response.data;
}

export async function deleteBase(name: string): Promise<void> {
  await apiClient.delete(`/v1/knowledge/bases/${encodeURIComponent(name)}`);
}

/** Re-embed the base's retained chunk text with the current platform model
 *  (202 Accepted; the base reports ``reindexing`` until it completes). */
export async function reindexBase(name: string): Promise<void> {
  await apiClient.post(`/v1/knowledge/bases/${encodeURIComponent(name)}/reindex`);
}

/** POST multipart — 202 Accepted; ingestion runs in the background and
 *  the caller polls ``listDocuments`` for the status (Mini-ADR H-18). */
export async function uploadDocument(baseName: string, file: File): Promise<KnowledgeDocument> {
  const form = new FormData();
  form.append("file", file);
  const response = await apiClient.post<KnowledgeDocument>(
    `/v1/knowledge/bases/${encodeURIComponent(baseName)}/documents`,
    form,
    { headers: { "Content-Type": "multipart/form-data" } },
  );
  return response.data;
}

export async function listDocuments(
  baseName: string,
  tenantScope?: TenantScope,
): Promise<KnowledgeDocument[]> {
  const response = await apiClient.get<{ documents: KnowledgeDocument[] }>(
    `/v1/knowledge/bases/${encodeURIComponent(baseName)}/documents`,
    { params: withTenantScope({}, tenantScope) },
  );
  return response.data.documents;
}

export async function deleteDocument(baseName: string, documentId: string): Promise<void> {
  await apiClient.delete(
    `/v1/knowledge/bases/${encodeURIComponent(baseName)}/documents/${encodeURIComponent(documentId)}`,
  );
}

/** Re-drive a document's ingestion from its retained bytes (202 Accepted).
 *  Throws (HTTP 409) for a legacy document whose bytes were not retained. */
export async function reingestDocument(
  baseName: string,
  documentId: string,
): Promise<KnowledgeDocument> {
  const response = await apiClient.post<KnowledgeDocument>(
    `/v1/knowledge/bases/${encodeURIComponent(baseName)}/documents/${encodeURIComponent(documentId)}/reingest`,
  );
  return response.data;
}

export async function listChunks(
  baseName: string,
  documentId: string,
  params?: { offset?: number; limit?: number },
  tenantScope?: TenantScope,
): Promise<ChunksPage> {
  const response = await apiClient.get<ChunksPage>(
    `/v1/knowledge/bases/${encodeURIComponent(baseName)}/documents/${encodeURIComponent(documentId)}/chunks`,
    {
      params: withTenantScope(
        { offset: params?.offset, limit: params?.limit },
        tenantScope,
      ),
    },
  );
  return response.data;
}

/** Retrieval "hit testing" — run a query through the live retrieval pipeline
 *  and see the ranked chunks with scores + recall path. */
export async function testRetrieval(
  baseName: string,
  body: RetrievalTestBody,
  tenantScope?: TenantScope,
): Promise<RetrievalTestResponse> {
  const response = await apiClient.post<RetrievalTestResponse>(
    `/v1/knowledge/bases/${encodeURIComponent(baseName)}/test`,
    body,
    { params: withTenantScope({}, tenantScope) },
  );
  return response.data;
}
