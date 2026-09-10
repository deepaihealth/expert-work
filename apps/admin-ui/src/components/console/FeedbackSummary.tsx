/**
 * FeedbackSummary — read-only rendering of the 👍/👎 already recorded on one
 * turn (P-2).
 *
 * The conversation page is read-only, so this is the only feedback
 * affordance there; the playground keeps ``FeedbackBar`` for *writing* a
 * vote. The two are different things and can in principle coexist on one
 * footer — this component never gates the bar.
 *
 * Source is always shown: 2026-09-09 拍板「凡是人看的地方必须显示来源」——
 * 审阅员不能把员工打的分和终端用户打的分当成一回事。
 */
import { Tag, Typography } from "antd";
import { ThumbsDown, ThumbsUp } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { SessionFeedbackItem } from "../../api/sessions";

const { Text } = Typography;

export function FeedbackSummary({ items }: { items: readonly SessionFeedbackItem[] }) {
  const { t } = useTranslation();
  return (
    <span
      style={{ display: "inline-flex", gap: 8, alignItems: "center" }}
      data-testid="console-turn-feedback-summary"
    >
      {items.map((item) => (
        <span
          key={item.id}
          style={{ display: "inline-flex", gap: 4, alignItems: "center" }}
          data-testid="console-turn-feedback-item"
        >
          {item.rating === "up" ? (
            <ThumbsUp size={13} strokeWidth={1.75} color="var(--ew-status-success, #52c41a)" />
          ) : (
            <ThumbsDown size={13} strokeWidth={1.75} color="var(--ew-status-error, #f5222d)" />
          )}
          <Text style={{ fontSize: 12 }}>
            {t(item.rating === "up" ? "console.feedback_up_label" : "console.feedback_down_label")}
          </Text>
          {item.comment !== null && (
            <Text
              type="secondary"
              style={{ fontSize: 12, maxWidth: 320 }}
              ellipsis={{ tooltip: item.comment }}
            >
              “{item.comment}”
            </Text>
          )}
          <Tag bordered={false} style={{ fontSize: 11, marginInlineEnd: 0 }}>
            {t(
              item.source === "external"
                ? "console.feedback_source_external"
                : "console.feedback_source_console",
            )}
          </Tag>
        </span>
      ))}
    </span>
  );
}
