/**
 * B-73 ② —— 一轮里平台自己贴进检查点的隐藏行,渲染成默认折叠的一段。
 *
 * 这类行(B-67 的「本轮输入」段、CM-1 的 ``<recovery-advisory>``)是 HumanMessage,
 * 但**不是用户说的话**。对话气泡视图按设计不显示它们;**跨租户审计视图**要的恰恰
 * 是忠实记录(后端为此专门走 ``include_hidden=True``),此前界面却把它们直接丢掉 ——
 * 特意要来的东西又不见了。
 *
 * 所以这里既不能混进用户气泡(那会把平台文案说成用户说的),也不能不显示。折叠
 * 一段、标上「平台自动生成」,是两者之间唯一诚实的形态。
 */
import { Tag, Typography } from "antd";
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

const { Text } = Typography;

export interface PlatformRowsProps {
  /** 这一轮的隐藏行原文,按它们在检查点里的顺序。空数组时本组件不渲染。 */
  lines: readonly string[];
}

export function PlatformRows({ lines }: PlatformRowsProps): JSX.Element | null {
  const { t } = useTranslation();
  if (lines.length === 0) return null;
  return (
    <details data-testid="platform-rows" style={{ opacity: 0.75, margin: "4px 0" }}>
      <summary style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 8 }}>
        <Tag color="default">{t("conversations_detail.platform_rows_tag")}</Tag>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t("conversations_detail.platform_rows_hint", { count: lines.length })}
        </Text>
      </summary>
      <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 8 }}>
        {lines.map((line, i) => (
          <pre
            // 平台文本,没有比下标更稳的 key;这一份在一次渲染里不会重排。
            key={i}
            data-testid="platform-row"
            style={{
              margin: 0,
              padding: 8,
              borderRadius: 6,
              background: "var(--ew-surface-muted, rgba(0,0,0,0.04))",
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              fontSize: 12,
            }}
          >
            {line}
          </pre>
        ))}
      </div>
    </details>
  );
}
