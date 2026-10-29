/**
 * B-222 PR-2 — the documents tab's estimate actions: confirm (one, a batch,
 * or the base's 「全部确认」 with the count and total the user saw), withdraw
 * (cancel an estimate: a new upload is deleted, anything else put back) and
 * 「仍入库」 for a duplicate. Each says what happened and calls ``onChanged``
 * so the table, the banner and the upload queue re-read.
 */
import { useCallback, useMemo, useState } from "react";
import { App } from "antd";
import { useTranslation } from "react-i18next";

import { ApiError, errMessage } from "../../api/client";
import {
  cancelDocuments,
  confirmAllDocuments,
  confirmDocuments,
  estimateDuplicate,
  listDocuments,
  type ConfirmResult,
  type EstimateSummary,
  type KnowledgeDocument,
} from "../../api/knowledge";

const WAITING_PAGE = 200;

export interface EstimateActions {
  busy: boolean;
  confirm: (documents: KnowledgeDocument[], skip?: KnowledgeDocument[]) => Promise<void>;
  confirmAll: (summary: EstimateSummary) => Promise<void>;
  withdraw: (documents: KnowledgeDocument[]) => Promise<void>;
  withdrawAll: () => Promise<void>;
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

  const withdrawIds = useCallback(
    async (ids: string[]) => {
      if (ids.length === 0) return;
      const results = await cancelDocuments(baseName, ids);
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
        if (pairs.length > 0) reportConfirmed(await confirmDocuments(baseName, pairs));
        await withdrawIds(skip.map((d) => d.id));
      }),
    [baseName, reportConfirmed, run, withdrawIds],
  );

  const confirmAll = useCallback(
    (summary: EstimateSummary) =>
      run(async () => {
        try {
          const result = await confirmAllDocuments(baseName, {
            count: summary.awaiting,
            totalMicros: summary.total_micros,
          });
          message.success(
            t("knowledge_page.estimate_confirmed", { count: result.confirmed.length }),
          );
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
    (documents: KnowledgeDocument[]) => run(() => withdrawIds(documents.map((d) => d.id))),
    [run, withdrawIds],
  );

  const withdrawAll = useCallback(
    () =>
      run(async () => {
        // Withdrawn documents leave the filter: read the first page again
        // until none is left (bounded).
        for (let round = 0; round < 50; round += 1) {
          const page = await listDocuments(
            baseName,
            { limit: WAITING_PAGE, offset: 0 },
            undefined,
            { status: ["estimating", "awaiting_confirmation"] },
          );
          if (page.documents.length === 0) return;
          await withdrawIds(page.documents.map((d) => d.id));
        }
      }),
    [baseName, run, withdrawIds],
  );

  const ingestAnyway = useCallback(
    (document: KnowledgeDocument) =>
      run(async () => {
        await estimateDuplicate(baseName, document.id);
        message.success(t("knowledge_page.estimate_ingest_anyway_started"));
      }),
    [baseName, message, run, t],
  );

  return useMemo(
    () => ({ busy, confirm, confirmAll, withdraw, withdrawAll, ingestAnyway }),
    [busy, confirm, confirmAll, withdraw, withdrawAll, ingestAnyway],
  );
}
