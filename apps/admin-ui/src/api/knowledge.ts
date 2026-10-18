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
  /** Minimum rerank score to keep a hit (B-175 wave 2 PR3); ``null`` = off. */
  rerank_score_threshold?: number | null;
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
  /** A re-upload is waiting in the pending slot (ingesting, or failed); the
   *  previous version's chunks keep serving until it lands or is discarded. */
  has_pending?: boolean;
  /** Tenant-wide document number (short id ``D{doc_seq}``); ``null`` only
   *  for documents from before it existed. */
  doc_seq?: number | null;
  title?: string | null;
  source_type?: string;
  /** Furthest pipeline step reached — ``queued`` / ``parsing`` /
   *  ``chunking`` / ``embedding`` / ``indexing`` / ``ready``. */
  stage?: string | null;
  /** Machine-readable failure reason next to ``error``. */
  error_code?: string | null;
  size_bytes?: number | null;
  mime?: string | null;
  page_count?: number | null;
  enabled?: boolean;
  created_at: string | null;
  updated_at: string | null;
}

/** Upload response — ``unchanged`` is true when the bytes matched the
 *  ready document already there (200, nothing re-ingested). */
export interface UploadedDocument extends KnowledgeDocument {
  unchanged?: boolean;
}

/** One page of a list endpoint called with ``limit`` / ``offset``. */
export interface PageParams {
  limit: number;
  offset: number;
}

export interface DocumentsPage {
  documents: KnowledgeDocument[];
  total: number;
  offset: number;
  limit: number | null;
}

export interface BasesPage {
  bases: KnowledgeBase[];
  total: number;
  offset: number;
  limit: number | null;
}

/** One chunk in the segment-preview list (embedding intentionally omitted). */
export interface KnowledgeChunk {
  id: string;
  chunk_index: number;
  /** The chunk body (legacy chunks still carry a ``[Section: …]`` prefix). */
  content: string;
  /** Section breadcrumb, outermost first; empty for legacy chunks. */
  heading_path?: string[];
  chunk_type?: string;
  enabled?: boolean;
  edited?: boolean;
  revision?: number;
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
  /** Section breadcrumb of the hit, outermost first. */
  heading_path?: string[];
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
  rerank_score_threshold?: number | null;
}

export interface RetrievalTestBody {
  query: string;
  top_k?: number;
  method?: RetrievalMethod;
  score_threshold?: number | null;
  rerank?: boolean;
}

/** Upload whitelist — mirrors the backend's SUPPORTED_EXTENSIONS
 *  (``knowledge/parsing.py``); the upload queue's pre-flight check so
 *  unsupported files are skipped before any request. Deliberately not an
 *  Upload ``accept`` attr: rc-upload would filter those files out before
 *  the queue can count them. */
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

/** Client-side upload size cap — B-175 L3. Same value as the backend's
 *  ``settings.knowledge_document_max_bytes`` default (50 MiB); files over it
 *  are skipped before any request. A server 413 (different configured cap)
 *  still surfaces per file as ``failed`` with the server's limit. */
export const KNOWLEDGE_DOCUMENT_MAX_BYTES = 50 * 1024 * 1024;

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

/** One page of bases (the list page); ``listBases`` stays the whole list
 *  for the knowledge picker. */
export async function listBasesPage(
  page: PageParams,
  tenantScope?: TenantScope,
): Promise<BasesPage> {
  const response = await apiClient.get<BasesPage>("/v1/knowledge/bases", {
    params: withTenantScope({ limit: page.limit, offset: page.offset }, tenantScope),
  });
  return response.data;
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

export interface UploadDocumentOptions {
  /** Aborts the request (axios ``signal``); the rejection is an ``ApiError``
   *  with status 0 — callers that cancel check ``signal.aborted`` first. */
  signal?: AbortSignal;
  /** Upload progress in whole percent (0–100), from axios ``onUploadProgress``. */
  onProgress?: (percent: number) => void;
}

/** POST multipart — 202 Accepted; ingestion runs in the background and
 *  the caller polls ``listDocuments`` for the status (Mini-ADR H-18). 200
 *  with ``unchanged: true`` when the same bytes are already ready. A name
 *  already in the base is an update: the new bytes wait in the pending slot
 *  while the previous version keeps serving. */
export async function uploadDocument(
  baseName: string,
  file: File,
  { signal, onProgress }: UploadDocumentOptions = {},
): Promise<UploadedDocument> {
  const form = new FormData();
  form.append("file", file);
  const response = await apiClient.post<UploadedDocument>(
    `/v1/knowledge/bases/${encodeURIComponent(baseName)}/documents`,
    form,
    {
      headers: { "Content-Type": "multipart/form-data" },
      signal,
      onUploadProgress: onProgress
        ? (event) => {
            const total = event.total ?? file.size;
            if (total > 0) onProgress(Math.min(100, Math.round((event.loaded / total) * 100)));
          }
        : undefined,
    },
  );
  return response.data;
}

export async function listDocuments(
  baseName: string,
  page: PageParams,
  tenantScope?: TenantScope,
): Promise<DocumentsPage> {
  const response = await apiClient.get<DocumentsPage>(
    `/v1/knowledge/bases/${encodeURIComponent(baseName)}/documents`,
    { params: withTenantScope({ limit: page.limit, offset: page.offset }, tenantScope) },
  );
  return response.data;
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

/** Drop a re-upload waiting in the pending slot and keep serving the
 *  previous version. Throws (HTTP 409) when nothing is pending. */
export async function discardPendingDocument(
  baseName: string,
  documentId: string,
): Promise<KnowledgeDocument> {
  const response = await apiClient.post<KnowledgeDocument>(
    `/v1/knowledge/bases/${encodeURIComponent(baseName)}/documents/${encodeURIComponent(documentId)}/discard-pending`,
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
