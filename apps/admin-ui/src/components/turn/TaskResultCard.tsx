/**
 * i18n note: reads the ``playground.*`` / ``tool_timeline.*`` namespaces —
 * ``playground.*`` is now a **cross-page shared** namespace (see
 * ``components/turn/types.ts``).
 */
import { Button, Tag, Typography } from "antd";
import { ExternalLink } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";

import type { FireNowResult } from "../../api/triggers";
import { MarkdownView } from "../MarkdownView";

const { Text } = Typography;

/** Spec 1 PR4 Task 5 — one fire-now result rendered inline in the transcript.
 *  ``taskResults`` accumulates every ``FireNowResult`` the manage_task card's
 *  「立即触发」 button reports (via ``onFireResult``, threaded down through
 *  TurnCard → StepTimeline → AgentStepCard → ToolCallCard); this renders the
 *  delivered answer (or a pending hint) plus created/fired/completed
 *  lifecycle chips derived purely from the result — no extra audit fetch.
 *  「查看运行」 opens the fired run's own conversation, where the full
 *  step/tool/trace view already lives (reused, not re-embedded here). */
export function TaskResultCard({ result }: { result: FireNowResult }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const succeeded = result.trigger_run_status === "succeeded";
  const failed = result.trigger_run_status === "failed";
  const completedColor = succeeded ? "success" : failed ? "error" : "default";
  // Terminal outcomes both read as "completed" (color carries success/failure);
  // still in-flight reuses the tool card's own 「已触发,运行中」 wording.
  const completedLabel =
    succeeded || failed
      ? t("playground.lifecycle_completed")
      : t("tool_timeline.fire_pending");

  return (
    <div
      data-testid="playground-task-result"
      style={{
        border: "1px solid var(--ew-border-subtle)",
        borderRadius: 6,
        overflow: "hidden",
        flexShrink: 0,
      }}
    >
      <div
        style={{
          padding: "8px 12px",
          background: "var(--ew-surface-raised)",
          borderBottom: "1px solid var(--ew-border-subtle)",
          display: "flex",
          alignItems: "center",
          gap: 8,
          flexWrap: "wrap",
        }}
      >
        <Text strong style={{ fontSize: 13 }}>
          {t("playground.task_result")}
        </Text>
        <Tag bordered={false} color="default" style={{ margin: 0 }}>
          {t("playground.lifecycle_created")}
        </Tag>
        <Tag bordered={false} color="processing" style={{ margin: 0 }}>
          {t("playground.lifecycle_fired")}
        </Tag>
        <Tag
          bordered={false}
          color={completedColor}
          style={{ margin: 0 }}
          data-testid="playground-task-result-completed"
        >
          {completedLabel}
        </Tag>
        {result.thread_id !== "" ? (
          <Button
            size="small"
            type="link"
            icon={<ExternalLink size={12} strokeWidth={1.75} />}
            onClick={() =>
              navigate(`/conversations/${encodeURIComponent(result.thread_id)}`)
            }
            style={{ marginLeft: "auto" }}
            data-testid="playground-task-result-view-run"
          >
            {t("playground.view_run")}
          </Button>
        ) : null}
      </div>
      <div style={{ padding: "8px 12px" }}>
        {result.delivery === "delivered" && result.delivered_text ? (
          <MarkdownView>{result.delivered_text}</MarkdownView>
        ) : result.delivery === "pending" ? (
          <Text
            type="secondary"
            style={{ fontSize: 12 }}
            data-testid="playground-task-result-pending"
          >
            {t("playground.fire_pending")}
          </Text>
        ) : (
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t("tool_timeline.fire_failed")}
          </Text>
        )}
      </div>
    </div>
  );
}
