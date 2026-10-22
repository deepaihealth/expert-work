import type { TFunction } from "i18next";

/** B-206 K-7b — the notice for a rerank-score threshold set under another rerank
 *  model (``calibrated``) than the one scoring now (``current``): the settings
 *  tab names the platform's, the retrieval test the one that scored its search. */
export function thresholdPausedNotice(
  t: TFunction,
  calibrated: string | null | undefined,
  current: string | null | undefined,
): string {
  return t("knowledge_page.rerank_threshold_paused", {
    calibrated: calibrated ?? t("knowledge_page.rerank_threshold_model_unrecorded"),
    current: current ?? "—",
  });
}
