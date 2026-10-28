/**
 * Documents tab — KB commercial uplift.
 *
 * Rich document table (status with localized labels, chunk count, attempts,
 * updated) + drag-drop multi-file upload + per-document actions (view chunks /
 * re-ingest / delete). Ingest progress is a conditional 5s poll (Mini-ADR
 * H-18): poll while any document is non-terminal, stop once all settle.
 *
 * B-175 L3: uploads go through ``useUploadQueue`` (concurrency 3, per-file
 * progress / cancel / retry, unsupported + oversized files and names
 * repeated inside the batch skipped up front). A name already in the base is
 * an update, not a duplicate: the new bytes wait in the pending slot while
 * the previous version keeps serving, and the row shows that marker plus a
 * "discard new version" action. The table pages server-side (50 per page;
 * the poll refetches only the current page, so a row ingesting on another
 * page shows its new status only once the user turns to that page). ``beforeUpload`` enqueues the
 * whole antd batch once and returns ``LIST_IGNORE`` so antd keeps no list
 * of its own; the table refreshes at most every 15 s while a batch runs and
 * once more when it settles. The Dragger carries no ``accept`` — rc-upload would filter
 * unsupported files before the queue can count them. Folders: the toggle
 * flips the picker to ``webkitdirectory``; while a drag is over the dropzone
 * the Dragger is switched to directory mode too, so rc-upload expands dropped
 * folders via ``webkitGetAsEntry``. The drag flag is set on ``dragover`` and
 * cleared by an enter/leave counter plus a window-level ``drop`` / ``dragend``
 * listener (Safari leaves ``relatedTarget`` null on leave).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  App,
  Button,
  Empty,
  Popconfirm,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  Upload,
} from "antd";
import type { TableColumnsType, TablePaginationConfig } from "antd";
import { CircleStop, Eye, RefreshCw, ScanText, Trash2, Undo2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  cancelDocuments,
  deleteDocument,
  discardPendingDocument,
  KNOWLEDGE_DOCUMENT_MAX_BYTES,
  listDocuments,
  recognizeDocument,
  reingestDocument,
  uploadDocument,
  type DocumentStatus,
  type KnowledgeDocument,
  type UploadDocumentOptions,
  type UploadedDocument,
} from "../../api/knowledge";
import { ApiError } from "../../api/client";
import { formatBytes } from "../../components/console/workspace_format";
import { DEFAULT_UPLOAD_STALL_TIMEOUT_MS, useUploadQueue } from "../../hooks/useUploadQueue";
import { documentErrorText } from "./documentError";
import { FigureStatsTags, figuresNeedDescription } from "./FigureStatsTags";
import { needsRecognition, PageRecognitionTags } from "./PageRecognitionTags";
import { SegmentPreviewDrawer } from "./SegmentPreviewDrawer";
import { UploadQueuePanel } from "./UploadQueuePanel";
import { ReadonlyTooltip } from "../../components/ReadonlyTooltip";

const { Text } = Typography;

const STATUS_COLOR: Record<DocumentStatus, string> = {
  pending: "default",
  processing: "processing",
  ready: "success",
  failed: "error",
  cancelled: "warning",
};

/** B-222 — the statuses an ingest can still be cancelled from. */
function isCancellable(status: DocumentStatus): boolean {
  return status === "pending" || status === "processing";
}

const POLL_INTERVAL_MS = 5_000;
const DOC_TABLE_PAGE_SIZE = 50;
// Spread into the pagination config: ``data-*`` isn't in antd's prop type,
// but rc-pagination forwards it onto the <ul> (``pickAttrs`` data:true).
const DOC_TABLE_PAGINATION_TESTID = { "data-testid": "doc-table-pagination" };

function errMessage(err: unknown): string {
  return err instanceof ApiError
    ? `${err.code}: ${err.message}`
    : err instanceof Error
      ? err.message
      : "unknown error";
}

interface DocumentsTabProps {
  baseName: string;
  /** Cross-tenant W4(review C-2)— 权威读口径:URL ``?tenant_id=`` 原样透
   *  传优先;无 URL 参数时取 ambient scope("*" 折叠成 undefined),由
   *  KnowledgeDetail 统一下传。 */
  readScope: string | undefined;
  /** Cross-tenant W4(review C-2)— 只读态(切入态 ∪ "*" 聚合深链外租户
   *  读),由 KnowledgeDetail 统一判定下传;上传/重入库/删除一律置灰。 */
  readonly: boolean;
}

export function DocumentsTab({ baseName, readScope, readonly }: DocumentsTabProps) {
  const { t, i18n } = useTranslation();
  const { message } = App.useApp();

  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [total, setTotal] = useState(0);
  /** 1-based page of the server-side paged table. */
  const [page, setPage] = useState(1);
  // Another base (or tenant scope) starts again on page 1 — reset during
  // render so the fetch below never runs with the previous base's page.
  const pageOwner = `${readScope ?? ""}|${baseName}`;
  const [pageOwnerSeen, setPageOwnerSeen] = useState(pageOwner);
  if (pageOwnerSeen !== pageOwner) {
    setPageOwnerSeen(pageOwner);
    setPage(1);
  }
  /** Only the latest list request may write the table (page flips and the
   *  poll can overlap). */
  const requestSeqRef = useRef(0);
  const [loading, setLoading] = useState(false);
  const [previewDoc, setPreviewDoc] = useState<KnowledgeDocument | null>(null);
  const [folderMode, setFolderMode] = useState(false);
  const [dragging, setDragging] = useState(false);
  /** Nested dragenter/dragleave pairs over the dropzone's children. */
  const dragDepthRef = useRef(0);
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  /** antd calls ``beforeUpload`` once per file with the same batch array —
   *  remember the last batch so the whole pick is enqueued exactly once. */
  const lastBatchRef = useRef<readonly File[] | null>(null);

  const refresh = useCallback(
    async ({ quiet = false }: { quiet?: boolean } = {}) => {
      if (!quiet) setLoading(true);
      const seq = ++requestSeqRef.current;
      try {
        const result = await listDocuments(
          baseName,
          { limit: DOC_TABLE_PAGE_SIZE, offset: (page - 1) * DOC_TABLE_PAGE_SIZE },
          readScope,
        );
        if (seq !== requestSeqRef.current) return;
        // The page emptied under us (last rows deleted elsewhere) → step
        // back to the last page that still has rows; that refetches.
        const lastPage = Math.max(1, Math.ceil(result.total / DOC_TABLE_PAGE_SIZE));
        if (result.documents.length === 0 && page > lastPage) {
          setPage(lastPage);
          return;
        }
        setDocuments(result.documents);
        setTotal(result.total);
      } catch (err) {
        if (seq === requestSeqRef.current) message.error(errMessage(err));
      } finally {
        // Whichever request is latest clears the spinner — a quiet poll that
        // overtook a loud fetch must not leave it spinning.
        if (seq === requestSeqRef.current) setLoading(false);
      }
    },
    [baseName, message, page, readScope],
  );

  useEffect(() => {
    void refresh();
  }, [refresh]);


  const hasActiveIngest = useMemo(
    () => documents.some((d) => d.status === "pending" || d.status === "processing"),
    [documents],
  );

  useEffect(() => {
    if (hasActiveIngest) {
      pollTimer.current = setInterval(() => {
        // Same page-visibility gate as ``useStatusPolling``: a backgrounded
        // tab keeps the timer but skips the fetch until the user is back.
        if (typeof document !== "undefined" && document.visibilityState !== "visible") return;
        void refresh({ quiet: true });
      }, POLL_INTERVAL_MS);
      return () => {
        if (pollTimer.current !== null) clearInterval(pollTimer.current);
        pollTimer.current = null;
      };
    }
    return undefined;
  }, [hasActiveIngest, refresh]);

  /** Queue adapter: one upload with the request-level errors turned into
   *  the row's message (503 → embedder missing, 413 → server's own cap). An
   *  abort rejection is rethrown as-is; the queue ignores it. */
  const uploadOne = useCallback(
    async (file: File, opts: Required<UploadDocumentOptions>): Promise<UploadedDocument> => {
      try {
        return await uploadDocument(baseName, file, opts);
      } catch (err) {
        if (opts.signal.aborted) throw err;
        if (err instanceof ApiError && err.status === 503) {
          throw new Error(t("knowledge_page.embedder_missing"));
        }
        if (err instanceof ApiError && err.status === 413) {
          const serverLimit = err.details?.limit_bytes;
          // Only quote a number the server actually sent — the client cap
          // is not the server's.
          throw new Error(
            typeof serverLimit === "number"
              ? t("knowledge_page.upload_queue_err_too_large_server", {
                  limit: formatBytes(serverLimit),
                })
              : t("knowledge_page.upload_queue_err_too_large_server_unknown"),
          );
        }
        throw new Error(errMessage(err));
      }
    },
    [baseName, t],
  );

  const quietRefresh = useCallback(() => {
    void refresh({ quiet: true });
  }, [refresh]);

  const queue = useUploadQueue<UploadedDocument>({
    upload: uploadOne,
    onSettled: quietRefresh,
    onBatchProgress: quietRefresh,
    stallMessage: t("knowledge_page.upload_queue_err_stalled", {
      seconds: DEFAULT_UPLOAD_STALL_TIMEOUT_MS / 1000,
    }),
  });
  const { enqueue, retry } = queue;

  const handleBeforeUpload = useCallback(
    (_file: File, batch: File[]) => {
      if (batch !== lastBatchRef.current) {
        lastBatchRef.current = batch;
        if (!readonly) enqueue(batch);
      }
      // The queue owns the file; keep it out of antd's own fileList.
      return Upload.LIST_IGNORE;
    },
    [enqueue, readonly],
  );

  const handleRetry = useCallback(
    (id: string) => {
      if (!readonly) retry(id);
    },
    [readonly, retry],
  );

  // A drag over the zone switches the Dragger to directory mode so dropped
  // folders are expanded (rc-upload reads ``directory`` at drop time).
  // ``dragover`` keeps the flag set (React bails out when it is unchanged);
  // an enter/leave depth counter clears it when the drag really leaves, and
  // the window listeners catch a drop / dragend anywhere else.
  const resetDrag = useCallback(() => {
    dragDepthRef.current = 0;
    setDragging(false);
  }, []);
  const onDragEnter = useCallback(() => {
    dragDepthRef.current += 1;
    setDragging(true);
  }, []);
  const onDragOver = useCallback(() => setDragging(true), []);
  const onDragLeave = useCallback(() => {
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) setDragging(false);
  }, []);
  useEffect(() => {
    if (!dragging) return undefined;
    window.addEventListener("drop", resetDrag);
    window.addEventListener("dragend", resetDrag);
    return () => {
      window.removeEventListener("drop", resetDrag);
      window.removeEventListener("dragend", resetDrag);
    };
  }, [dragging, resetDrag]);
  const directoryMode = folderMode || dragging;

  const handleReingest = useCallback(
    async (documentId: string) => {
      try {
        await reingestDocument(baseName, documentId);
        message.success(t("knowledge_page.reingest_started"));
        await refresh();
      } catch (err) {
        const detail =
          err instanceof ApiError && err.status === 409
            ? t("knowledge_page.reingest_no_bytes")
            : errMessage(err);
        message.error(detail);
      }
    },
    [t, baseName, message, refresh],
  );

  const handleRecognize = useCallback(
    async (documentId: string) => {
      try {
        await recognizeDocument(baseName, documentId);
        message.success(t("knowledge_page.recognize_started"));
      } catch (err) {
        message.error(
          err instanceof ApiError && err.status === 409
            ? t("knowledge_page.recognize_nothing")
            : errMessage(err),
        );
      }
      await refresh();
    },
    [t, baseName, message, refresh],
  );

  const handleDiscardPending = useCallback(
    async (documentId: string) => {
      try {
        await discardPendingDocument(baseName, documentId);
        message.success(t("knowledge_page.discard_pending_done"));
      } catch (err) {
        message.error(
          err instanceof ApiError && err.status === 409
            ? t("knowledge_page.discard_pending_none")
            : errMessage(err),
        );
      }
      await refresh();
    },
    [t, baseName, message, refresh],
  );

  const handleCancel = useCallback(
    async (documentId: string) => {
      try {
        const [result] = await cancelDocuments(baseName, [documentId]);
        if (result?.outcome === "cancelled") {
          message.success(t("knowledge_page.cancel_ingest_done"));
        } else if (result?.outcome === "not_cancellable") {
          // Settled, or past the point of no return (writing its chunks).
          message.info(t("knowledge_page.cancel_ingest_not_cancellable"));
        } else {
          message.warning(t("knowledge_page.cancel_ingest_not_found"));
        }
      } catch (err) {
        message.error(errMessage(err));
      }
      await refresh();
    },
    [t, baseName, message, refresh],
  );

  const handleDelete = useCallback(
    async (documentId: string) => {
      try {
        await deleteDocument(baseName, documentId);
        await refresh();
      } catch (err) {
        message.error(errMessage(err));
      }
    },
    [baseName, message, refresh],
  );

  const columns: TableColumnsType<KnowledgeDocument> = useMemo(
    () => [
      {
        title: t("knowledge_page.col_filename"),
        dataIndex: "filename",
        key: "filename",
        ellipsis: true,
        render: (filename: string) => <Text strong>{filename}</Text>,
      },
      {
        title: t("knowledge_page.col_status"),
        dataIndex: "status",
        key: "status",
        width: 220,
        render: (status: DocumentStatus, record) => {
          const tag = <Tag color={STATUS_COLOR[status]}>{t(`knowledge_page.status_${status}`)}</Tag>;
          const reason =
            status === "failed"
              ? documentErrorText(t, i18n.language, record)
              : status === "cancelled"
                ? t("knowledge_page.status_cancelled_hint")
                : null;
          const statusTag = reason ? <Tooltip title={reason}>{tag}</Tooltip> : tag;
          const pages = (
            <>
              <PageRecognitionTags stats={record.page_stats} documentId={record.id} />
              <FigureStatsTags stats={record.figure_stats} documentId={record.id} />
            </>
          );
          if (!record.has_pending) {
            return (
              <Space size={4} wrap>
                {statusTag}
                {pages}
              </Space>
            );
          }
          // A re-upload is pending: say which version retrieval uses now.
          const serving = record.chunk_count > 0;
          return (
            <Space size={4} wrap>
              {statusTag}
              <Tooltip
                title={t(
                  status === "failed"
                    ? "knowledge_page.pending_failed_hint"
                    : status === "cancelled"
                      ? "knowledge_page.pending_cancelled_hint"
                      : "knowledge_page.pending_ingesting_hint",
                )}
              >
                <Tag
                  color={serving ? "gold" : "default"}
                  bordered={false}
                  // Focusable so keyboard users reach the hint tooltip.
                  tabIndex={0}
                  data-testid={`doc-pending-${record.id}`}
                >
                  {t(
                    serving
                      ? "knowledge_page.pending_previous_serving"
                      : "knowledge_page.pending_nothing_serving",
                  )}
                </Tag>
              </Tooltip>
              {pages}
            </Space>
          );
        },
      },
      {
        title: t("knowledge_page.col_chunks"),
        dataIndex: "chunk_count",
        key: "chunk_count",
        width: 80,
        render: (count: number) => <Text className="mono">{count}</Text>,
      },
      {
        title: t("knowledge_page.col_attempts"),
        dataIndex: "attempts",
        key: "attempts",
        width: 80,
        render: (attempts: number | undefined) => (
          <Text className="mono">{attempts ?? 0}</Text>
        ),
      },
      {
        title: t("knowledge_page.col_updated"),
        dataIndex: "updated_at",
        key: "updated_at",
        width: 170,
        render: (iso: string | null) =>
          iso ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {new Date(iso).toLocaleString()}
            </Text>
          ) : (
            <Text type="secondary">—</Text>
          ),
      },
      {
        title: "",
        key: "actions",
        width: 190,
        render: (_: unknown, record) => (
          <Space size={4}>
            <Tooltip title={t("knowledge_page.view_chunks")}>
              <Button
                size="small"
                type="text"
                icon={<Eye size={14} strokeWidth={1.5} />}
                onClick={() => setPreviewDoc(record)}
                aria-label={t("knowledge_page.view_chunks")}
                data-testid={`doc-chunks-${record.id}`}
              />
            </Tooltip>
            {isCancellable(record.status) && (
              <ReadonlyTooltip on={readonly}>
                <Popconfirm
                  title={t("knowledge_page.cancel_ingest_confirm_title", {
                    name: record.filename,
                  })}
                  description={t("knowledge_page.cancel_ingest_confirm_body")}
                  onConfirm={() => void handleCancel(record.id)}
                  okText={t("knowledge_page.cancel_ingest")}
                  disabled={readonly}
                >
                  <Tooltip title={t("knowledge_page.cancel_ingest")}>
                    <Button
                      size="small"
                      type="text"
                      disabled={readonly}
                      icon={<CircleStop size={14} strokeWidth={1.5} />}
                      aria-label={t("knowledge_page.cancel_ingest")}
                      data-testid={`doc-cancel-${record.id}`}
                    />
                  </Tooltip>
                </Popconfirm>
              </ReadonlyTooltip>
            )}
            <ReadonlyTooltip on={readonly}>
              <Tooltip title={t("knowledge_page.reingest")}>
                <Button
                  size="small"
                  type="text"
                  disabled={readonly}
                  icon={<RefreshCw size={14} strokeWidth={1.5} />}
                  onClick={() => void handleReingest(record.id)}
                  aria-label={t("knowledge_page.reingest")}
                  data-testid={`doc-reingest-${record.id}`}
                />
              </Tooltip>
            </ReadonlyTooltip>
            {(needsRecognition(record.page_stats) ||
              figuresNeedDescription(record.figure_stats)) && (
              <ReadonlyTooltip on={readonly}>
                <Tooltip title={t("knowledge_page.recognize")}>
                  <Button
                    size="small"
                    type="text"
                    disabled={readonly}
                    icon={<ScanText size={14} strokeWidth={1.5} />}
                    onClick={() => void handleRecognize(record.id)}
                    aria-label={t("knowledge_page.recognize")}
                    data-testid={`doc-recognize-${record.id}`}
                  />
                </Tooltip>
              </ReadonlyTooltip>
            )}
            {record.has_pending && (
              <ReadonlyTooltip on={readonly}>
                <Popconfirm
                  title={t("knowledge_page.discard_pending_confirm_title", {
                    name: record.filename,
                  })}
                  description={t(
                    record.chunk_count > 0
                      ? "knowledge_page.discard_pending_confirm_body"
                      : "knowledge_page.discard_pending_confirm_body_nothing_serving",
                  )}
                  onConfirm={() => void handleDiscardPending(record.id)}
                  okText={t("knowledge_page.discard_pending")}
                  disabled={readonly}
                >
                  <Tooltip title={t("knowledge_page.discard_pending")}>
                    <Button
                      size="small"
                      type="text"
                      disabled={readonly}
                      icon={<Undo2 size={14} strokeWidth={1.5} />}
                      aria-label={t("knowledge_page.discard_pending")}
                      data-testid={`doc-discard-pending-${record.id}`}
                    />
                  </Tooltip>
                </Popconfirm>
              </ReadonlyTooltip>
            )}
            <ReadonlyTooltip on={readonly}>
              <Popconfirm
                title={t("knowledge_page.delete_doc_confirm_title", { name: record.filename })}
                onConfirm={() => void handleDelete(record.id)}
                okText={t("knowledge_page.delete")}
                okButtonProps={{ danger: true }}
                disabled={readonly}
              >
                <Button
                  size="small"
                  danger
                  type="text"
                  disabled={readonly}
                  icon={<Trash2 size={13} strokeWidth={1.5} />}
                  aria-label={t("knowledge_page.delete")}
                  data-testid={`doc-delete-${record.id}`}
                />
              </Popconfirm>
            </ReadonlyTooltip>
          </Space>
        ),
      },
    ],
    [t, i18n.language, handleReingest, handleRecognize, handleDiscardPending, handleCancel, handleDelete, readonly],
  );

  const pagination = useMemo<TablePaginationConfig>(
    () => ({
      current: page,
      pageSize: DOC_TABLE_PAGE_SIZE,
      total,
      showSizeChanger: false,
      showTotal: (count: number) => t("knowledge_page.doc_table_total", { total: count }),
      onChange: setPage,
      ...DOC_TABLE_PAGINATION_TESTID,
    }),
    [page, t, total],
  );

  return (
    <div data-testid="knowledge-documents-tab">
      <ReadonlyTooltip on={readonly} block>
        <div
          style={{ marginBottom: 16 }}
          onDragEnter={onDragEnter}
          onDragOver={onDragOver}
          onDragLeave={onDragLeave}
          onDrop={resetDrag}
        >
          <Upload.Dragger
            directory={directoryMode}
            showUploadList={false}
            multiple
            beforeUpload={handleBeforeUpload}
            disabled={readonly}
            data-testid="doc-upload-dragger"
          >
            <p className="ant-upload-text">
              {t(
                folderMode
                  ? "knowledge_page.upload_dragger_hint_folder"
                  : "knowledge_page.upload_dragger_hint",
              )}
            </p>
            <p className="ant-upload-hint">{t("knowledge_page.upload_dragger_sub")}</p>
          </Upload.Dragger>
          <div
            style={{
              display: "flex",
              justifyContent: "flex-end",
              alignItems: "center",
              gap: 8,
              marginTop: 8,
            }}
          >
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t("knowledge_page.upload_folder_toggle")}
            </Text>
            <Switch
              size="small"
              checked={folderMode}
              disabled={readonly}
              onChange={setFolderMode}
              aria-label={t("knowledge_page.upload_folder_toggle")}
              data-testid="doc-upload-folder-toggle"
            />
          </div>
        </div>
      </ReadonlyTooltip>

      <UploadQueuePanel
        items={queue.items}
        summary={queue.summary}
        maxBytes={KNOWLEDGE_DOCUMENT_MAX_BYTES}
        readonly={readonly}
        onCancel={queue.cancel}
        onRetry={handleRetry}
        onCancelAll={queue.cancelAll}
        onClearDone={queue.clearDone}
      />

      <Table<KnowledgeDocument>
        size="small"
        columns={columns}
        dataSource={documents}
        rowKey="id"
        loading={loading}
        pagination={pagination}
        locale={{ emptyText: <Empty description={t("knowledge_page.documents_empty")} /> }}
        data-testid="doc-table"
      />

      <SegmentPreviewDrawer
        baseName={baseName}
        readScope={readScope}
        document={previewDoc}
        onClose={() => setPreviewDoc(null)}
      />
    </div>
  );
}
