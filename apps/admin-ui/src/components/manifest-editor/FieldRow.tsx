/**
 * FieldRow v2 — 一行式字段行(配置页重设计 v2 Task 1)。
 * 布局:标签 | 控件 | 一句大白话(常显) | ⓘ(点击弹长解释) | 已自定义+恢复默认。
 * 默认徽章废除:值===默认 → 行内零噪音;非默认 → 蓝「已自定义」Tag + 恢复默认按钮。
 * 纯展示组件:文案由调用方翻好传入。
 */
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Button, Popover, Tag, Tooltip } from "antd";
import { HelpCircle } from "lucide-react";

export interface FieldRowProps {
  fieldId: string;
  label: string;
  /** 一句大白话,常显 */
  brief: string;
  /** 长解释+场景,点击 ⓘ 弹 Popover;缺省不渲染 ⓘ */
  help?: string;
  isDefault: boolean;
  /** 非默认时渲染「恢复默认」;点击回调(通常 patch undefined) */
  onReset?: () => void;
  /** 恢复默认按钮的 Tooltip:「恢复默认:{resetHint}」;缺省只显按钮 */
  resetHint?: string;
  /** 行内对齐:默认 `center`(单行控件);控件多行高时传 `start`,标签与说明贴在控件首行旁,不漂到中间 */
  align?: "center" | "start";
  children: ReactNode;
}

export function FieldRow({
  fieldId,
  label,
  brief,
  help,
  isDefault,
  onReset,
  resetHint,
  align = "center",
  children,
}: FieldRowProps) {
  const { t } = useTranslation();

  const resetButton = (
    <Button
      type="link"
      size="small"
      data-testid={`field-reset-${fieldId}`}
      style={{ padding: 0, height: "auto" }}
      onClick={onReset}
    >
      {t("manifest_editor.field_reset")}
    </Button>
  );

  return (
    <div
      data-field-id={fieldId}
      style={{
        display: "flex",
        alignItems: align === "start" ? "flex-start" : "center",
        flexWrap: "wrap",
        columnGap: 12,
        rowGap: 4,
        marginBottom: 12,
      }}
    >
      <span style={{ minWidth: 160, flexShrink: 0 }}>{label}</span>
      <span style={{ flexShrink: 0 }}>{children}</span>
      <span
        style={{
          flex: "1 1 200px",
          fontSize: 12,
          color: "var(--ew-text-secondary)",
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
        }}
      >
        <span>{brief}</span>
        {help !== undefined && (
          <Popover
            trigger="click"
            content={
              <div style={{ maxWidth: 360, fontSize: 12, whiteSpace: "pre-line" }}>
                {help}
              </div>
            }
          >
            <button
              type="button"
              aria-label={t("common.field_help")}
              data-testid={`field-help-${fieldId}`}
              style={{
                display: "inline-flex",
                alignItems: "center",
                padding: 0,
                border: "none",
                background: "none",
                cursor: "help",
                color: "var(--ew-text-tertiary, #888)",
              }}
            >
              <HelpCircle size={13} strokeWidth={1.75} />
            </button>
          </Popover>
        )}
      </span>
      {!isDefault && (
        <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          <Tag color="blue" bordered={false} data-testid={`field-customized-${fieldId}`}>
            {t("manifest_editor.field_customized_badge")}
          </Tag>
          {onReset !== undefined &&
            (resetHint !== undefined ? (
              <Tooltip title={t("manifest_editor.field_reset_hint", { value: resetHint })}>
                {resetButton}
              </Tooltip>
            ) : (
              resetButton
            ))}
        </span>
      )}
    </div>
  );
}
