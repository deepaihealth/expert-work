/**
 * A failed document's reason as the console shows it. The server records an
 * English ``error`` plus a machine-readable ``error_code``; a reason the user
 * has to act on is rendered in the console's language from the code — the
 * rest is shown as recorded.
 */
import type { TFunction } from "i18next";

import type { KnowledgeDocument } from "../../api/knowledge";

/** The numbers in a ``text_too_large`` refusal — the same pattern as
 *  ``TOO_LARGE_PATTERN`` in ``control_plane/knowledge/parsed_size.py``. */
const TOO_LARGE = /([\d,]+) characters, over the per-document limit of ([\d,]+)/;

function count(digits: string): number {
  return Number(digits.replace(/,/g, ""));
}

export function documentErrorText(
  t: TFunction,
  language: string,
  document: Pick<KnowledgeDocument, "error" | "error_code">,
): string | null {
  if (document.error_code !== "text_too_large") return document.error;
  const match = document.error ? TOO_LARGE.exec(document.error) : null;
  if (!match) return t("knowledge_page.error_text_too_large_unknown");
  // Exact, grouped: rounded, a document just over the limit would read as
  // the same size as the limit.
  const number = new Intl.NumberFormat(language);
  return t("knowledge_page.error_text_too_large", {
    size: number.format(count(match[1])),
    limit: number.format(count(match[2])),
  });
}
