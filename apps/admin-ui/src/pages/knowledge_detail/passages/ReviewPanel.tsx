/**
 * The 「待复核」 view of a document — B-175 P3-PR3.
 *
 * What a re-ingest left for a person (``GET …/unapplied-edits``):
 *
 * * passages it switched off by inheritance — they hold text someone had
 *   switched off on a passage the new file no longer has. Switching one on
 *   (it may show) or keeping it off (now the person's own switch-off)
 *   settles it: no later re-ingest switches it off for that reason again;
 * * edits and switch-offs it could not put back — their passage is gone.
 *   Their text can be copied into today's passages; 「全部丢弃」 deletes their
 *   history (``manifest:delete``: tenant admins), else the retention job does
 *   after its period.
 *
 * Read-only viewers see everything; the writes are disabled with the hint.
 */
import { useState, type ReactElement } from "react";
import { App, Button, Card, Empty, Popconfirm, Space, Tag, Tooltip, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { errMessage } from "../../../api/client";
import {
  discardUnappliedEdits,
  type Passage,
  type PassageRevision,
  type UnappliedEdits,
} from "../../../api/knowledge_maintenance";
import { ReadonlyTooltip } from "../../../components/ReadonlyTooltip";
import { HeadingPath } from "../HeadingPath";
import { PassageCard } from "./PassageCard";
import { usePassageSwitch } from "./usePassageSwitch";

const { Paragraph, Text } = Typography;

export interface ReviewPanelProps {
  baseName: string;
  documentId: string;
  readScope: string | undefined;
  review: UnappliedEdits;
  figureIds: ReadonlySet<string>;
  readonly: boolean;
  /** The document is being ingested: no writes. */
  busy: boolean;
  /** ``manifest:delete`` (tenant admin / system admin): may discard. */
  canDelete: boolean;
  /** Something was settled or discarded: read the review (and the list) again. */
  onChanged: () => void;
  onEdit: (passage: Passage) => void;
  onHistory: (passage: Passage) => void;
}

function UnappliedRow({ record }: { record: PassageRevision }): ReactElement {
  const { t } = useTranslation();
  const lost = record.switch_off_lost;
  return (
    <Card
      size="small"
      style={{ marginBottom: 12 }}
      data-testid={`kb-review-unapplied-${record.passage_id}-${record.revision}`}
      title={
        <Space size={6} wrap>
          <Tag color={lost ? "default" : "blue"} bordered={false}>
            {lost
              ? t("kb_maintenance.review_kind_switch_off")
              : t("kb_maintenance.review_kind_edit", { revision: record.revision })}
          </Tag>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {record.orphaned_at
              ? t("kb_maintenance.review_orphaned_since", {
                  at: new Date(record.orphaned_at).toLocaleString(),
                })
              : t("kb_maintenance.review_still_there")}
            {record.edited_by ? ` · ${t("kb_maintenance.history_by", { who: record.edited_by })}` : ""}
          </Text>
        </Space>
      }
    >
      {lost && (
        <Text type="secondary" style={{ display: "block", fontSize: 12, marginBottom: 4 }}>
          {t("kb_maintenance.review_switch_off_hint")}
        </Text>
      )}
      <HeadingPath path={record.heading_path} />
      <Paragraph
        copyable={{ text: record.content }}
        style={{ whiteSpace: "pre-wrap", marginBottom: 0, fontSize: 13 }}
        ellipsis={{ rows: 6, expandable: true }}
      >
        {record.content}
      </Paragraph>
    </Card>
  );
}

function DiscardButton({
  count,
  readonly,
  canDelete,
  busy,
  onConfirm,
}: {
  count: number;
  readonly: boolean;
  canDelete: boolean;
  busy: boolean;
  onConfirm: () => void;
}): ReactElement {
  const { t } = useTranslation();
  const button = (
    <Popconfirm
      title={t("kb_maintenance.review_discard_confirm_title", { n: count })}
      description={t("kb_maintenance.review_discard_confirm_body")}
      okText={t("kb_maintenance.review_discard")}
      okButtonProps={{ danger: true }}
      onConfirm={onConfirm}
      disabled={readonly || !canDelete}
    >
      <Button
        size="small"
        danger
        loading={busy}
        disabled={readonly || !canDelete}
        data-testid="kb-review-discard"
      >
        {t("kb_maintenance.review_discard")}
      </Button>
    </Popconfirm>
  );
  if (readonly) return <ReadonlyTooltip on>{button}</ReadonlyTooltip>;
  if (!canDelete) {
    return (
      <Tooltip title={t("kb_maintenance.needs_admin")}>
        <span
          data-testid="kb-review-discard-needs-admin"
          style={{ display: "inline-flex", cursor: "not-allowed" }}
        >
          <span style={{ display: "inline-flex", pointerEvents: "none" }}>{button}</span>
        </span>
      </Tooltip>
    );
  }
  return button;
}

export function ReviewPanel({
  baseName,
  documentId,
  readScope,
  review,
  figureIds,
  readonly,
  busy,
  canDelete,
  onChanged,
  onEdit,
  onHistory,
}: ReviewPanelProps): ReactElement {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const { switching, switchPassage } = usePassageSwitch(baseName, documentId, onChanged);
  const [discarding, setDiscarding] = useState(false);

  const settle = async (passage: Passage, enabled: boolean) => {
    if ((await switchPassage(passage, enabled)) !== null) onChanged();
  };

  const discard = async () => {
    setDiscarding(true);
    try {
      await discardUnappliedEdits(baseName, documentId);
      message.success(t("kb_maintenance.review_discarded"));
      onChanged();
    } catch (err) {
      message.error(errMessage(err));
    } finally {
      setDiscarding(false);
    }
  };

  if (review.inherited.length === 0 && review.unapplied.length === 0) {
    return (
      <div data-testid="kb-review-panel">
        <Empty description={t("kb_maintenance.review_empty")} />
      </div>
    );
  }

  return (
    <div data-testid="kb-review-panel">
      {review.inherited.length > 0 && (
        <section data-testid="kb-review-inherited" style={{ marginBottom: 16 }}>
          <Text strong style={{ display: "block" }}>
            {t("kb_maintenance.review_inherited_title", { n: review.inherited.length })}
          </Text>
          <Text type="secondary" style={{ display: "block", fontSize: 12, marginBottom: 8 }}>
            {t("kb_maintenance.inherited_hint")}
          </Text>
          {review.inherited.map((passage) => (
            <PassageCard
              key={passage.id}
              baseName={baseName}
              documentId={documentId}
              readScope={readScope}
              passage={passage}
              figureIds={figureIds}
              readonly={readonly}
              busy={busy}
              switching={switching === passage.id}
              onSwitch={(p, enabled) => void settle(p, enabled)}
              onEdit={onEdit}
              onHistory={onHistory}
            />
          ))}
        </section>
      )}
      {review.unapplied.length > 0 && (
        <section data-testid="kb-review-unapplied">
          <Space style={{ display: "flex", justifyContent: "space-between" }} align="start">
            <Text strong>
              {t("kb_maintenance.review_unapplied_title", { n: review.unapplied.length })}
            </Text>
            <DiscardButton
              count={review.unapplied.length}
              readonly={readonly}
              canDelete={canDelete}
              busy={discarding}
              onConfirm={() => void discard()}
            />
          </Space>
          <Text type="secondary" style={{ display: "block", fontSize: 12, margin: "4px 0 8px" }}>
            {t("kb_maintenance.review_unapplied_hint")}
          </Text>
          {review.unapplied.map((record) => (
            <UnappliedRow key={`${record.passage_id}-${record.revision}`} record={record} />
          ))}
        </section>
      )}
    </div>
  );
}
