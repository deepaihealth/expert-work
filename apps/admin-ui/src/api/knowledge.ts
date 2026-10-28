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
import type { RerankOutcome } from "./rerank_outcome";

export type { RerankOutcome } from "./rerank_outcome";

/** Document ingestion lifecycle — mirrors the backend ``DocumentStatus``
 *  (``cancelled``: the user stopped the ingest, B-222). */
export type DocumentStatus = "pending" | "processing" | "ready" | "failed" | "cancelled";

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
  /** B-206 K-7b — the rerank model (``provider/model``) the threshold was set
   *  under; ``null`` when none is set, or it was set before models were recorded. */
  rerank_threshold_model?: string | null;
  /** The threshold is left out of searches: another rerank model scores now. */
  rerank_threshold_paused?: boolean;
  /** The platform's rerank model now (``provider/model``); ``null`` = none. */
  current_rerank_model?: string | null;
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
   *  ``recognizing`` (B-209, scanned pages only) / ``chunking`` /
   *  ``embedding`` / ``indexing`` / ``ready``. */
  stage?: string | null;
  /** Machine-readable failure reason next to ``error``. */
  error_code?: string | null;
  size_bytes?: number | null;
  mime?: string | null;
  page_count?: number | null;
  /** B-209 — page counts of the latest PDF ingest; ``null`` for other files. */
  page_stats?: PageStats | null;
  /** B-209 PR-2 — what became of the document's pictures; ``null`` when it
   *  was ingested before pictures were described. */
  figure_stats?: FigureStats | null;
  enabled?: boolean;
  created_at: string | null;
  updated_at: string | null;
}

/** B-209 — 「已识别 N 页 · 失败 M 页 · 待识别 K 页」 for a PDF with scanned pages. */
export interface PageStats {
  pages_total: number;
  /** Scanned pages a recognition model read. */
  pages_ocr: number;
  /** Both the recognition model and its fallback failed — 「补识别」 retries. */
  pages_failed: number;
  /** No usable recognition model when ingested — 「补识别」 once configured. */
  pages_pending: number;
  /** Past the per-document recognition page limit — 「补识别」 reads them
   *  once the platform limit is raised. */
  pages_over_limit: number;
}

/** B-209 PR-2 — 「已描述 N 张图」: the pictures inside a PDF / Word / slide
 *  file, from extraction to description. */
export interface FigureStats {
  /** Pictures found in the document. */
  extracted: number;
  /** Dropped by the filters (too small, a tiny share of the page, a few KB,
   *  repeated: logos, icons, backgrounds). */
  filtered: number;
  /** Past the per-document picture limit (smallest first). */
  capped: number;
  /** Formats that cannot be read (WMF / EMF / SVG …). */
  unsupported: number;
  /** Described into the document text. */
  described: number;
  /** The description models gave no usable answer — 「补识别」 retries. */
  failed: number;
  /** No usable description model when ingested — 「补识别」 once configured. */
  pending: number;
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
  /** B-206 K-1 — the ``chunk_index`` of the parent row (「整块」) this chunk
   *  belongs to; ``null`` without one, and for a parent row itself. */
  parent_index?: number | null;
}

/** B-206 K-1 — which rows the segment preview lists: the searchable chunks
 *  (「小段」, the default) or the parent rows grouping them (「整块」). */
export type ChunkView = "children" | "parents";

export interface ChunksPage {
  chunks: KnowledgeChunk[];
  /** B-209 PR-2 — ids of the pictures these chunks describe whose originals
   *  the server holds for this document; only these get a thumbnail. */
  figure_ids?: string[];
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
  /** B-206 K-1 — a result is the parent row (「整块」) of its hit chunks: then
   *  ``content`` / ``heading_path`` are the parent's and this is its index;
   *  ``null`` when the hit has no parent row (the result is the chunk itself).
   *  ``chunk_index`` and the scores are the best hit chunk's. */
  parent_index?: number | null;
  /** Every hit chunk of the result, best first (``[0] === chunk_index``). */
  hit_chunk_indexes?: number[];
  /** The best hit chunk's own body; ``null`` when the result is that chunk. */
  hit_content?: string | null;
  /** The best hit chunk's own breadcrumb (empty when the result is that chunk). */
  hit_heading_path?: string[];
  // ── B-175 P2-PR4 — the best hit chunk's four scores (``null`` for a path
  //    that did not find it; ``vector_score`` is ``score``) and the document. ──
  vector_score?: number | null;
  keyword_score?: number | null;
  fused_score?: number | null;
  rerank_score?: number | null;
  /** The document as it is now; ``null`` once deleted. */
  document?: KnowledgeHitDocument | null;
}

/** B-175 P2-PR4 — the document a retrieval hit belongs to (B-206 §6: short
 *  id ``D{doc_seq}``, title, upload time). */
export interface KnowledgeHitDocument {
  id: string;
  doc_seq: number | null;
  title: string | null;
  filename: string;
  created_at: string | null;
}

/** How a knowledge search ended — ``no_hits`` / ``below_threshold`` came back
 *  empty (every candidate under the rerank floor for the latter). */
export type SearchStatus = "ok" | "no_hits" | "below_threshold";

export interface RetrievalTestResponse {
  query: string;
  results: RetrievalTestResult[];
  count: number;
  /** B-206 K-7 — ``null`` when the search asked for no rerank. */
  rerank_outcome?: RerankOutcome | null;
  /** B-206 K-7b — the base's threshold was left out: set under another model. */
  rerank_threshold_paused?: boolean;
  /** The model the base's threshold was set under (``provider/model``). */
  rerank_threshold_model?: string | null;
  /** The model that scored this search; ``null`` without scores. */
  rerank_model?: string | null;
  // ── B-175 wave 2 / B-206 — how the search ran (B-175 P2-PR4 shows them). ──
  status?: SearchStatus;
  /** Ranked passages the rerank floor was applied to. */
  candidates?: number;
  /** K-8 — the question could not be embedded: keyword results only. */
  embedding_degraded?: boolean;
  /** K-22 — keyword recall did not finish: the vector path's results only. */
  keyword_degraded?: boolean;
  /** K-21② — passages left out as a copy of a better-ranked one. */
  duplicates_dropped?: number;
  /** K-14 — candidates the reranker was handed but never scored. */
  rerank_dropped?: number;
  /** B-175 P2-PR4 — the search's own time (not its logging). */
  elapsed_ms?: number;
  /** The retrieval record this search wrote; ``null`` when none was written. */
  log_id?: string | null;
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
  /** B-206 K-7b — bind the stored threshold, unchanged, to the current rerank model. */
  rerank_threshold_confirm?: boolean;
}

export interface RetrievalTestBody {
  query: string;
  top_k?: number;
  method?: RetrievalMethod;
  score_threshold?: number | null;
  rerank?: boolean;
  /** B-175 wave 2 PR3 — a rerank floor for this search only (calibration);
   *  omitted = the base's own applies. */
  rerank_score_threshold?: number;
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

/** B-209 「补识别」 — re-drive a document whose scanned pages failed or waited
 *  for recognition (202). Pages already recognized come from the recognition
 *  cache; only the failed / waiting pages are sent to a model. Throws (HTTP
 *  409) when no page needs it. */
export async function recognizeDocument(
  baseName: string,
  documentId: string,
): Promise<KnowledgeDocument> {
  const response = await apiClient.post<KnowledgeDocument>(
    `/v1/knowledge/bases/${encodeURIComponent(baseName)}/documents/${encodeURIComponent(documentId)}/recognize`,
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

/** B-222 — what cancelling did with one document: ``not_cancellable`` = it
 *  had already settled (ready / failed / cancelled). */
export type CancelOutcome = "cancelled" | "not_cancellable" | "not_found";

export interface CancelResult {
  document_id: string;
  outcome: CancelOutcome;
}

/** B-222 — stop the ingest of pending / processing documents; one result per
 *  distinct id, in request order. */
export async function cancelDocuments(
  baseName: string,
  documentIds: string[],
): Promise<CancelResult[]> {
  const response = await apiClient.post<{ results: CancelResult[] }>(
    `/v1/knowledge/bases/${encodeURIComponent(baseName)}/documents/cancel`,
    { document_ids: documentIds },
  );
  return response.data.results;
}

/** B-209 PR-2 — one described picture of a document (the original, stored
 *  when it was described) as an object URL for an ``<img>``; the caller
 *  revokes it. Throws (HTTP 404) when there is no such picture. */
export async function fetchFigureUrl(
  baseName: string,
  documentId: string,
  figureId: string,
  tenantScope?: TenantScope,
): Promise<string> {
  const response = await apiClient.get<Blob>(
    `/v1/knowledge/bases/${encodeURIComponent(baseName)}/documents/${encodeURIComponent(documentId)}/figures/${encodeURIComponent(figureId)}`,
    { params: withTenantScope({}, tenantScope), responseType: "blob" },
  );
  return URL.createObjectURL(response.data);
}

export async function listChunks(
  baseName: string,
  documentId: string,
  params?: { offset?: number; limit?: number; view?: ChunkView },
  tenantScope?: TenantScope,
): Promise<ChunksPage> {
  const response = await apiClient.get<ChunksPage>(
    `/v1/knowledge/bases/${encodeURIComponent(baseName)}/documents/${encodeURIComponent(documentId)}/chunks`,
    {
      params: withTenantScope(
        { offset: params?.offset, limit: params?.limit, view: params?.view },
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
