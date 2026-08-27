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
import { useTranslation } from "react-i18next";

import { CopyButton } from "../CopyButton";
import type { Attachment } from "../turn/types";

const { Text } = Typography;

/** 入参总长超过这个字符数就折叠成「入参 N 项」—— 对外派发的入参常带整段
 *  OSS URL / 免责声明 / JSON,摊开是一堵墙(2026-08-26 用户反馈)。 */
const INPUTS_INLINE_CHARS = 120;

/** ③ 反馈 — 排序:``order``(Agent 配置 variables 声明序)命中的键按声明序
 *  在前,未声明的键按原相对序拖后;不传 ``order`` 原样返回(后端 JSON 序)。 */
function orderEntries(
  entries: readonly (readonly [string, string])[],
  order: readonly string[] | undefined,
): readonly (readonly [string, string])[] {
  if (order === undefined || order.length === 0) return entries;
  const declared = new Set(order);
  const head: (readonly [string, string])[] = [];
  for (const key of order) {
    const hit = entries.find(([k]) => k === key);
    if (hit !== undefined) head.push(hit);
  }
  return [...head, ...entries.filter(([k]) => !declared.has(k))];
}

export interface UserBubbleProps {
  input: string;
  attachments: readonly Attachment[];
  /** #10 — the jinja prompt-variable values this turn was dispatched with
   *  (``Turn.inputs``). Rendered below the bubble: short sets inline (one
   *  ``key=value`` per line), long sets folded behind 「入参 N 项」. */
  inputs?: Record<string, string>;
  /** ③ 反馈 — Agent 配置 variables 的声明序。省略 → 保持后端 JSON 序
   *  (拿不到 manifest 的页面不传,零变化)。 */
  inputOrder?: readonly string[];
}

export function UserBubble({ input, attachments, inputs, inputOrder }: UserBubbleProps) {
  const { t } = useTranslation();
  const inputEntries = orderEntries(inputs ? Object.entries(inputs) : [], inputOrder);
  const inputsTotalChars = inputEntries.reduce((n, [k, v]) => n + k.length + v.length + 1, 0);

  // 一键一行 + `overflowWrap: anywhere`:原来拼成一行且容器无宽度上限,
  // 超长 URL 会把整行顶出气泡区被裁掉(BUG:inputs 展示溢出)。
  const inputRows = (
    <div
      style={{
        fontFamily: "var(--ew-font-mono)",
        fontSize: 11,
        color: "var(--ew-text-tertiary)",
        textAlign: "left",
      }}
    >
      {/* ③ 反馈 — 行尾复制按钮,复制值本身(入参常是整段 URL / JSON,拿去
          别处用);flex + 按钮 flexShrink:0,长值换行不挤崩按钮。 */}
      {inputEntries.map(([k, v]) => (
        <div
          key={k}
          data-testid="console-turn-input-row"
          style={{ display: "flex", alignItems: "center", gap: 2 }}
        >
          <span style={{ flex: 1, minWidth: 0, overflowWrap: "anywhere" }}>
            {k}={v}
          </span>
          <span style={{ flexShrink: 0 }}>
            <CopyButton text={v} testId={`console-turn-input-copy-${k}`} />
          </span>
        </div>
      ))}
    </div>
  );

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
              // 终审 F2 — antd Tag 默认近黑文字在品牌深蓝底上 ~2.8:1,半透明
              // 白底白字两主题都可读。
              <Tag
                key={a.id}
                bordered={false}
                style={{
                  fontSize: 11,
                  background: "rgba(255,255,255,0.22)",
                  color: "#fff",
                }}
              >
                {a.name}
              </Tag>
            ))}
          </div>
        )}
      </div>
      {inputEntries.length > 0 && (
        <div data-testid="console-turn-inputs" style={{ maxWidth: "85%" }}>
          {inputsTotalChars <= INPUTS_INLINE_CHARS ? (
            inputRows
          ) : (
            <details>
              <summary
                style={{
                  cursor: "pointer",
                  fontSize: 11,
                  color: "var(--ew-text-tertiary)",
                  listStylePosition: "inside",
                }}
              >
                {t("console.turn_inputs_fold", { n: inputEntries.length })}
              </summary>
              {inputRows}
            </details>
          )}
        </div>
      )}
    </div>
  );
}
