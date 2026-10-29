/**
 * Upload queue panel — B-175 L3-T3 (batch / folder import).
 *
 * Presentational: renders the ``useUploadQueue`` items. Overall progress,
 * one row per real upload (relative path, size, status, progress, cancel /
 * retry) in a scroll box, and a summary line for the files skipped up front
 * (unsupported type / over the size cap / name repeated in the batch) with
 * an expandable list of those rows. A done row whose bytes matched the
 * ready document already there says so instead of a full progress bar.
 * Rows are memoised so a progress tick on one upload does not re-render the
 * other few hundred.
 */
import { memo, useMemo, useState, type ReactNode } from "react";
import { Button, Progress, Space, Tag, Typography } from "antd";
import { useTranslation } from "react-i18next";

import type { UploadedDocument } from "../../api/knowledge";
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

/** Row list height before it scrolls (≈ 10 rows). */
const ROW_LIST_MAX_HEIGHT = 320;

interface UploadQueuePanelProps {
  items: UploadQueueItem<UploadedDocument>[];
  summary: UploadQueueSummary;
  /** Client-side cap, for the "over {{limit}}" copy. */
  maxBytes: number;
  /** Read-only page state: retry / cancel-all are disabled. */
  readonly: boolean;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onCancelAll: () => void;
  onClearDone: () => void;
  /** B-222 PR-2 — under the header: the batch's estimates and actions. */
  batchBar?: ReactNode;
  /** B-222 PR-2 — under a row: its document on the server (status, cost). */
  rowExtra?: (item: UploadQueueItem<UploadedDocument>) => ReactNode;
}

interface UploadQueueRowProps {
  item: UploadQueueItem<UploadedDocument>;
  limitLabel: string;
  readonly: boolean;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
}

function skipReasonCopy(
  item: UploadQueueItem<UploadedDocument>,
  limitLabel: string,
  t: ReturnType<typeof useTranslation>["t"],
): string {
  switch (item.skipReason) {
    case "too_large":
      return t("knowledge_page.upload_queue_reason_too_large", { limit: limitLabel });
    case "duplicate_name":
      return t("knowledge_page.upload_queue_reason_duplicate");
    default:
      return t("knowledge_page.upload_queue_reason_unsupported");
  }
}

const UploadQueueRow = memo(function UploadQueueRow({
  item,
  limitLabel,
  readonly,
  onCancel,
  onRetry,
}: UploadQueueRowProps) {
  const { t } = useTranslation();
  return (
    <div
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
            {skipReasonCopy(item, limitLabel, t)}
          </Text>
        ) : item.status === "done" && item.result?.unchanged ? (
          <Text
            type="secondary"
            style={{ fontSize: 12 }}
            data-testid={`doc-upload-unchanged-${item.id}`}
          >
            {t("knowledge_page.upload_queue_unchanged")}
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
            aria-label={t("knowledge_page.upload_queue_progress_label", {
              name: item.relativePath,
            })}
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
          <Button
            size="small"
            type="link"
            onClick={() => onCancel(item.id)}
            aria-label={t("knowledge_page.upload_queue_cancel_file", { name: item.relativePath })}
          >
            {t("knowledge_page.upload_queue_cancel")}
          </Button>
        ) : null}
        {item.status === "failed" ? (
          <Button
            size="small"
            type="link"
            disabled={readonly}
            onClick={() => onRetry(item.id)}
            aria-label={t("knowledge_page.upload_queue_retry_file", { name: item.relativePath })}
          >
            {t("knowledge_page.upload_queue_retry")}
          </Button>
        ) : null}
      </Space>
    </div>
  );
});

export function UploadQueuePanel({
  items,
  summary,
  maxBytes,
  readonly,
  onCancel,
  onRetry,
  onCancelAll,
  onClearDone,
  batchBar,
  rowExtra,
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
  const unsupportedCount = skipped.filter((i) => i.skipReason === "unsupported").length;
  const tooLargeCount = skipped.filter((i) => i.skipReason === "too_large").length;
  const duplicateCount = skipped.filter((i) => i.skipReason === "duplicate_name").length;
  const skippedParts = [
    unsupportedCount > 0
      ? t("knowledge_page.upload_queue_skipped_unsupported", { count: unsupportedCount })
      : null,
    tooLargeCount > 0
      ? t("knowledge_page.upload_queue_skipped_too_large", {
          n: tooLargeCount,
          limit: limitLabel,
        })
      : null,
    duplicateCount > 0
      ? t("knowledge_page.upload_queue_skipped_duplicate", { count: duplicateCount })
      : null,
  ].filter((part): part is string => part !== null);

  const renderRow = (item: UploadQueueItem<UploadedDocument>) => (
    <div key={item.id}>
      <UploadQueueRow
        item={item}
        limitLabel={limitLabel}
        readonly={readonly}
        onCancel={onCancel}
        onRetry={onRetry}
      />
      {rowExtra ? rowExtra(item) : null}
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
      <div
        data-testid="doc-upload-queue-header"
        style={{ display: "flex", alignItems: "center", gap: 12 }}
      >
        <Text strong>{t("knowledge_page.upload_queue_title")}</Text>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t("knowledge_page.upload_queue_progress", {
            done: summary.done,
            total: uploads.length,
          })}
          {summary.failed > 0
            ? ` · ${t("knowledge_page.upload_queue_status_failed")} ${summary.failed}`
            : ""}
          {summary.cancelled > 0
            ? ` · ${t("knowledge_page.upload_queue_cancelled")} ${summary.cancelled}`
            : ""}
        </Text>
        <div style={{ flex: 1, minWidth: 120 }}>
          <Progress
            percent={overallPercent}
            size="small"
            aria-label={t("knowledge_page.upload_queue_overall_progress_label")}
            status={active ? "active" : summary.failed > 0 ? "exception" : undefined}
          />
        </div>
        <Space size={4}>
          <Button size="small" disabled={!active || readonly} onClick={onCancelAll}>
            {t("knowledge_page.upload_queue_cancel_all")}
          </Button>
          <Button
            size="small"
            disabled={summary.done + summary.skipped + summary.cancelled === 0}
            onClick={onClearDone}
          >
            {t("knowledge_page.upload_queue_clear_done")}
          </Button>
        </Space>
      </div>
      {batchBar}

      {uploads.length > 0 ? (
        <div style={{ marginTop: 8, maxHeight: ROW_LIST_MAX_HEIGHT, overflowY: "auto" }}>
          {uploads.map(renderRow)}
        </div>
      ) : null}

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
          {showSkipped ? (
            <div style={{ maxHeight: ROW_LIST_MAX_HEIGHT, overflowY: "auto" }}>
              {skipped.map(renderRow)}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
