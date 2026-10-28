/**
 * The document a hit belongs to — B-175 P2-PR4 (B-206 §6): short id
 * ``D{doc_seq}``, title, file and upload time, as the document is now. A
 * document deleted since says so; an older backend that sends no document
 * renders nothing.
 */
import { Tag, Typography } from "antd";
import { useTranslation } from "react-i18next";

import type { KnowledgeHitDocument } from "../../api/knowledge";

const { Text } = Typography;

/** ``D12 · Title`` (or the file name when there is no title). */
export function documentLabel(document: KnowledgeHitDocument): string {
  const name = document.title || document.filename;
  return document.doc_seq != null ? `D${document.doc_seq} · ${name}` : name;
}

export function HitDocument({
  document,
  testId,
}: {
  document: KnowledgeHitDocument | null | undefined;
  /** The line's testid; ``${testId}-deleted`` for a deleted document. */
  testId: string;
}) {
  const { t } = useTranslation();
  if (document === undefined) return null;
  if (document === null) {
    return (
      <Tag bordered={false} data-testid={`${testId}-deleted`}>
        {t("knowledge_retrieval.doc_deleted")}
      </Tag>
    );
  }
  const parts = [documentLabel(document)];
  if (document.title && document.title !== document.filename) parts.push(document.filename);
  if (document.created_at) {
    parts.push(
      t("knowledge_retrieval.uploaded_at", {
        time: new Date(document.created_at).toLocaleString(),
      }),
    );
  }
  return (
    <Text type="secondary" style={{ fontSize: 12 }} data-testid={testId}>
      {parts.join(" · ")}
    </Text>
  );
}
