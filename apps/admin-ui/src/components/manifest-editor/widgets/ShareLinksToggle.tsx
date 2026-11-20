/**
 * ShareLinksToggle — B-326「分享文件链接」勾选框 + 有效期。
 *
 * 和上面的工具勾选框同一个样式、同一列；有效期是它下面缩进的一行子选项。
 *
 * 开:写 ``share_links: {enabled: true}``(用户填过有效期才带 ``ttl_minutes``)。
 * 关:整块删掉,不写 ``enabled: false`` —— 后端口径是「不写 = 关,且不落库」。
 * 同一次编辑里关了再开,恢复关之前填的有效期(块删掉了,值记在组件里)。
 * 有效期只在开着时显示,5..1440 分钟,留空 = 平台默认 120。手改 YAML 写出范围外的值时,
 * 输入框标红并通过 ``useReportFormErrors`` 拦住保存(后端同样会拒)。
 */
import { Checkbox, InputNumber, Typography } from "antd";
import { useRef } from "react";
import { useTranslation } from "react-i18next";

import { FieldHelp } from "../../FieldHelp";
import { useReportFormErrors } from "../form_errors";
import {
  SHARE_LINKS_TTL_DEFAULT,
  SHARE_LINKS_TTL_MAX,
  SHARE_LINKS_TTL_MIN,
  isValidShareLinksTtl,
  readShareLinks,
  setShareLinksOn,
  setShareLinksTtl,
} from "../form_model";

const { Text } = Typography;

// Lines the lifetime row up under the checkbox label (antd box 16px + 8px gap).
const SUB_OPTION_INDENT = 24;

interface ShareLinksToggleProps {
  formData: unknown;
  onChange: (data: unknown) => void;
}

export function ShareLinksToggle({ formData, onChange }: ShareLinksToggleProps) {
  const { t } = useTranslation();
  const { enabled, ttlMinutes, ttlInvalid } = readShareLinks(formData);
  useReportFormErrors("share_links", ttlInvalid);
  // Turning off removes the block (and its lifetime); turning back on in the same
  // editing session restores the lifetime the user had set.
  const rememberedTtl = useRef<number | undefined>(undefined);
  const toggle = (on: boolean) => {
    if (!on) {
      rememberedTtl.current = isValidShareLinksTtl(ttlMinutes) ? ttlMinutes : undefined;
      onChange(setShareLinksOn(formData, false));
      return;
    }
    const next = setShareLinksOn(formData, true);
    const restored = rememberedTtl.current;
    onChange(
      restored !== undefined && readShareLinks(next).ttlMinutes === undefined
        ? setShareLinksTtl(next, restored)
        : next,
    );
  };
  const invalidText = t("agent_form.share_links_ttl_invalid", {
    min: SHARE_LINKS_TTL_MIN,
    max: SHARE_LINKS_TTL_MAX,
  });

  return (
    <div data-testid="af-share-links">
      <span>
        <Checkbox
          checked={enabled}
          data-testid="af-share-links-checkbox"
          onChange={(e) => toggle(e.target.checked)}
        >
          {t("agent_form.share_links_label")}
        </Checkbox>
        <FieldHelp text={t("agent_form.share_links_help")} testId="af-share-links" />
      </span>
      {!enabled && ttlInvalid && (
        // Hand-edited YAML: the input is hidden while off, but the stored value is
        // still validated by the backend — say why saving is blocked.
        <Text
          type="danger"
          data-testid="af-share-links-ttl-error"
          style={{ display: "block", marginLeft: SUB_OPTION_INDENT }}
        >
          {invalidText}
        </Text>
      )}
      {enabled && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            flexWrap: "wrap",
            gap: 8,
            marginTop: 6,
            marginLeft: SUB_OPTION_INDENT,
          }}
        >
          <Text type="secondary">{t("agent_form.share_links_ttl_label")}</Text>
          <InputNumber
            size="small"
            style={{ width: 96 }}
            data-testid="af-share-links-ttl"
            aria-label={t("agent_form.share_links_ttl_label")}
            min={SHARE_LINKS_TTL_MIN}
            max={SHARE_LINKS_TTL_MAX}
            precision={0}
            value={ttlMinutes ?? null}
            placeholder={String(SHARE_LINKS_TTL_DEFAULT)}
            status={ttlInvalid ? "error" : undefined}
            onChange={(v) => onChange(setShareLinksTtl(formData, v ?? undefined))}
          />
          <Text type="secondary">
            {ttlInvalid
              ? invalidText
              : t("agent_form.share_links_ttl_hint", { default: SHARE_LINKS_TTL_DEFAULT })}
          </Text>
        </div>
      )}
    </div>
  );
}
