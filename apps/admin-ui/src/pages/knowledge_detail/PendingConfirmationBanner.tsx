/**
 * B-222 PR-2 — the documents page's banner while estimates wait: 「N 份文档等待
 * 确认入库，预估合计 ¥X，D 天内不确认将自动撤回」 with 「全部确认」 / 「全部取消」 /
 * 「只看待确认」. It reads the base's ``estimate_summary`` (the whole base,
 * not this page), so it survives a reload when the upload queue is gone.
 * Read-only viewers see the numbers; the buttons are disabled.
 */
import { useState } from "react";
import { Alert, Button, Popconfirm, Space, Switch, Typography } from "antd";
import { useTranslation } from "react-i18next";

import type { EstimateSummary } from "../../api/knowledge";
import { ReadonlyTooltip } from "../../components/ReadonlyTooltip";
import { daysLeft, formatYuan } from "./ingestCost";

const { Text } = Typography;

interface PendingConfirmationBannerProps {
  summary: EstimateSummary | undefined;
  readonly: boolean;
  busy: boolean;
  onlyWaiting: boolean;
  onConfirmAll: () => void;
  /** The waiting documents' ids, read when 「全部取消」's confirm opens. */
  onPrepareWithdrawAll: () => Promise<string[]>;
  /** Withdraw exactly those (one uploaded after the confirm opened stays). */
  onWithdrawAll: (ids: string[]) => void;
  onOnlyWaitingChange: (on: boolean) => void;
}

export function PendingConfirmationBanner({
  summary,
  readonly,
  busy,
  onlyWaiting,
  onConfirmAll,
  onPrepareWithdrawAll,
  onWithdrawAll,
  onOnlyWaitingChange,
}: PendingConfirmationBannerProps) {
  const { t } = useTranslation();
  /** What 「全部取消」 withdraws: read when its confirm opened (``null``: reading). */
  const [withdrawIds, setWithdrawIds] = useState<string[] | null>(null);
  const waiting = summary ? summary.awaiting + summary.duplicates + summary.estimating : 0;
  if (!summary || (waiting === 0 && !onlyWaiting)) return null;
  const parts = [
    t("knowledge_page.estimate_banner_awaiting", {
      count: summary.awaiting,
      amount: formatYuan(summary.total_micros),
    }),
    summary.duplicates > 0
      ? t("knowledge_page.estimate_banner_duplicates", { count: summary.duplicates })
      : null,
    summary.estimating > 0
      ? t("knowledge_page.estimate_banner_estimating", { count: summary.estimating })
      : null,
    summary.earliest_confirm_by
      ? t("knowledge_page.estimate_banner_deadline", {
          count: daysLeft(summary.earliest_confirm_by),
        })
      : null,
  ].filter((part): part is string => part !== null);
  return (
    <Alert
      type="info"
      showIcon
      style={{ marginBottom: 16 }}
      data-testid="doc-estimate-banner"
      message={<Text data-testid="doc-estimate-banner-text">{parts.join(t("knowledge_page.estimate_sep"))}</Text>}
      action={
        <Space size={8} wrap>
          <ReadonlyTooltip on={readonly}>
            <Button
              size="small"
              type="primary"
              disabled={readonly || busy || summary.awaiting === 0}
              onClick={onConfirmAll}
              data-testid="doc-estimate-confirm-all"
            >
              {t("knowledge_page.estimate_confirm_all", { count: summary.awaiting })}
            </Button>
          </ReadonlyTooltip>
          <ReadonlyTooltip on={readonly}>
            <Popconfirm
              title={t("knowledge_page.estimate_withdraw_all_confirm_title", {
                count: withdrawIds?.length ?? 0,
              })}
              description={t("knowledge_page.estimate_withdraw_confirm_body")}
              onOpenChange={(open) => {
                if (!open) return;
                setWithdrawIds(null);
                void onPrepareWithdrawAll()
                  .then(setWithdrawIds)
                  .catch(() => setWithdrawIds([]));
              }}
              okButtonProps={{ disabled: withdrawIds === null || withdrawIds.length === 0 }}
              onConfirm={() => {
                if (withdrawIds && withdrawIds.length > 0) onWithdrawAll(withdrawIds);
              }}
              disabled={readonly || busy || waiting === 0}
            >
              <Button
                size="small"
                disabled={readonly || busy || waiting === 0}
                data-testid="doc-estimate-withdraw-all"
              >
                {t("knowledge_page.estimate_withdraw_all")}
              </Button>
            </Popconfirm>
          </ReadonlyTooltip>
          <Space size={4}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t("knowledge_page.estimate_only_waiting")}
            </Text>
            <Switch
              size="small"
              checked={onlyWaiting}
              onChange={onOnlyWaitingChange}
              aria-label={t("knowledge_page.estimate_only_waiting")}
              data-testid="doc-estimate-only-waiting"
            />
          </Space>
        </Space>
      }
    />
  );
}
