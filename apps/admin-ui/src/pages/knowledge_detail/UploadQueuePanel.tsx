/**
 * Upload queue panel — B-175 L3-T3 (batch / folder import).
 *
 * Presentational: renders the ``useUploadQueue`` items. Overall progress,
 * one row per real upload (relative path, size, status, progress, cancel /
 * retry), and a summary line for the files skipped up front (unsupported
 * type / over the size cap) with an expandable list of those rows.
 */
import { useMemo, useState } from "react";
import { Button, Progress, Space, Tag, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { formatBytes } from "../../components/console/workspace_format";
import type {
  UploadQueueItem,
  UploadQueueStatus,
  UploadQueueSummary,
} from "../../hooks/useUploadQueue";

const { Text } = Typography;

const STATUS_COLOR: Record<UploadQueueStatus, string> = {
  queued: "default",
  uploading: "processing",
  done: "success",
  failed: "error",
  skipped: "warning",
};

interface UploadQueuePanelProps {
  items: UploadQueueItem<unknown>[];
  summary: UploadQueueSummary;
  /** Client-side cap, for the "over {{limit}}" copy. */
  maxBytes: number;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onCancelAll: () => void;
  onClearDone: () => void;
}

export function UploadQueuePanel({
  items,
  summary,
  maxBytes,
  onCancel,
  onRetry,
  onCancelAll,
  onClearDone,
}: UploadQueuePanelProps) {
  const { t } = useTranslation();
  const [showSkipped, setShowSkipped] = useState(false);

  const uploads = useMemo(() => items.filter((i) => i.status !== "skipped"), [items]);
  const skipped = useMemo(() => items.filter((i) => i.status === "skipped"), [items]);
  const overallPercent = useMemo(() => {
    if (uploads.length === 0) return 0;
    const sum = uploads.reduce((acc, i) => acc + (i.status === "done" ? 100 : i.progress), 0);
    return Math.round(sum / uploads.length);
  }, [uploads]);

  if (items.length === 0) return null;

  const active = summary.queued + summary.uploading > 0;
  const limitLabel = formatBytes(maxBytes);
  const skippedParts = [
    skipped.some((i) => i.skipReason === "unsupported")
      ? t("knowledge_page.upload_queue_skipped_unsupported", {
          n: skipped.filter((i) => i.skipReason === "unsupported").length,
        })
      : null,
    skipped.some((i) => i.skipReason === "too_large")
      ? t("knowledge_page.upload_queue_skipped_too_large", {
          n: skipped.filter((i) => i.skipReason === "too_large").length,
          limit: limitLabel,
        })
      : null,
  ].filter((part): part is string => part !== null);

  const renderRow = (item: UploadQueueItem<unknown>) => (
    <div
      key={item.id}
      data-testid={`doc-upload-queue-row-${item.id}`}
      data-status={item.status}
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(0, 1fr) 80px 90px 160px 90px",
        alignItems: "center",
        gap: 8,
        padding: "4px 0",
        opacity: item.status === "skipped" ? 0.7 : 1,
      }}
    >
      <Text ellipsis={{ tooltip: item.relativePath }} style={{ minWidth: 0 }}>
        {item.relativePath}
      </Text>
      <Text type="secondary" className="mono" style={{ fontSize: 12 }}>
        {formatBytes(item.size)}
      </Text>
      <Tag color={STATUS_COLOR[item.status]} style={{ marginInlineEnd: 0 }}>
        {item.cancelled
          ? t("knowledge_page.upload_queue_cancelled")
          : t(`knowledge_page.upload_queue_status_${item.status}`)}
      </Tag>
      <div style={{ minWidth: 0 }}>
        {item.status === "skipped" ? (
          <Text type="secondary" style={{ fontSize: 12 }}>
            {item.skipReason === "too_large"
              ? t("knowledge_page.upload_queue_reason_too_large", { limit: limitLabel })
              : t("knowledge_page.upload_queue_reason_unsupported")}
          </Text>
        ) : item.status === "failed" && !item.cancelled && item.error ? (
          <Text type="danger" ellipsis={{ tooltip: item.error }} style={{ fontSize: 12 }}>
            {item.error}
          </Text>
        ) : (
          <Progress
            percent={item.status === "done" ? 100 : item.progress}
            size="small"
            showInfo={item.status === "uploading"}
            status={
              item.status === "failed"
                ? "exception"
                : item.status === "uploading"
                  ? "active"
                  : undefined
            }
          />
        )}
      </div>
      <Space size={4} style={{ justifyContent: "flex-end" }}>
        {item.status === "queued" || item.status === "uploading" ? (
          <Button size="small" type="link" onClick={() => onCancel(item.id)}>
            {t("knowledge_page.upload_queue_cancel")}
          </Button>
        ) : null}
        {item.status === "failed" ? (
          <Button size="small" type="link" onClick={() => onRetry(item.id)}>
            {t("knowledge_page.upload_queue_retry")}
          </Button>
        ) : null}
      </Space>
    </div>
  );

  return (
    <div
      data-testid="doc-upload-queue"
      style={{
        border: "1px solid var(--ant-color-border, #d9d9d9)",
        borderRadius: 8,
        padding: "8px 12px",
        marginBottom: 16,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <Text strong>{t("knowledge_page.upload_queue_title")}</Text>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t("knowledge_page.upload_queue_progress", {
            done: summary.done,
            total: uploads.length,
          })}
          {summary.failed > 0
            ? ` · ${t("knowledge_page.upload_queue_status_failed")} ${summary.failed}`
            : ""}
        </Text>
        <div style={{ flex: 1, minWidth: 120 }}>
          <Progress
            percent={overallPercent}
            size="small"
            status={active ? "active" : summary.failed > 0 ? "exception" : undefined}
          />
        </div>
        <Space size={4}>
          <Button size="small" disabled={!active} onClick={onCancelAll}>
            {t("knowledge_page.upload_queue_cancel_all")}
          </Button>
          <Button
            size="small"
            disabled={summary.done + summary.skipped === 0}
            onClick={onClearDone}
          >
            {t("knowledge_page.upload_queue_clear_done")}
          </Button>
        </Space>
      </div>

      {uploads.length > 0 ? <div style={{ marginTop: 8 }}>{uploads.map(renderRow)}</div> : null}

      {skipped.length > 0 ? (
        <div style={{ marginTop: 8, borderTop: "1px dashed var(--ant-color-border, #d9d9d9)" }}>
          <div
            data-testid="doc-upload-summary"
            style={{ display: "flex", alignItems: "center", gap: 8, paddingTop: 6 }}
          >
            <Text type="warning" style={{ fontSize: 12 }}>
              {t("knowledge_page.upload_queue_skipped_summary", {
                parts: skippedParts.join(t("knowledge_page.upload_queue_skipped_sep")),
              })}
            </Text>
            <Button size="small" type="link" onClick={() => setShowSkipped((v) => !v)}>
              {showSkipped
                ? t("knowledge_page.upload_queue_hide_skipped")
                : t("knowledge_page.upload_queue_show_skipped")}
            </Button>
          </div>
          {showSkipped ? <div>{skipped.map(renderRow)}</div> : null}
        </div>
      ) : null}
    </div>
  );
}
