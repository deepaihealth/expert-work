/**
 * Documents tab — KB commercial uplift.
 *
 * Rich document table (status with localized labels, chunk count, attempts,
 * updated) + drag-drop multi-file upload + per-document actions (view chunks /
 * re-ingest / delete). Ingest progress is a conditional 5s poll (Mini-ADR
 * H-18): poll while any document is non-terminal, stop once all settle.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { App, Button, Empty, Popconfirm, Space, Table, Tag, Tooltip, Typography, Upload } from "antd";
import type { TableColumnsType } from "antd";
import { Eye, RefreshCw, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  deleteDocument,
  isSupportedDocument,
  listDocuments,
  reingestDocument,
  SUPPORTED_DOCUMENT_EXTENSIONS,
  uploadDocument,
  type DocumentStatus,
  type KnowledgeDocument,
} from "../../api/knowledge";
import { ApiError } from "../../api/client";
import { SegmentPreviewDrawer } from "./SegmentPreviewDrawer";
import { ReadonlyTooltip } from "../../components/ReadonlyTooltip";

const { Text } = Typography;

const STATUS_COLOR: Record<DocumentStatus, string> = {
  pending: "default",
  processing: "processing",
  ready: "success",
  failed: "error",
};

const POLL_INTERVAL_MS = 5_000;

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
  const { t } = useTranslation();
  const { message } = App.useApp();

  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [previewDoc, setPreviewDoc] = useState<KnowledgeDocument | null>(null);
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  const refresh = useCallback(
    async ({ quiet = false }: { quiet?: boolean } = {}) => {
      if (!quiet) setLoading(true);
      try {
        setDocuments(await listDocuments(baseName, readScope));
      } catch (err) {
        message.error(errMessage(err));
      } finally {
        if (!quiet) setLoading(false);
      }
    },
    [baseName, message, readScope],
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
        void refresh({ quiet: true });
      }, POLL_INTERVAL_MS);
      return () => {
        if (pollTimer.current !== null) clearInterval(pollTimer.current);
        pollTimer.current = null;
      };
    }
    return undefined;
  }, [hasActiveIngest, refresh]);

  const handleUpload = useCallback(
    async (file: File) => {
      if (!isSupportedDocument(file.name)) {
        message.error(t("knowledge_page.unsupported_type", { name: file.name }));
        return false;
      }
      setUploading(true);
      try {
        await uploadDocument(baseName, file);
        await refresh();
      } catch (err) {
        const detail =
          err instanceof ApiError && err.status === 503
            ? t("knowledge_page.embedder_missing")
            : errMessage(err);
        message.error(detail);
      } finally {
        setUploading(false);
      }
      return false; // handled manually
    },
    [t, baseName, message, refresh],
  );

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
        width: 120,
        render: (status: DocumentStatus, record) => {
          const tag = <Tag color={STATUS_COLOR[status]}>{t(`knowledge_page.status_${status}`)}</Tag>;
          return status === "failed" && record.error ? (
            <Tooltip title={record.error}>{tag}</Tooltip>
          ) : (
            tag
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
        width: 130,
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
    [t, handleReingest, handleDelete, readonly],
  );

  return (
    <div data-testid="knowledge-documents-tab">
      <ReadonlyTooltip on={readonly} block>
        <div style={{ marginBottom: 16 }}>
          <Upload.Dragger
            accept={SUPPORTED_DOCUMENT_EXTENSIONS.join(",")}
            showUploadList={false}
            multiple
            beforeUpload={(file) => handleUpload(file)}
            disabled={uploading || readonly}
            data-testid="doc-upload-dragger"
          >
            <p className="ant-upload-text">{t("knowledge_page.upload_dragger_hint")}</p>
            <p className="ant-upload-hint">{t("knowledge_page.upload_dragger_sub")}</p>
          </Upload.Dragger>
        </div>
      </ReadonlyTooltip>

      <Table<KnowledgeDocument>
        size="small"
        columns={columns}
        dataSource={documents}
        rowKey="id"
        loading={loading}
        pagination={false}
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
