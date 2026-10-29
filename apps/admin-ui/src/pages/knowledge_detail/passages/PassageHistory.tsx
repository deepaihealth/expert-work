/**
 * A passage's revision history — B-175 P3-PR3.
 *
 * Newest first. Revision 1 of a passage cut from the file has no author:
 * 「入库原文」. Restoring writes an older revision's text back as a new
 * revision, against the revision the passage was read at; when someone
 * changed it in between (409) the history is read again and the list shows
 * the passage as it stands. Read-only viewers see the history but cannot
 * restore.
 */
import { useCallback, useEffect, useRef, useState, type ReactElement } from "react";
import { App, Button, Card, Empty, Modal, Popconfirm, Skeleton, Space, Tag, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { errMessage } from "../../../api/client";
import {
  listPassageRevisions,
  restorePassage,
  type Passage,
  type PassageRevision,
} from "../../../api/knowledge_maintenance";
import { HeadingPath } from "../HeadingPath";
import { usePassageLabel, WriteGate } from "./PassageCard";
import { passageWriteError } from "./passageErrors";

const { Paragraph, Text } = Typography;

export interface PassageHistoryProps {
  baseName: string;
  documentId: string;
  readScope: string | undefined;
  /** The passage whose history to show; ``null`` = closed. */
  passage: Passage | null;
  readonly: boolean;
  busy: boolean;
  onClose: () => void;
  /** The passage as it stands now (after a restore, or as a conflict showed it). */
  onChanged: (passage: Passage) => void;
  onGone: () => void;
}

function RevisionCard({
  record,
  current,
  readonly,
  busy,
  restoring,
  onRestore,
}: {
  record: PassageRevision;
  current: boolean;
  readonly: boolean;
  busy: boolean;
  restoring: boolean;
  onRestore: (revision: number) => void;
}): ReactElement {
  const { t } = useTranslation();
  const who =
    record.edited_by === null
      ? t("kb_maintenance.history_from_file")
      : t("kb_maintenance.history_by", { who: record.edited_by });
  return (
    <Card
      size="small"
      style={{ marginBottom: 12 }}
      data-testid={`kb-passage-revision-${record.revision}`}
      title={
        <Space size={6} wrap>
          <Text strong>{t("kb_maintenance.history_revision", { revision: record.revision })}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {who}
            {record.created_at ? ` · ${new Date(record.created_at).toLocaleString()}` : ""}
          </Text>
          {current && (
            <Tag color="blue" bordered={false}>
              {t("kb_maintenance.history_current")}
            </Tag>
          )}
        </Space>
      }
      extra={
        current ? null : (
          <WriteGate readonly={readonly} busy={busy}>
            <Popconfirm
              title={t("kb_maintenance.history_restore_confirm", { revision: record.revision })}
              okText={t("kb_maintenance.history_restore")}
              onConfirm={() => onRestore(record.revision)}
              disabled={readonly || busy}
            >
              <Button
                size="small"
                loading={restoring}
                disabled={readonly || busy}
                data-testid={`kb-passage-restore-${record.revision}`}
              >
                {t("kb_maintenance.history_restore")}
              </Button>
            </Popconfirm>
          </WriteGate>
        )
      }
    >
      <HeadingPath path={record.heading_path} />
      <Paragraph
        style={{ whiteSpace: "pre-wrap", marginBottom: 0, fontSize: 13 }}
        ellipsis={{ rows: 6, expandable: true }}
      >
        {record.content}
      </Paragraph>
    </Card>
  );
}

/** Mounted while the history is open, so each opening starts from the
 *  passage's revision as read. */
function HistoryBody({
  baseName,
  documentId,
  readScope,
  passage,
  readonly,
  busy,
  onClose,
  onChanged,
  onGone,
}: Omit<PassageHistoryProps, "passage"> & { passage: Passage }): ReactElement {
  const { t } = useTranslation();
  const { message } = App.useApp();
  /** The passage's revision as last read (what a restore must name). */
  const [currentRevision, setCurrentRevision] = useState(passage.revision);
  const [revisions, setRevisions] = useState<PassageRevision[]>([]);
  const [loading, setLoading] = useState(true);
  const [restoring, setRestoring] = useState<number | null>(null);
  const latestRead = useRef(0);

  const load = useCallback(async () => {
    const read = ++latestRead.current;
    setLoading(true);
    try {
      const found = await listPassageRevisions(baseName, documentId, passage.id, readScope);
      if (read === latestRead.current) setRevisions(found);
    } catch (err) {
      if (read === latestRead.current) message.error(errMessage(err));
    } finally {
      if (read === latestRead.current) setLoading(false);
    }
  }, [baseName, documentId, passage.id, readScope, message]);

  useEffect(() => {
    void load();
  }, [load]);

  const handleRestore = async (revision: number) => {
    setRestoring(revision);
    try {
      const restored = await restorePassage(
        baseName,
        documentId,
        passage.id,
        revision,
        currentRevision,
      );
      setCurrentRevision(restored.revision);
      message.success(t("kb_maintenance.history_restored", { revision: restored.revision }));
      onChanged(restored);
    } catch (err) {
      const failure = passageWriteError(t, err);
      if (failure.kind === "conflict") {
        setCurrentRevision(failure.passage.revision);
        message.warning(t("kb_maintenance.history_conflict"));
        onChanged(failure.passage);
      } else if (failure.kind === "gone") {
        message.warning(t("kb_maintenance.error_gone"));
        onGone();
        onClose();
        return;
      } else {
        message.error(failure.text);
      }
    } finally {
      setRestoring(null);
    }
    await load();
  };

  if (loading && revisions.length === 0) return <Skeleton active paragraph={{ rows: 4 }} />;
  if (revisions.length === 0) return <Empty description={t("kb_maintenance.history_empty")} />;
  return (
    <>
      {[...revisions].reverse().map((record) => (
        <RevisionCard
          key={record.revision}
          record={record}
          current={record.revision === currentRevision}
          readonly={readonly}
          busy={busy}
          restoring={restoring === record.revision}
          onRestore={(target) => void handleRestore(target)}
        />
      ))}
    </>
  );
}

export function PassageHistory({ passage, ...props }: PassageHistoryProps): ReactElement {
  const { t } = useTranslation();
  const label = usePassageLabel();
  return (
    <Modal
      open={passage !== null}
      onCancel={props.onClose}
      footer={null}
      width={720}
      destroyOnHidden
      title={passage ? t("kb_maintenance.history_title", { label: label(passage) }) : ""}
    >
      <div data-testid="kb-passage-history">
        {passage !== null && <HistoryBody {...props} passage={passage} />}
      </div>
    </Modal>
  );
}
