/**
 * UserBubble — the console's right-aligned user-message bubble: input text +
 * attachment tags + (optional) the jinja prompt-variable values this turn was
 * dispatched with, one mono line below the bubble.
 *
 * Style lifted from ``components/turn/TurnCard.tsx``'s historical-turn
 * fallback bubble (``alignSelf: "flex-end"`` + the same padding/border), the
 * only right-aligned user bubble that already existed. Too thin for its own
 * test file (see the Task 10 brief) — covered by Task 11's TurnBlock tests.
 */
import { Tag, Typography } from "antd";

import type { Attachment } from "../turn/types";

const { Text } = Typography;

export interface UserBubbleProps {
  input: string;
  attachments: readonly Attachment[];
  /** #10 — the jinja prompt-variable values this turn was dispatched with
   *  (``Turn.inputs``). Rendered as a `key=value · key=value` line below the
   *  bubble when non-empty. */
  inputs?: Record<string, string>;
}

export function UserBubble({ input, attachments, inputs }: UserBubbleProps) {
  const inputEntries = inputs ? Object.entries(inputs) : [];

  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 4 }}>
      <div
        // BUG-14 — 用户气泡上品牌色:原来与 AI 内容同底色,左右对齐是唯一
        // 区分,扫一眼分不出谁说的。brand-500 深底白字在明暗两主题都成立。
        style={{
          maxWidth: "85%",
          padding: "6px 10px",
          borderRadius: 8,
          fontSize: 13,
          whiteSpace: "pre-wrap",
          background: "var(--ew-color-brand-500)",
          color: "#fff",
        }}
      >
        <Text style={{ whiteSpace: "pre-wrap", fontSize: 13, color: "inherit" }}>{input}</Text>
        {attachments.length > 0 && (
          <div style={{ marginTop: 4 }}>
            {attachments.map((a) => (
              <Tag key={a.id} bordered={false} style={{ fontSize: 11 }}>
                {a.name}
              </Tag>
            ))}
          </div>
        )}
      </div>
      {inputEntries.length > 0 && (
        <div
          data-testid="console-turn-inputs"
          style={{
            fontSize: 11,
            fontFamily: "var(--ew-font-mono)",
            color: "var(--ew-text-tertiary)",
          }}
        >
          {inputEntries.map(([k, v]) => `${k}=${v}`).join(" · ")}
        </div>
      )}
    </div>
  );
}
