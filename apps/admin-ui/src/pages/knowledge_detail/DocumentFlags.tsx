/**
 * Flags next to a document's name — B-175 P3-PR3: 「已停用」 (recall leaves
 * it out) and 「待复核 N」 (passages a re-ingest switched off by inheritance
 * plus edits it could not put back; clicking opens the drawer on 待复核, also
 * for a read-only viewer).
 */
import type { KeyboardEvent, ReactElement } from "react";
import { Tag, Tooltip } from "antd";
import { useTranslation } from "react-i18next";

import type { KnowledgeDocument } from "../../api/knowledge";

interface DocumentFlagsProps {
  document: KnowledgeDocument;
  onReview: (document: KnowledgeDocument) => void;
}

export function DocumentFlags({ document, onReview }: DocumentFlagsProps): ReactElement | null {
  const { t } = useTranslation();
  const waiting = (document.review?.inherited ?? 0) + (document.review?.unapplied ?? 0);
  const off = document.enabled === false;
  if (!off && waiting === 0) return null;
  const open = () => onReview(document);
  return (
    <>
      {off && (
        <Tooltip title={t("kb_maintenance.doc_disabled_hint")}>
          <Tag bordered={false} tabIndex={0} data-testid={`doc-disabled-${document.id}`}>
            {t("kb_maintenance.doc_disabled")}
          </Tag>
        </Tooltip>
      )}
      {waiting > 0 && (
        <Tooltip title={t("kb_maintenance.doc_review_hint")}>
          <Tag
            color="orange"
            bordered={false}
            role="button"
            tabIndex={0}
            style={{ cursor: "pointer" }}
            onClick={open}
            onKeyDown={(event: KeyboardEvent) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                open();
              }
            }}
            data-testid={`doc-review-${document.id}`}
          >
            {t("kb_maintenance.doc_review_tag", { n: waiting })}
          </Tag>
        </Tooltip>
      )}
    </>
  );
}
