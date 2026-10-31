/**
 * SDK for document and passage maintenance on ``/v1/knowledge`` — B-175
 * P3-PR3 (the console for the P3-PR2 endpoints).
 *
 * A passage (「整块」) is what a search returns and what the console edits as
 * one unit: a parent row with the chunks (「小段」) under it, or a chunk
 * standing for itself. Every endpoint is RAW (no envelope). Reads take the
 * optional ``tenantScope`` like the rest of the knowledge SDK; writes go to
 * the caller's home tenant (the console disables them while read-only).
 */
import { ApiError, apiClient, withTenantScope, type TenantScope } from "./client";

/** Why a passage is off: a person switched it off, or a re-ingest did
 *  because it holds text a person switched off on a passage that is gone. */
export type DisabledReason = "manual" | "inherited";

export interface PassageChild {
  id: string;
  chunk_index: number;
  content: string;
  heading_path: string[];
  enabled: boolean;
  disabled_reason: DisabledReason | null;
}

export interface Passage {
  id: string;
  /** ``parent``: a parent row (``chunk_index`` in the parents' own
   *  numbering); ``chunk``: a chunk standing for itself (its one child). */
  kind: "parent" | "chunk";
  chunk_index: number;
  content: string;
  heading_path: string[];
  enabled: boolean;
  disabled_reason: DisabledReason | null;
  /** Its text differs from what the file gave (always for one added by hand). */
  edited: boolean;
  /** What a text change must send back (optimistic lock). */
  revision: number;
  children: PassageChild[];
}

export interface PassagesPage {
  passages: Passage[];
  /** Ids of the pictures these passages describe whose originals the server
   *  holds for this document; only these get a thumbnail. */
  figure_ids: string[];
  total: number;
  offset: number;
  limit: number;
}

export interface PassageRevision {
  passage_id: string;
  /** ``0`` for a switch-off record (``switch_off_lost``). */
  revision: number;
  content: string;
  heading_path: string[];
  /** ``null``: the text the file gave (revision 1 of an ingested passage). */
  edited_by: string | null;
  /** B-241 — the editor's display name (member name, else user display name;
   *  your own login email for your own edits; never a coworker's email);
   *  ``null`` when none is known: show ``edited_by``. */
  edited_by_name: string | null;
  created_at: string | null;
  /** Since when a re-ingest left it without a passage; ``null`` otherwise. */
  orphaned_at: string | null;
  /** A switch-off nothing holds any more (nothing was switched off for it). */
  switch_off_lost: boolean;
}

export interface UnappliedEdits {
  /** Edits and switch-offs a re-ingest could not put back. */
  unapplied: PassageRevision[];
  /** Passages a re-ingest switched off by inheritance, to review. */
  inherited: Passage[];
}

export interface PassageTextBody {
  content: string;
  heading_path?: string[];
}

export interface PassagePatch {
  content?: string;
  heading_path?: string[];
  /** Required with a text change: the revision it was read at. */
  revision?: number;
  enabled?: boolean;
}

export type DocumentBatchAction = "enable" | "disable" | "reingest";

/** Why a batch left a document out: not this base's (or gone), its file
 *  was not kept (re-upload it), or (B-222) its new version is being written
 *  (try again once it is ready). */
export type BatchSkipReason = "not_found" | "content_missing" | "busy";

export interface BatchSkip {
  id: string;
  reason: BatchSkipReason;
}

export interface DocumentBatchResult {
  action: DocumentBatchAction | "delete";
  done: string[];
  skipped: BatchSkip[];
}

export interface StaleDocuments {
  /** Ready documents cut by other chunking parameters than the base's. */
  stale: number;
  chunk_params_hash: string;
}

export interface ReingestStaleResult {
  queued: string[];
  skipped: BatchSkip[];
  /** Past the per-call cap: call again. */
  remaining: number;
}

function documentPath(baseName: string, documentId: string): string {
  return `/v1/knowledge/bases/${encodeURIComponent(baseName)}/documents/${encodeURIComponent(documentId)}`;
}

function passagePath(baseName: string, documentId: string, passageId: string): string {
  return `${documentPath(baseName, documentId)}/passages/${encodeURIComponent(passageId)}`;
}

export async function listPassages(
  baseName: string,
  documentId: string,
  page: { offset: number; limit: number },
  tenantScope?: TenantScope,
): Promise<PassagesPage> {
  const response = await apiClient.get<PassagesPage>(
    `${documentPath(baseName, documentId)}/passages`,
    { params: withTenantScope({ offset: page.offset, limit: page.limit }, tenantScope) },
  );
  return response.data;
}

/** Add a passage at the end of the document (201). */
export async function addPassage(
  baseName: string,
  documentId: string,
  body: PassageTextBody,
): Promise<Passage> {
  const response = await apiClient.post<Passage>(
    `${documentPath(baseName, documentId)}/passages`,
    body,
  );
  return response.data;
}

/** New text (with the revision it was read at) and / or the switch. 409
 *  ``revision_conflict`` carries the passage as it stands
 *  (:func:`passageConflict`). */
export async function updatePassage(
  baseName: string,
  documentId: string,
  passageId: string,
  body: PassagePatch,
): Promise<Passage> {
  const response = await apiClient.patch<Passage>(
    passagePath(baseName, documentId, passageId),
    body,
  );
  return response.data;
}

/** The passage's text history, oldest first. */
export async function listPassageRevisions(
  baseName: string,
  documentId: string,
  passageId: string,
  tenantScope?: TenantScope,
): Promise<PassageRevision[]> {
  const response = await apiClient.get<{ revisions: PassageRevision[] }>(
    `${passagePath(baseName, documentId, passageId)}/revisions`,
    { params: withTenantScope({}, tenantScope) },
  );
  return response.data.revisions;
}

/** Write ``revision``'s text back as a new revision; ``expectedRevision`` is
 *  the passage's revision as read (409 ``revision_conflict`` otherwise). */
export async function restorePassage(
  baseName: string,
  documentId: string,
  passageId: string,
  revision: number,
  expectedRevision: number,
): Promise<Passage> {
  const response = await apiClient.post<Passage>(
    `${passagePath(baseName, documentId, passageId)}/restore`,
    { revision, expected_revision: expectedRevision },
  );
  return response.data;
}

export async function getUnappliedEdits(
  baseName: string,
  documentId: string,
  tenantScope?: TenantScope,
): Promise<UnappliedEdits> {
  const response = await apiClient.get<UnappliedEdits>(
    `${documentPath(baseName, documentId)}/unapplied-edits`,
    { params: withTenantScope({}, tenantScope) },
  );
  return response.data;
}

/** Delete the unapplied edits' history (``manifest:delete``). Answers how
 *  many entries went — the unit the confirm counts (B-241; the response's
 *  ``revisions_deleted`` is the revision rows behind them). */
export async function discardUnappliedEdits(
  baseName: string,
  documentId: string,
): Promise<number> {
  const response = await apiClient.delete<{ discarded: number; revisions_deleted: number }>(
    `${documentPath(baseName, documentId)}/unapplied-edits`,
  );
  return response.data.discarded;
}

export async function batchDocuments(
  baseName: string,
  action: DocumentBatchAction,
  documentIds: string[],
): Promise<DocumentBatchResult> {
  const response = await apiClient.post<DocumentBatchResult>(
    `/v1/knowledge/bases/${encodeURIComponent(baseName)}/documents/batch`,
    { action, document_ids: documentIds },
  );
  return response.data;
}

/** ``manifest:delete``. */
export async function batchDeleteDocuments(
  baseName: string,
  documentIds: string[],
): Promise<DocumentBatchResult> {
  const response = await apiClient.post<DocumentBatchResult>(
    `/v1/knowledge/bases/${encodeURIComponent(baseName)}/documents/batch-delete`,
    { document_ids: documentIds },
  );
  return response.data;
}

export async function getStaleDocuments(
  baseName: string,
  tenantScope?: TenantScope,
): Promise<StaleDocuments> {
  const response = await apiClient.get<StaleDocuments>(
    `/v1/knowledge/bases/${encodeURIComponent(baseName)}/stale-documents`,
    { params: withTenantScope({}, tenantScope) },
  );
  return response.data;
}

/** Re-ingest the stale documents that kept their file (202). */
export async function reingestStale(baseName: string): Promise<ReingestStaleResult> {
  const response = await apiClient.post<ReingestStaleResult>(
    `/v1/knowledge/bases/${encodeURIComponent(baseName)}/reingest-stale`,
  );
  return response.data;
}

/** The passage as it stands when a text write was refused because someone
 *  changed it since it was read (409 ``revision_conflict``); ``null`` for
 *  any other error. */
export function passageConflict(err: unknown): Passage | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null;
  if (err.details?.error !== "revision_conflict") return null;
  const passage = err.details.passage;
  return typeof passage === "object" && passage !== null ? (passage as Passage) : null;
}

/** The refusal code a passage write came back with (``revision_conflict`` /
 *  ``document_busy`` / ``passage_too_long`` / ``passage_empty``), if any. */
export function passageErrorCode(err: unknown): string | null {
  if (!(err instanceof ApiError)) return null;
  const code = err.details?.error;
  return typeof code === "string" ? code : null;
}
