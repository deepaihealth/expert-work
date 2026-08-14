/**
 * EventCard — shared raw-SSE-frame renderer for RunDetail's EventStreamPanel
 * and the agent Playground (Batch 1 left these as two near-duplicate copies;
 * this is the merge). Shows the event tag, the received-at timestamp (when
 * present) and the frame's ``id`` (when non-null), plus the raw payload with
 * a copy affordance.
 */
import { Tag, Typography } from "antd";

import type { SseEvent } from "../api/sessions";
import { CopyButton } from "./CopyButton";

const { Text } = Typography;

const EVENT_COLOR: Record<string, string> = {
  metadata: "blue",
  updates: "geekblue",
  approval: "gold",
  error: "red",
  end: "green",
  compaction: "purple",
  // P3 PR-1 的两个新帧型。``gap`` = 这条连接补不到的一段(不是错误,别用红);
  // ``truncated`` 通常被 SDK 的翻页吃掉,翻不动了才会露到这里。
  gap: "orange",
  truncated: "orange",
};

export function EventCard({ evt }: { evt: SseEvent }) {
  const tagColor = EVENT_COLOR[evt.event] ?? "default";
  const display = typeof evt.data === "string" ? evt.data : JSON.stringify(evt.data, null, 2);
  return (
    <div
      style={{
        border: "1px solid var(--ew-border-subtle)",
        borderRadius: 4,
        padding: 8,
        background: "var(--ew-surface-raised)",
      }}
      data-testid={`event-card-${evt.event}`}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          marginBottom: 4,
          fontSize: 11,
        }}
      >
        <Tag color={tagColor} bordered={false} style={{ margin: 0 }}>
          {evt.event}
        </Tag>
        {evt.receivedAt && (
          <Text type="secondary" style={{ fontSize: 11 }} className="mono">
            {new Date(evt.receivedAt).toLocaleTimeString()}
          </Text>
        )}
        {evt.id !== null && (
          <Text type="secondary" style={{ fontSize: 11 }} className="mono">
            {evt.id}
          </Text>
        )}
        <span style={{ marginLeft: "auto" }}>
          <CopyButton text={display} testId="event-card-copy" />
        </span>
      </div>
      <pre
        style={{
          margin: 0,
          fontSize: 11,
          fontFamily: "var(--ew-font-mono)",
          color: "var(--ew-text-secondary)",
          whiteSpace: "pre-wrap",
          wordBreak: "break-all",
          maxHeight: 280,
          overflow: "auto",
        }}
      >
        {display}
      </pre>
    </div>
  );
}
