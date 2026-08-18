/**
 * AttachmentChips — closable tags for staged upload attachments, lifted
 * verbatim out of ``PlaygroundTab.tsx`` (调试台重设计 PR-A Task 7; see
 * ``playground-attachments`` / ``playground-attachment`` testids there).
 */
import type { JSX } from "react";
import { Tag, Typography } from "antd";
import { X } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { Attachment } from "../turn/types";

const { Text } = Typography;

export interface AttachmentChipsProps {
  attachments: readonly Attachment[];
  onRemove: (id: string) => void;
}

export function AttachmentChips({
  attachments,
  onRemove,
}: AttachmentChipsProps): JSX.Element | null {
  const { t } = useTranslation();
  if (attachments.length === 0) return null;
  return (
    <div data-testid="playground-attachments">
      <Text type="secondary" style={{ fontSize: 12 }}>
        {t("playground.attachments_label")}
      </Text>
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: 6,
          marginTop: 6,
        }}
      >
        {attachments.map((a) => (
          <Tag
            key={a.id}
            closable
            onClose={(e) => {
              e.preventDefault();
              onRemove(a.id);
            }}
            closeIcon={
              <X
                size={11}
                strokeWidth={1.75}
                aria-label={t("playground.remove_attachment")}
              />
            }
            bordered={false}
            data-testid="playground-attachment"
          >
            {a.name}
          </Tag>
        ))}
      </div>
    </div>
  );
}
