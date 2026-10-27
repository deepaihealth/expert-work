/**
 * Segment-preview drawer — KB commercial uplift.
 *
 * Read-only paginated view of a document's chunks (the embedding is never
 * fetched). Lets an operator verify that chunking produced sensible segments.
 * B-209 PR-2: a picture description shows the original's thumbnail beside it.
 * B-206 K-1: a switch lists either the chunks (「按小段」, the default — each
 * names the parent row it belongs to) or the parent rows (「按整块」).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { App, Card, Drawer, Empty, Pagination, Segmented, Skeleton, Typography } from "antd";
import { useTranslation } from "react-i18next";

import {
  listChunks,
  type ChunkView,
  type KnowledgeChunk,
  type KnowledgeDocument,
} from "../../api/knowledge";
import { ApiError } from "../../api/client";
import { ChunkContent } from "./ChunkContent";
import { HeadingPath } from "./HeadingPath";

const { Text } = Typography;

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
  const [figureIds, setFigureIds] = useState<ReadonlySet<string>>(new Set());
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [view, setView] = useState<ChunkView>("children");
  const [loading, setLoading] = useState(false);
  // Only the latest read lands: a slower answer for the view (or page) just
  // left never replaces the one asked for after it.
  const latestRead = useRef(0);

  const documentId = document?.id ?? null;

  const load = useCallback(
    async (targetPage: number, targetView: ChunkView) => {
      if (!documentId) return;
      const read = ++latestRead.current;
      setLoading(true);
      try {
        const result = await listChunks(
          baseName,
          documentId,
          {
            offset: (targetPage - 1) * PAGE_SIZE,
            limit: PAGE_SIZE,
            view: targetView,
          },
          readScope,
        );
        if (read !== latestRead.current) return;
        setChunks(result.chunks);
        setFigureIds(new Set(result.figure_ids ?? []));
        setTotal(result.total);
      } catch (err) {
        if (read === latestRead.current) message.error(errMessage(err));
      } finally {
        if (read === latestRead.current) setLoading(false);
      }
    },
    [baseName, documentId, message, readScope],
  );

  useEffect(() => {
    // A newly opened document starts on the default view.
    setView("children");
    if (documentId) {
      setPage(1);
      void load(1, "children");
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
        <Segmented<ChunkView>
          size="small"
          value={view}
          onChange={(v) => {
            setView(v);
            setPage(1);
            void load(1, v);
          }}
          options={[
            { value: "children", label: t("knowledge_page.chunks_view_children") },
            { value: "parents", label: t("knowledge_page.chunks_view_parents") },
          ]}
          style={{ marginBottom: 12 }}
          data-testid="kb-chunks-view"
        />
        {loading ? (
          <Skeleton active paragraph={{ rows: 8 }} />
        ) : chunks.length === 0 ? (
          <Empty
            description={t(
              view === "parents" ? "knowledge_page.parents_empty" : "knowledge_page.chunks_empty",
            )}
          />
        ) : (
          <>
            {chunks.map((chunk) => (
              <Card
                key={chunk.id}
                size="small"
                style={{ marginBottom: 12 }}
                title={
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {t(
                      chunk.chunk_type === "parent"
                        ? "knowledge_page.parent_label"
                        : "knowledge_page.chunk_label",
                      { index: chunk.chunk_index },
                    )}
                    {chunk.parent_index != null && (
                      <span data-testid={`kb-chunk-parent-${chunk.id}`}>
                        {" · "}
                        {t("knowledge_page.chunk_in_parent", { index: chunk.parent_index })}
                      </span>
                    )}
                  </Text>
                }
              >
                <HeadingPath path={chunk.heading_path} testId={`kb-chunk-heading-${chunk.id}`} />
                {documentId && (
                  <ChunkContent
                    baseName={baseName}
                    documentId={documentId}
                    readScope={readScope}
                    content={chunk.content}
                    figureIds={figureIds}
                  />
                )}
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
                  void load(p, view);
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
