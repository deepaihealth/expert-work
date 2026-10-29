/**
 * B-222 PR-2 — the upload queue's estimate parts: the batch line 「本批 20
 * 份：待确认 17 份，预估合计 ¥1.23；副本 2 份（不入库，不计费）；估算中 1 份」
 * with 「确认 17 份 · 跳过 2 份副本」 and 「全部取消」, and under each row its
 * document's server status, cost and actions (confirm, withdraw, ingest a
 * duplicate anyway). Presentational: the documents and the handlers come from
 * the documents tab.
 */
import { Button, Popconfirm, Space, Tag, Typography } from "antd";
import { useTranslation } from "react-i18next";

import type { KnowledgeDocument } from "../../api/knowledge";
import { ReadonlyTooltip } from "../../components/ReadonlyTooltip";
import { IngestCostTag } from "./IngestCostTag";
import { canConfirm, formatYuan, isDuplicate, isWaiting } from "./ingestCost";

const { Text } = Typography;

export interface BatchCounts {
  confirmable: KnowledgeDocument[];
  duplicates: KnowledgeDocument[];
  estimating: number;
  waiting: KnowledgeDocument[];
  totalMicros: number;
}

export function batchCounts(documents: KnowledgeDocument[]): BatchCounts {
  const confirmable = documents.filter(canConfirm);
  const duplicates = documents.filter(
    (d) => d.status === "awaiting_confirmation" && isDuplicate(d.ingest_estimate),
  );
  return {
    confirmable,
    duplicates,
    estimating: documents.filter((d) => d.status === "estimating").length,
    waiting: documents.filter((d) => isWaiting(d.status)),
    totalMicros: confirmable.reduce((sum, d) => sum + (d.ingest_estimate?.total_micros ?? 0), 0),
  };
}

interface UploadBatchBarProps {
  documents: KnowledgeDocument[];
  readonly: boolean;
  busy: boolean;
  onConfirm: (confirm: KnowledgeDocument[], skip: KnowledgeDocument[]) => void;
  onWithdraw: (documents: KnowledgeDocument[]) => void;
}

export function UploadBatchBar({
  documents,
  readonly,
  busy,
  onConfirm,
  onWithdraw,
}: UploadBatchBarProps) {
  const { t } = useTranslation();
  if (documents.length === 0) return null;
  const counts = batchCounts(documents);
  const parts = [
    t("knowledge_page.estimate_batch_awaiting", {
      count: counts.confirmable.length,
      amount: formatYuan(counts.totalMicros),
    }),
    counts.duplicates.length > 0
      ? t("knowledge_page.estimate_batch_duplicates", { count: counts.duplicates.length })
      : null,
    counts.estimating > 0
      ? t("knowledge_page.estimate_banner_estimating", { count: counts.estimating })
      : null,
  ].filter((part): part is string => part !== null);
  return (
    <div
      data-testid="doc-upload-batch"
      style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", marginTop: 6 }}
    >
      <Text style={{ fontSize: 12 }} data-testid="doc-upload-batch-text">
        {t("knowledge_page.estimate_batch_title", { count: documents.length })}
        {parts.join(t("knowledge_page.estimate_sep"))}
      </Text>
      <ReadonlyTooltip on={readonly}>
        <Button
          size="small"
          type="primary"
          disabled={readonly || busy || counts.confirmable.length === 0}
          onClick={() => onConfirm(counts.confirmable, counts.duplicates)}
          data-testid="doc-upload-batch-confirm"
        >
          {counts.duplicates.length > 0
            ? t("knowledge_page.estimate_batch_confirm_skip", {
                count: counts.confirmable.length,
                skip: counts.duplicates.length,
              })
            : t("knowledge_page.estimate_confirm_all", { count: counts.confirmable.length })}
        </Button>
      </ReadonlyTooltip>
      <ReadonlyTooltip on={readonly}>
        <Popconfirm
          title={t("knowledge_page.estimate_withdraw_batch_confirm_title")}
          description={t("knowledge_page.estimate_withdraw_confirm_body")}
          onConfirm={() => onWithdraw(counts.waiting)}
          disabled={readonly || busy || counts.waiting.length === 0}
        >
          <Button
            size="small"
            disabled={readonly || busy || counts.waiting.length === 0}
            data-testid="doc-upload-batch-withdraw"
          >
            {t("knowledge_page.estimate_withdraw_all")}
          </Button>
        </Popconfirm>
      </ReadonlyTooltip>
    </div>
  );
}

const STATUS_COLOR: Record<string, string> = {
  estimating: "processing",
  awaiting_confirmation: "blue",
  pending: "default",
  processing: "processing",
  ready: "success",
  failed: "error",
  cancelled: "warning",
};

interface EstimateActionsProps {
  document: KnowledgeDocument;
  readonly: boolean;
  busy: boolean;
  onConfirm: (document: KnowledgeDocument) => void;
  onWithdraw: (document: KnowledgeDocument) => void;
  onIngestAnyway: (document: KnowledgeDocument) => void;
}

/** The confirm / 仍入库 / withdraw buttons of one waiting document. */
export function EstimateActions({
  document,
  readonly,
  busy,
  onConfirm,
  onWithdraw,
  onIngestAnyway,
}: EstimateActionsProps) {
  const { t } = useTranslation();
  const duplicate = isDuplicate(document.ingest_estimate);
  return (
    <Space size={4}>
      {canConfirm(document) && (
        <ReadonlyTooltip on={readonly}>
          <Button
            size="small"
            type="link"
            disabled={readonly || busy}
            onClick={() => onConfirm(document)}
            data-testid={`doc-confirm-${document.id}`}
          >
            {t("knowledge_page.estimate_confirm")}
          </Button>
        </ReadonlyTooltip>
      )}
      {document.status === "awaiting_confirmation" && duplicate && (
        <ReadonlyTooltip on={readonly}>
          <Button
            size="small"
            type="link"
            disabled={readonly || busy}
            onClick={() => onIngestAnyway(document)}
            data-testid={`doc-ingest-anyway-${document.id}`}
          >
            {t("knowledge_page.estimate_ingest_anyway")}
          </Button>
        </ReadonlyTooltip>
      )}
      {isWaiting(document.status) && (
        <ReadonlyTooltip on={readonly}>
          <Popconfirm
            title={t("knowledge_page.estimate_withdraw_confirm_title", { name: document.filename })}
            description={t("knowledge_page.estimate_withdraw_confirm_body")}
            onConfirm={() => onWithdraw(document)}
            disabled={readonly || busy}
          >
            <Button
              size="small"
              type="link"
              disabled={readonly || busy}
              data-testid={`doc-withdraw-${document.id}`}
            >
              {t("knowledge_page.estimate_withdraw")}
            </Button>
          </Popconfirm>
        </ReadonlyTooltip>
      )}
    </Space>
  );
}

interface UploadRowEstimateProps extends Omit<EstimateActionsProps, "document"> {
  document: KnowledgeDocument | undefined;
}

/** Under an upload queue row: what became of the document on the server. */
export function UploadRowEstimate({ document, ...actions }: UploadRowEstimateProps) {
  const { t } = useTranslation();
  if (!document) return null;
  return (
    <div
      data-testid={`doc-upload-server-${document.id}`}
      data-server-status={document.status}
      style={{ display: "flex", alignItems: "center", gap: 8, paddingBottom: 4, flexWrap: "wrap" }}
    >
      <Tag color={STATUS_COLOR[document.status] ?? "default"} bordered={false}>
        {t(`knowledge_page.status_${document.status}`)}
      </Tag>
      <IngestCostTag document={document} />
      <EstimateActions document={document} {...actions} />
    </div>
  );
}
