/**
 * Segment-preview drawer — KB commercial uplift.
 *
 * Read-only paginated view of a document's chunks (the embedding is never
 * fetched). Lets an operator verify that chunking produced sensible segments.
 */
import { useCallback, useEffect, useState } from "react";
import { App, Card, Drawer, Empty, Pagination, Skeleton, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { listChunks, type KnowledgeChunk, type KnowledgeDocument } from "../../api/knowledge";
import { ApiError } from "../../api/client";

const { Text, Paragraph } = Typography;

const PAGE_SIZE = 20;

function errMessage(err: unknown): string {
  return err instanceof ApiError
    ? `${err.code}: ${err.message}`
    : err instanceof Error
      ? err.message
      : "unknown error";
}

interface SegmentPreviewDrawerProps {
  baseName: string;
  /** Cross-tenant W4(review C-2)— 权威读口径:URL ``?tenant_id=`` 原样透
   *  传优先;无 URL 参数时取 ambient scope("*" 折叠成 undefined),由
   *  KnowledgeDetail 经 DocumentsTab 统一下传。 */
  readScope: string | undefined;
  document: KnowledgeDocument | null;
  onClose: () => void;
}

export function SegmentPreviewDrawer({
  baseName,
  readScope,
  document,
  onClose,
}: SegmentPreviewDrawerProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();

  const [chunks, setChunks] = useState<KnowledgeChunk[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);

  const documentId = document?.id ?? null;

  const load = useCallback(
    async (targetPage: number) => {
      if (!documentId) return;
      setLoading(true);
      try {
        const result = await listChunks(
          baseName,
          documentId,
          {
            offset: (targetPage - 1) * PAGE_SIZE,
            limit: PAGE_SIZE,
          },
          readScope,
        );
        setChunks(result.chunks);
        setTotal(result.total);
      } catch (err) {
        message.error(errMessage(err));
      } finally {
        setLoading(false);
      }
    },
    [baseName, documentId, message, readScope],
  );

  useEffect(() => {
    if (documentId) {
      setPage(1);
      void load(1);
    } else {
      setChunks([]);
      setTotal(0);
    }
  }, [documentId, load]);

  return (
    <Drawer
      open={document !== null}
      onClose={onClose}
      width={640}
      title={t("knowledge_page.chunks_drawer_title", { name: document?.filename ?? "" })}
    >
      <div data-testid="kb-chunks-drawer">
        {loading ? (
          <Skeleton active paragraph={{ rows: 8 }} />
        ) : chunks.length === 0 ? (
          <Empty description={t("knowledge_page.chunks_empty")} />
        ) : (
          <>
            {chunks.map((chunk) => (
              <Card
                key={chunk.id}
                size="small"
                style={{ marginBottom: 12 }}
                title={
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {t("knowledge_page.chunk_label", { index: chunk.chunk_index })}
                  </Text>
                }
              >
                <Paragraph style={{ whiteSpace: "pre-wrap", marginBottom: 0, fontSize: 13 }}>
                  {chunk.content}
                </Paragraph>
              </Card>
            ))}
            {total > PAGE_SIZE && (
              <Pagination
                current={page}
                total={total}
                pageSize={PAGE_SIZE}
                showSizeChanger={false}
                onChange={(p) => {
                  setPage(p);
                  void load(p);
                }}
                style={{ marginTop: 8, textAlign: "right" }}
              />
            )}
          </>
        )}
      </div>
    </Drawer>
  );
}
