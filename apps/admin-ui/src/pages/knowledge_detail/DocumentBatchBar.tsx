/**
 * Batch actions on the selected documents — B-175 P3-PR3.
 *
 * Switch on / off (recall leaves a switched-off document out; its chunks
 * stay), re-ingest (after a confirm: documents cut by other chunking
 * parameters are embedded again) and delete (after a confirm; tenant admins
 * only — ``manifest:delete``). Each result says what was done and what was
 * skipped and why, one i18n key per message. Always shown so a read-only
 * viewer sees the actions with the read-only hint.
 */
import { useState, type ReactElement } from "react";
import { App, Button, Popconfirm, Space, Tooltip, Typography } from "antd";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";

import { ApiError, errMessage } from "../../api/client";
import {
  batchDeleteDocuments,
  batchDocuments,
  type BatchSkip,
  type DocumentBatchAction,
  type DocumentBatchResult,
} from "../../api/knowledge_maintenance";
import { ReadonlyTooltip } from "../../components/ReadonlyTooltip";

const { Text } = Typography;

type Action = DocumentBatchAction | "delete";

const DONE_KEY: Record<Action, string> = {
  enable: "kb_maintenance.batch_done_enable",
  disable: "kb_maintenance.batch_done_disable",
  reingest: "kb_maintenance.batch_done_reingest",
  delete: "kb_maintenance.batch_done_delete",
};

/** 「跳过 N 份：M 份…；K 份…」 — ``null`` when nothing was skipped. */
export function skippedSummary(t: TFunction, skipped: readonly BatchSkip[]): string | null {
  if (skipped.length === 0) return null;
  const notFound = skipped.filter((s) => s.reason === "not_found").length;
  const missing = skipped.filter((s) => s.reason === "content_missing").length;
  const busy = skipped.filter((s) => s.reason === "busy").length;
  // A reason this version does not know still counts, so the parts add up.
  const other = skipped.length - notFound - missing - busy;
  const reasons = [
    notFound > 0 ? t("kb_maintenance.skip_not_found", { n: notFound }) : null,
    missing > 0 ? t("kb_maintenance.skip_content_missing", { n: missing }) : null,
    busy > 0 ? t("kb_maintenance.skip_busy", { n: busy }) : null,
    other > 0 ? t("kb_maintenance.skip_other", { n: other }) : null,
  ].filter((part): part is string => part !== null);
  return t("kb_maintenance.batch_skipped", {
    n: skipped.length,
    reasons: reasons.join(t("kb_maintenance.skip_sep")),
  });
}

export interface DocumentBatchBarProps {
  baseName: string;
  selectedIds: string[];
  readonly: boolean;
  /** ``manifest:delete`` (tenant admin / system admin). */
  canDelete: boolean;
  /** A batch ran: refresh the table and clear the selection. */
  onDone: () => void;
  onClear: () => void;
}

export function DocumentBatchBar({
  baseName,
  selectedIds,
  readonly,
  canDelete,
  onDone,
  onClear,
}: DocumentBatchBarProps): ReactElement {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [running, setRunning] = useState<Action | null>(null);
  const count = selectedIds.length;
  const idle = count === 0 || running !== null;

  const run = async (action: Action) => {
    setRunning(action);
    try {
      const result: DocumentBatchResult =
        action === "delete"
          ? await batchDeleteDocuments(baseName, selectedIds)
          : await batchDocuments(baseName, action, selectedIds);
      message.success(t(DONE_KEY[action], { n: result.done.length }));
      const skipped = skippedSummary(t, result.skipped);
      if (skipped !== null) message.warning(skipped);
    } catch (err) {
      message.error(
        err instanceof ApiError && err.status === 503
          ? t("knowledge_page.embedder_missing")
          : errMessage(err),
      );
    } finally {
      setRunning(null);
      onDone();
    }
  };

  const gate = (button: ReactElement) =>
    readonly ? <ReadonlyTooltip on>{button}</ReadonlyTooltip> : button;

  const deleteButton = (
    <Popconfirm
      title={t("kb_maintenance.batch_delete_confirm_title", { n: count })}
      description={t("kb_maintenance.batch_delete_confirm_body")}
      okText={t("kb_maintenance.batch_delete")}
      okButtonProps={{ danger: true }}
      onConfirm={() => void run("delete")}
      disabled={readonly || !canDelete || idle}
    >
      <Button
        size="small"
        danger
        loading={running === "delete"}
        disabled={readonly || !canDelete || idle}
        data-testid="doc-batch-delete"
      >
        {t("kb_maintenance.batch_delete")}
      </Button>
    </Popconfirm>
  );

  return (
    <div
      role="toolbar"
      aria-label={t("kb_maintenance.batch_toolbar_aria")}
      data-testid="doc-batch-bar"
      style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8, flexWrap: "wrap" }}
    >
      {count > 0 && (
        <Text type="secondary" style={{ fontSize: 12 }} data-testid="doc-batch-count">
          {t("kb_maintenance.batch_selected", { n: count })}
        </Text>
      )}
      <Space size={4} wrap>
        {gate(
          <Button
            size="small"
            loading={running === "enable"}
            disabled={readonly || idle}
            onClick={() => void run("enable")}
            data-testid="doc-batch-enable"
          >
            {t("kb_maintenance.batch_enable")}
          </Button>,
        )}
        {gate(
          <Button
            size="small"
            loading={running === "disable"}
            disabled={readonly || idle}
            onClick={() => void run("disable")}
            data-testid="doc-batch-disable"
          >
            {t("kb_maintenance.batch_disable")}
          </Button>,
        )}
        {gate(
          <Popconfirm
            title={t("kb_maintenance.batch_reingest_confirm_title", { n: count })}
            description={t("kb_maintenance.batch_reingest_confirm_body")}
            okText={t("kb_maintenance.batch_reingest")}
            onConfirm={() => void run("reingest")}
            disabled={readonly || idle}
          >
            <Button
              size="small"
              loading={running === "reingest"}
              disabled={readonly || idle}
              data-testid="doc-batch-reingest"
            >
              {t("kb_maintenance.batch_reingest")}
            </Button>
          </Popconfirm>,
        )}
        {readonly ? (
          <ReadonlyTooltip on>{deleteButton}</ReadonlyTooltip>
        ) : !canDelete ? (
          <Tooltip title={t("kb_maintenance.needs_admin")}>
            <span
              data-testid="doc-batch-delete-needs-admin"
              style={{ display: "inline-flex", cursor: "not-allowed" }}
            >
              <span style={{ display: "inline-flex", pointerEvents: "none" }}>{deleteButton}</span>
            </span>
          </Tooltip>
        ) : (
          deleteButton
        )}
        {count > 0 && (
          <Button size="small" type="link" onClick={onClear} data-testid="doc-batch-clear">
            {t("kb_maintenance.batch_clear")}
          </Button>
        )}
      </Space>
    </div>
  );
}
