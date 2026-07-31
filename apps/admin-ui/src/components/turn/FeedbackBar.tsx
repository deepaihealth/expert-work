/**
 * i18n note: reads the ``playground.*`` namespace — now a **cross-page
 * shared** namespace (see ``components/turn/types.ts``).
 */
import { useCallback, useState } from "react";
import { Button, Input, Popover, Typography } from "antd";
import { ThumbsDown, ThumbsUp } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../../api/client";
import { submitSessionFeedback } from "../../api/sessions";

const { Text } = Typography;

/** SE-16 (SE-A46) — the per-turn 👍/👎 bar. 👍 submits immediately; 👎 opens
 *  a comment popover (the user's own words are the highest-value failure
 *  label for the distiller). One submission per turn; errors surface inline
 *  (fire-and-forget signal — never blocks the conversation).
 *
 *  ``disabled`` (Track C W2) — 切入态只读:反馈是写操作,置灰两个按钮。 */
export function FeedbackBar({
  threadId,
  turnSeq,
  disabled = false,
}: {
  threadId: string;
  turnSeq: number;
  disabled?: boolean;
}) {
  const { t } = useTranslation();
  const [submitted, setSubmitted] = useState<"up" | "down" | null>(null);
  const [busy, setBusy] = useState(false);
  const [commentOpen, setCommentOpen] = useState(false);
  const [comment, setComment] = useState("");
  const [error, setError] = useState<string | null>(null);

  const submit = useCallback(
    async (rating: "up" | "down", text?: string) => {
      setBusy(true);
      setError(null);
      try {
        await submitSessionFeedback(threadId, {
          rating,
          comment: text?.trim() || undefined,
          turn_seq: turnSeq,
        });
        setSubmitted(rating);
        setCommentOpen(false);
      } catch (err) {
        setError(
          err instanceof ApiError ? `${err.code}: ${err.message}` : String(err),
        );
      } finally {
        setBusy(false);
      }
    },
    [threadId, turnSeq],
  );

  return (
    <div
      style={{ marginTop: 6, display: "flex", gap: 4, alignItems: "center" }}
      data-testid="playground-turn-feedback"
    >
      <Button
        type="text"
        size="small"
        disabled={disabled || busy || submitted !== null}
        onClick={() => void submit("up")}
        aria-label={t("playground.feedback_up")}
        data-testid="playground-feedback-up"
        icon={
          <ThumbsUp
            size={13}
            strokeWidth={1.75}
            color={submitted === "up" ? "var(--ew-status-success, #52c41a)" : undefined}
          />
        }
      />
      <Popover
        open={commentOpen}
        onOpenChange={(open) => {
          if (!disabled && submitted === null && !busy) setCommentOpen(open);
        }}
        trigger="click"
        content={
          <div style={{ width: 260 }}>
            <Input.TextArea
              rows={3}
              maxLength={4000}
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              placeholder={t("playground.feedback_comment_placeholder")}
              aria-label={t("playground.feedback_comment_placeholder")}
              data-testid="playground-feedback-comment"
            />
            <div style={{ marginTop: 8, textAlign: "right" }}>
              <Button
                type="primary"
                size="small"
                loading={busy}
                onClick={() => void submit("down", comment)}
                data-testid="playground-feedback-down-submit"
              >
                {t("playground.feedback_comment_submit")}
              </Button>
            </div>
          </div>
        }
      >
        <Button
          type="text"
          size="small"
          disabled={disabled || busy || submitted !== null}
          aria-label={t("playground.feedback_down")}
          data-testid="playground-feedback-down"
          icon={
            <ThumbsDown
              size={13}
              strokeWidth={1.75}
              color={
                submitted === "down" ? "var(--ew-status-error, #f5222d)" : undefined
              }
            />
          }
        />
      </Popover>
      {submitted !== null && (
        <Text type="secondary" style={{ fontSize: 11 }}>
          {t("playground.feedback_thanks")}
        </Text>
      )}
      {error !== null && (
        <Text type="danger" style={{ fontSize: 11 }} data-testid="playground-feedback-error">
          {t("playground.feedback_failed")}: {error}
        </Text>
      )}
    </div>
  );
}
