/**
 * One passage (「整块」) in the segment preview — B-175 P3-PR3.
 *
 * Header: 「整块 #n · N 字 · M 个小段」 plus its state (on / off / switched off
 * by inheritance, to review) and 「已编辑 · 修订 r」; then its heading path and
 * text, and its chunks (「小段」) folded below. Actions: the on / off switch,
 * 「保持停用」 for a passage switched off by inheritance (switching either way
 * settles it), edit and history. Writes are disabled while read-only (with
 * the read-only hint) and while the document is being ingested.
 */
import { useState, type ReactElement, type ReactNode } from "react";
import { Button, Card, Space, Switch, Tag, Tooltip, Typography } from "antd";
import { History, Pencil } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { Passage } from "../../../api/knowledge_maintenance";
import { ReadonlyTooltip } from "../../../components/ReadonlyTooltip";
import { ChunkContent } from "../ChunkContent";
import { HeadingPath } from "../HeadingPath";

const { Text } = Typography;

export interface PassageCardProps {
  baseName: string;
  documentId: string;
  readScope: string | undefined;
  passage: Passage;
  figureIds: ReadonlySet<string>;
  readonly: boolean;
  /** The document is being ingested (or a new version waits): no writes. */
  busy: boolean;
  /** A switch in flight for this passage. */
  switching?: boolean;
  onSwitch: (passage: Passage, enabled: boolean) => void;
  onEdit: (passage: Passage) => void;
  onHistory: (passage: Passage) => void;
}

/** 「整块 #n」 / 「小段 #n」 — the label the drawer, editor and history share. */
export function usePassageLabel(): (passage: Passage) => string {
  const { t } = useTranslation();
  return (passage) =>
    t(passage.kind === "parent" ? "knowledge_page.parent_label" : "knowledge_page.chunk_label", {
      index: passage.chunk_index,
    });
}

/** Wraps a write control: the read-only hint wins, then the busy hint. */
export function WriteGate({
  readonly,
  busy,
  children,
}: {
  readonly: boolean;
  busy: boolean;
  children: ReactElement;
}): ReactElement {
  const { t } = useTranslation();
  if (readonly) return <ReadonlyTooltip on>{children}</ReadonlyTooltip>;
  if (busy) {
    return (
      <Tooltip title={t("kb_maintenance.busy_hint")}>
        <span style={{ display: "inline-flex", cursor: "not-allowed" }}>
          <span style={{ display: "inline-flex", pointerEvents: "none" }}>{children}</span>
        </span>
      </Tooltip>
    );
  }
  return children;
}

function StateTag({ passage }: { passage: Passage }): ReactNode {
  const { t } = useTranslation();
  if (passage.enabled) {
    return (
      <Tag color="success" bordered={false}>
        {t("kb_maintenance.tag_enabled")}
      </Tag>
    );
  }
  if (passage.disabled_reason === "inherited") {
    return (
      <Tooltip title={t("kb_maintenance.inherited_hint")}>
        <Tag
          color="orange"
          bordered={false}
          tabIndex={0}
          data-testid={`kb-passage-inherited-${passage.id}`}
        >
          {t("kb_maintenance.tag_inherited")}
        </Tag>
      </Tooltip>
    );
  }
  return <Tag bordered={false}>{t("kb_maintenance.tag_disabled")}</Tag>;
}

export function PassageCard({
  baseName,
  documentId,
  readScope,
  passage,
  figureIds,
  readonly,
  busy,
  switching = false,
  onSwitch,
  onEdit,
  onHistory,
}: PassageCardProps): ReactElement {
  const { t } = useTranslation();
  const label = usePassageLabel()(passage);
  const [open, setOpen] = useState(false);
  const blocked = readonly || busy;
  const inherited = !passage.enabled && passage.disabled_reason === "inherited";
  const isParent = passage.kind === "parent";

  const meta = [
    label,
    t("kb_maintenance.passage_chars", { count: passage.content.length }),
    isParent
      ? t("kb_maintenance.passage_children", { count: passage.children.length })
      : t("kb_maintenance.standalone"),
  ].join(" · ");

  return (
    <Card
      size="small"
      style={{ marginBottom: 12 }}
      data-testid={`kb-passage-card-${passage.id}`}
      title={
        <Space size={6} wrap>
          <Text type="secondary" style={{ fontSize: 12 }} data-testid={`kb-passage-meta-${passage.id}`}>
            {meta}
          </Text>
          <StateTag passage={passage} />
          {passage.edited && (
            <Tag color="blue" bordered={false} data-testid={`kb-passage-edited-${passage.id}`}>
              {t("kb_maintenance.tag_edited", { revision: passage.revision })}
            </Tag>
          )}
        </Space>
      }
      extra={
        <Space size={4}>
          {inherited && (
            <WriteGate readonly={readonly} busy={busy}>
              <Button
                size="small"
                disabled={blocked || switching}
                onClick={() => onSwitch(passage, false)}
                data-testid={`kb-passage-keep-off-${passage.id}`}
              >
                {t("kb_maintenance.keep_off")}
              </Button>
            </WriteGate>
          )}
          <WriteGate readonly={readonly} busy={busy}>
            <Switch
              size="small"
              checked={passage.enabled}
              loading={switching}
              disabled={blocked}
              onChange={(checked) => onSwitch(passage, checked)}
              aria-label={t("kb_maintenance.toggle_aria", { label })}
              data-testid={`kb-passage-toggle-${passage.id}`}
            />
          </WriteGate>
          <WriteGate readonly={readonly} busy={busy}>
            <Tooltip title={t("kb_maintenance.edit")}>
              <Button
                size="small"
                type="text"
                disabled={blocked}
                icon={<Pencil size={14} strokeWidth={1.5} />}
                onClick={() => onEdit(passage)}
                aria-label={t("kb_maintenance.edit")}
                data-testid={`kb-passage-edit-${passage.id}`}
              />
            </Tooltip>
          </WriteGate>
          <Tooltip title={t("kb_maintenance.history")}>
            <Button
              size="small"
              type="text"
              icon={<History size={14} strokeWidth={1.5} />}
              onClick={() => onHistory(passage)}
              aria-label={t("kb_maintenance.history")}
              data-testid={`kb-passage-history-${passage.id}`}
            />
          </Tooltip>
        </Space>
      }
    >
      <HeadingPath path={passage.heading_path} testId={`kb-passage-heading-${passage.id}`} />
      <ChunkContent
        baseName={baseName}
        documentId={documentId}
        readScope={readScope}
        content={passage.content}
        figureIds={figureIds}
      />
      {isParent && passage.children.length > 0 && (
        <div style={{ marginTop: 8 }}>
          <Button
            size="small"
            type="link"
            style={{ paddingInline: 0 }}
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            data-testid={`kb-passage-children-toggle-${passage.id}`}
          >
            {t(open ? "kb_maintenance.hide_children" : "kb_maintenance.show_children")}
          </Button>
          {open &&
            passage.children.map((child) => (
              <div
                key={child.id}
                style={{ borderLeft: "2px solid var(--ew-border-subtle)", paddingLeft: 8, marginTop: 8 }}
                data-testid={`kb-passage-child-${child.id}`}
              >
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {t("knowledge_page.chunk_label", { index: child.chunk_index })}
                  {" · "}
                  {t("kb_maintenance.passage_chars", { count: child.content.length })}
                </Text>
                <HeadingPath path={child.heading_path} />
                <ChunkContent
                  baseName={baseName}
                  documentId={documentId}
                  readScope={readScope}
                  content={child.content}
                  figureIds={figureIds}
                />
              </div>
            ))}
        </div>
      )}
    </Card>
  );
}
