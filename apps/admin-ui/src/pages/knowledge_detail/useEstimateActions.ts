/**
 * B-222 PR-2 — the documents tab's estimate actions: confirm (one, a batch,
 * or the base's 「全部确认」 with the very estimates the user saw), withdraw
 * (cancel an estimate: a new upload is deleted, anything else put back) and
 * 「仍入库」 for a duplicate. Each says what happened and calls ``onChanged``
 * so the table, the banner and the upload queue re-read.
 *
 * Requests carry at most :data:`ESTIMATE_IDS_PER_REQUEST` ids (the server's
 * cap); a larger batch goes in several and the results are added up.
 * 「全部取消」 withdraws exactly the documents waiting when its confirm opened
 * (:func:`waitingDocuments`) — never one uploaded after — and a withdrawal
 * only touches a document still in the status it was read in (confirmed or
 * sent for 「仍入库」 since: left alone, ``status_changed``).
 */
import { useCallback, useMemo, useState } from "react";
import { App } from "antd";
import { useTranslation } from "react-i18next";

import { ApiError, errMessage } from "../../api/client";
import {
  cancelDocuments,
  confirmAllDocuments,
  confirmDocuments,
  ESTIMATE_IDS_PER_REQUEST,
  estimateDuplicate,
  listDocuments,
  type CancelResult,
  type ConfirmResult,
  type DocumentStatus,
  type EstimateSummary,
  type KnowledgeDocument,
} from "../../api/knowledge";

/** Pages of waiting documents read for 「全部取消」 at most (200 each). */
const MAX_WAITING_PAGES = 50;

/** A document as read for a withdrawal: which one, and the status it was in. */
export type WaitingDocument = Pick<KnowledgeDocument, "id" | "status">;

export function inChunks<T>(items: T[], size: number = ESTIMATE_IDS_PER_REQUEST): T[][] {
  const chunks: T[][] = [];
  for (let start = 0; start < items.length; start += size) {
    chunks.push(items.slice(start, start + size));
  }
  return chunks;
}

/** The statuses a withdrawal of a document read in ``status`` still applies
 *  to: an estimate under way may have finished meanwhile; anything else must
 *  be as it was read (a confirmed one is ``pending`` now, a duplicate sent for
 *  「仍入库」 ``estimating``). */
export function withdrawableFrom(status: DocumentStatus): DocumentStatus[] {
  return status === "estimating" ? ["estimating", "awaiting_confirmation"] : [status];
}

export interface EstimateActions {
  busy: boolean;
  confirm: (documents: KnowledgeDocument[], skip?: KnowledgeDocument[]) => Promise<void>;
  confirmAll: (summary: EstimateSummary) => Promise<void>;
  withdraw: (documents: WaitingDocument[]) => Promise<void>;
  /** What 「全部取消」 will withdraw: the documents waiting now. */
  waitingDocuments: () => Promise<WaitingDocument[]>;
  ingestAnyway: (document: KnowledgeDocument) => Promise<void>;
}

export function useEstimateActions(
  baseName: string,
  onChanged: () => Promise<void> | void,
): EstimateActions {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [busy, setBusy] = useState(false);

  const run = useCallback(
    async (work: () => Promise<void>) => {
      setBusy(true);
      try {
        await work();
      } catch (err) {
        message.error(errMessage(err));
      } finally {
        setBusy(false);
        await onChanged();
      }
    },
    [message, onChanged],
  );

  const reportConfirmed = useCallback(
    (results: ConfirmResult[]) => {
      const confirmed = results.filter((r) => r.outcome === "confirmed").length;
      if (confirmed > 0) message.success(t("knowledge_page.estimate_confirmed", { count: confirmed }));
      if (results.some((r) => r.outcome === "estimate_changed")) {
        message.warning(t("knowledge_page.estimate_changed"));
      }
      if (results.some((r) => r.outcome === "expired")) {
        message.warning(t("knowledge_page.estimate_expired"));
      }
    },
    [message, t],
  );

  const cancelAll = useCallback(
    async (documents: WaitingDocument[]) => {
      if (documents.length === 0) return;
      const groups = new Map<string, { expect: DocumentStatus[]; ids: string[] }>();
      for (const document of documents) {
        const expect = withdrawableFrom(document.status);
        const key = expect.join(",");
        const group = groups.get(key) ?? { expect, ids: [] };
        group.ids.push(document.id);
        groups.set(key, group);
      }
      const results: CancelResult[] = [];
      for (const { expect, ids } of groups.values()) {
        for (const chunk of inChunks(ids)) {
          results.push(...(await cancelDocuments(baseName, chunk, expect)));
        }
      }
      const withdrawn = results.filter(
        (r) => r.outcome === "withdrawn" || r.outcome === "withdrawn_deleted",
      ).length;
      if (withdrawn > 0) message.success(t("knowledge_page.estimate_withdrawn", { count: withdrawn }));
      const changed = results.filter((r) => r.outcome === "status_changed").length;
      if (changed > 0) {
        message.warning(t("knowledge_page.estimate_withdraw_changed", { count: changed }));
      }
    },
    [baseName, message, t],
  );

  const confirm = useCallback(
    (documents: KnowledgeDocument[], skip: KnowledgeDocument[] = []) =>
      run(async () => {
        const pairs = documents
          .filter((d) => d.ingest_estimate)
          .map((d) => ({ documentId: d.id, estimateId: d.ingest_estimate!.id }));
        const results: ConfirmResult[] = [];
        for (const chunk of inChunks(pairs)) {
          results.push(...(await confirmDocuments(baseName, chunk)));
        }
        if (results.length > 0) reportConfirmed(results);
        await cancelAll(skip);
      }),
    [baseName, cancelAll, reportConfirmed, run],
  );

  const confirmAll = useCallback(
    (summary: EstimateSummary) =>
      run(async () => {
        try {
          const result = await confirmAllDocuments(baseName, {
            count: summary.awaiting,
            totalMicros: summary.total_micros,
            fingerprint: summary.fingerprint ?? "",
          });
          message.success(
            t("knowledge_page.estimate_confirmed", { count: result.confirmed.length }),
          );
          if ((result.skipped ?? []).length > 0) message.warning(t("knowledge_page.estimate_expired"));
        } catch (err) {
          if (err instanceof ApiError && err.status === 409) {
            message.warning(t("knowledge_page.estimate_all_changed"));
            return;
          }
          throw err;
        }
      }),
    [baseName, message, run, t],
  );

  const withdraw = useCallback(
    (documents: WaitingDocument[]) => run(() => cancelAll(documents)),
    [cancelAll, run],
  );

  const waitingDocuments = useCallback(async () => {
    const found = new Map<string, WaitingDocument>();
    for (let page = 0; page < MAX_WAITING_PAGES; page += 1) {
      const listed = await listDocuments(
        baseName,
        { limit: ESTIMATE_IDS_PER_REQUEST, offset: page * ESTIMATE_IDS_PER_REQUEST },
        undefined,
        { status: ["estimating", "awaiting_confirmation"] },
      );
      for (const d of listed.documents) found.set(d.id, { id: d.id, status: d.status });
      if (found.size >= listed.total || listed.documents.length === 0) break;
    }
    return Array.from(found.values());
  }, [baseName]);

  const ingestAnyway = useCallback(
    (document: KnowledgeDocument) =>
      run(async () => {
        try {
          await estimateDuplicate(baseName, document.id);
          message.success(t("knowledge_page.estimate_ingest_anyway_started"));
        } catch (err) {
          if (err instanceof ApiError && err.status === 409) {
            message.warning(
              t(
                err.details?.error === "document_busy"
                  ? "knowledge_page.document_busy"
                  : "knowledge_page.estimate_ingest_anyway_changed",
              ),
            );
            return;
          }
          throw err;
        }
      }),
    [baseName, message, run, t],
  );

  return useMemo(
    () => ({ busy, confirm, confirmAll, withdraw, waitingDocuments, ingestAnyway }),
    [busy, confirm, confirmAll, withdraw, waitingDocuments, ingestAnyway],
  );
}
