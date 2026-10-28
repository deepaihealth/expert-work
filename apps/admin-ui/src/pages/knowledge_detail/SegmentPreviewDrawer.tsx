/**
 * Segment-preview drawer — KB commercial uplift.
 *
 * A document's text as retrieval sees it (the embedding is never fetched).
 * B-209 PR-2: a picture description shows the original's thumbnail beside it.
 * B-175 P3-PR3: three views —
 *
 * * 「按整块」 (default): the passages, each with its chunks folded below; switch
 *   one on / off, edit it (against the revision it was read at), see and
 *   restore its history, add one at the end;
 * * 「按小段」: the searchable chunks, each naming the passage it belongs to
 *   (B-206 K-1), read-only;
 * * 「待复核」: what a re-ingest left for a person — passages it switched off by
 *   inheritance, edits it could not put back. Its count is read on opening; an
 *   alert above the other views points to it.
 *
 * Writes are disabled while read-only (cross-tenant) and while the document
 * is being ingested.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, App, Button, Card, Drawer, Empty, Pagination, Segmented, Skeleton, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { listChunks, type KnowledgeChunk, type KnowledgeDocument } from "../../api/knowledge";
import {
  getUnappliedEdits,
  type Passage,
  type UnappliedEdits,
} from "../../api/knowledge_maintenance";
import { ApiError } from "../../api/client";
import { ChunkContent } from "./ChunkContent";
import { HeadingPath } from "./HeadingPath";
import { PassageEditModal, type EditTarget } from "./passages/PassageEditModal";
import { PassageHistory } from "./passages/PassageHistory";
import { PassageList } from "./passages/PassageList";
import { ReviewPanel } from "./passages/ReviewPanel";

const { Text } = Typography;

const PAGE_SIZE = 20;
const NO_FIGURES: ReadonlySet<string> = new Set();

/** Which list the drawer shows. */
export type DrawerView = "passages" | "children" | "review";

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
  /** Cross-tenant read-only: everything shows, every write is disabled. */
  readonly?: boolean;
  /** ``manifest:delete`` — may discard unapplied edits. */
  canDelete?: boolean;
  /** The view a newly opened document starts on (the row's 「待复核」 opens 待复核). */
  initialView?: DrawerView;
}

export function SegmentPreviewDrawer({
  baseName,
  readScope,
  document,
  onClose,
  readonly = false,
  canDelete = false,
  initialView = "passages",
}: SegmentPreviewDrawerProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();

  const [view, setView] = useState<DrawerView>(initialView);
  // 「按小段」 — the searchable chunks, read-only.
  const [chunks, setChunks] = useState<KnowledgeChunk[]>([]);
  const [figureIds, setFigureIds] = useState<ReadonlySet<string>>(new Set());
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  // 「待复核」 — read on opening, again after every change.
  const [review, setReview] = useState<UnappliedEdits | null>(null);
  // Bumped when passages changed outside the list (editor, history, review).
  const [listVersion, setListVersion] = useState(0);
  const [editing, setEditing] = useState<EditTarget>(null);
  const [history, setHistory] = useState<Passage | null>(null);
  // Only the latest read lands: a slower answer for the page just left
  // never replaces the one asked for after it.
  const latestRead = useRef(0);
  const latestReview = useRef(0);

  const documentId = document?.id ?? null;
  const busy = document !== null && (document.status !== "ready" || Boolean(document.has_pending));

  const loadChunks = useCallback(
    async (targetPage: number) => {
      if (!documentId) return;
      const read = ++latestRead.current;
      setLoading(true);
      try {
        const result = await listChunks(
          baseName,
          documentId,
          { offset: (targetPage - 1) * PAGE_SIZE, limit: PAGE_SIZE, view: "children" },
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

  const loadReview = useCallback(async () => {
    if (!documentId) return;
    const read = ++latestReview.current;
    try {
      const found = await getUnappliedEdits(baseName, documentId, readScope);
      if (read === latestReview.current) setReview(found);
    } catch (err) {
      if (read === latestReview.current) message.error(errMessage(err));
    }
  }, [baseName, documentId, message, readScope]);

  useEffect(() => {
    // A newly opened document starts on the view asked for, page 1.
    setView(initialView);
    setPage(1);
    setChunks([]);
    setTotal(0);
    setReview(null);
    setEditing(null);
    setHistory(null);
    if (documentId) {
      void loadReview();
      if (initialView === "children") void loadChunks(1);
    }
    // ``initialView`` is read once per opened document.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [documentId, loadChunks, loadReview]);

  const changed = useCallback(() => {
    setListVersion((v) => v + 1);
    void loadReview();
  }, [loadReview]);

  const reviewCount = review ? review.inherited.length + review.unapplied.length : 0;

  return (
    <Drawer
      open={document !== null}
      onClose={onClose}
      width={720}
      title={t("knowledge_page.chunks_drawer_title", { name: document?.filename ?? "" })}
    >
      <div data-testid="kb-chunks-drawer">
        <Segmented<DrawerView>
          size="small"
          value={view}
          onChange={(v) => {
            setView(v);
            if (v === "children") {
              setPage(1);
              void loadChunks(1);
            }
          }}
          options={[
            { value: "passages", label: t("knowledge_page.chunks_view_parents") },
            { value: "children", label: t("knowledge_page.chunks_view_children") },
            { value: "review", label: t("kb_maintenance.view_review", { n: reviewCount }) },
          ]}
          style={{ marginBottom: 12 }}
          data-testid="kb-chunks-view"
        />
        {reviewCount > 0 && view !== "review" && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message={t("kb_maintenance.review_alert", { count: reviewCount })}
            data-testid="kb-review-alert"
            action={
              <Button size="small" onClick={() => setView("review")} data-testid="kb-review-open">
                {t("kb_maintenance.review_open")}
              </Button>
            }
          />
        )}
        {documentId && view === "passages" && (
          <PassageList
            baseName={baseName}
            documentId={documentId}
            readScope={readScope}
            readonly={readonly}
            busy={busy}
            version={listVersion}
            onEdit={(passage) => setEditing({ mode: "edit", passage })}
            onHistory={setHistory}
            onAdd={() => setEditing({ mode: "add" })}
            onSwitched={() => void loadReview()}
          />
        )}
        {documentId && view === "review" &&
          (review === null ? (
            <Skeleton active paragraph={{ rows: 4 }} />
          ) : (
            <ReviewPanel
              baseName={baseName}
              documentId={documentId}
              readScope={readScope}
              review={review}
              // The review read carries no picture ids: its cards show text only.
              figureIds={NO_FIGURES}
              readonly={readonly}
              busy={busy}
              canDelete={canDelete}
              onChanged={changed}
              onEdit={(passage) => setEditing({ mode: "edit", passage })}
              onHistory={setHistory}
            />
          ))}
        {view === "children" &&
          (loading ? (
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
                    void loadChunks(p);
                  }}
                  style={{ marginTop: 8, textAlign: "right" }}
                />
              )}
            </>
          ))}
      </div>
      {documentId && (
        <>
          <PassageEditModal
            baseName={baseName}
            documentId={documentId}
            target={editing}
            onClose={() => setEditing(null)}
            onSaved={changed}
            onConflict={() => setListVersion((v) => v + 1)}
            onGone={changed}
          />
          <PassageHistory
            baseName={baseName}
            documentId={documentId}
            readScope={readScope}
            passage={history}
            readonly={readonly}
            busy={busy}
            onClose={() => setHistory(null)}
            onChanged={changed}
            onGone={changed}
          />
        </>
      )}
    </Drawer>
  );
}
