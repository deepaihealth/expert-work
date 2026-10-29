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
 * (:func:`waitingIds`) — never one uploaded after.
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
  type EstimateSummary,
  type KnowledgeDocument,
} from "../../api/knowledge";

/** Pages of waiting documents read for 「全部取消」 at most (200 each). */
const MAX_WAITING_PAGES = 50;

export function inChunks<T>(items: T[], size: number = ESTIMATE_IDS_PER_REQUEST): T[][] {
  const chunks: T[][] = [];
  for (let start = 0; start < items.length; start += size) {
    chunks.push(items.slice(start, start + size));
  }
  return chunks;
}

export interface EstimateActions {
  busy: boolean;
  confirm: (documents: KnowledgeDocument[], skip?: KnowledgeDocument[]) => Promise<void>;
  confirmAll: (summary: EstimateSummary) => Promise<void>;
  withdraw: (documents: KnowledgeDocument[]) => Promise<void>;
  /** The ids 「全部取消」 will withdraw: those waiting now. */
  waitingIds: () => Promise<string[]>;
  withdrawIds: (ids: string[]) => Promise<void>;
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
    async (ids: string[]) => {
      if (ids.length === 0) return;
      const results: CancelResult[] = [];
      for (const chunk of inChunks(ids)) results.push(...(await cancelDocuments(baseName, chunk)));
      const withdrawn = results.filter(
        (r) => r.outcome === "withdrawn" || r.outcome === "withdrawn_deleted",
      ).length;
      if (withdrawn > 0) message.success(t("knowledge_page.estimate_withdrawn", { count: withdrawn }));
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
        await cancelAll(skip.map((d) => d.id));
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
    (documents: KnowledgeDocument[]) => run(() => cancelAll(documents.map((d) => d.id))),
    [cancelAll, run],
  );

  const waitingIds = useCallback(async () => {
    const ids: string[] = [];
    for (let page = 0; page < MAX_WAITING_PAGES; page += 1) {
      const found = await listDocuments(
        baseName,
        { limit: ESTIMATE_IDS_PER_REQUEST, offset: page * ESTIMATE_IDS_PER_REQUEST },
        undefined,
        { status: ["estimating", "awaiting_confirmation"] },
      );
      ids.push(...found.documents.map((d) => d.id));
      if (ids.length >= found.total || found.documents.length === 0) break;
    }
    return Array.from(new Set(ids));
  }, [baseName]);

  const withdrawIds = useCallback((ids: string[]) => run(() => cancelAll(ids)), [cancelAll, run]);

  const ingestAnyway = useCallback(
    (document: KnowledgeDocument) =>
      run(async () => {
        await estimateDuplicate(baseName, document.id);
        message.success(t("knowledge_page.estimate_ingest_anyway_started"));
      }),
    [baseName, message, run, t],
  );

  return useMemo(
    () => ({ busy, confirm, confirmAll, withdraw, waitingIds, withdrawIds, ingestAnyway }),
    [busy, confirm, confirmAll, withdraw, waitingIds, withdrawIds, ingestAnyway],
  );
}
