/**
 * What a refused passage write means for the console — B-175 P3-PR3.
 *
 * ``conflict``: someone changed the passage since it was read (409
 * ``revision_conflict``; the server's copy comes with it). ``gone``: the
 * passage is no longer there (404, or a conflict without a copy — a re-ingest
 * replaced it). Anything else is a message to show.
 */
import type { TFunction } from "i18next";

import { ApiError, errMessage } from "../../../api/client";
import {
  passageConflict,
  passageErrorCode,
  type Passage,
} from "../../../api/knowledge_maintenance";

export type PassageWriteError =
  | { kind: "conflict"; passage: Passage }
  | { kind: "gone" }
  | { kind: "message"; text: string };

export function passageWriteError(t: TFunction, err: unknown): PassageWriteError {
  const theirs = passageConflict(err);
  if (theirs !== null) return { kind: "conflict", passage: theirs };
  const code = passageErrorCode(err);
  if (code === "revision_conflict") return { kind: "gone" };
  if (err instanceof ApiError && err.status === 404) return { kind: "gone" };
  if (code === "document_busy") return { kind: "message", text: t("kb_maintenance.error_busy") };
  if (code === "passage_empty") return { kind: "message", text: t("kb_maintenance.error_empty") };
  if (code === "passage_too_long") {
    const limit = err instanceof ApiError ? err.details?.limit_tokens : undefined;
    return { kind: "message", text: t("kb_maintenance.error_too_long", { limit: limit ?? "?" }) };
  }
  if (err instanceof ApiError && err.status === 503) {
    return { kind: "message", text: t("knowledge_page.embedder_missing") };
  }
  return { kind: "message", text: errMessage(err) };
}
