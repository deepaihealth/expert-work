/**
 * Documents still cut by older chunking parameters — B-175 P3-PR3 (B-206 §6
 * 库设置 and 「保存即问」).
 *
 * ``useStaleDocuments`` reads the count (with the read scope) and runs the
 * re-ingest behind one confirm, used both by the settings line's button and
 * right after a save that changed the chunking. Each result is one message
 * (one i18n key each): what was submitted, what was skipped (no original
 * file), what is left for another call.
 */
import { useCallback, useEffect, useRef, useState, type ReactElement } from "react";
import { App, Button, Space, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { ApiError, errMessage } from "../../api/client";
import { getStaleDocuments, reingestStale } from "../../api/knowledge_maintenance";
import { ReadonlyTooltip } from "../../components/ReadonlyTooltip";

const { Text } = Typography;

export interface StaleDocuments {
  /** ``null`` until read (or when the read failed: ``error``). */
  stale: number | null;
  error: string | null;
  submitting: boolean;
  /** Read the count again; resolves to it (``null`` on failure). */
  refresh: () => Promise<number | null>;
  /** Ask, then re-ingest on yes; resolves once the person decided. */
  confirmReingest: (count: number) => Promise<void>;
}

export function useStaleDocuments(baseName: string, readScope: string | undefined): StaleDocuments {
  const { t } = useTranslation();
  const { message, modal } = App.useApp();
  const [stale, setStale] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const latestRead = useRef(0);

  const refresh = useCallback(async (): Promise<number | null> => {
    const read = ++latestRead.current;
    try {
      const found = await getStaleDocuments(baseName, readScope);
      // An answer without a count says nothing (shown as nothing).
      const count = typeof found.stale === "number" ? found.stale : null;
      if (read === latestRead.current) {
        setStale(count);
        setError(null);
      }
      return count;
    } catch (err) {
      if (read === latestRead.current) setError(errMessage(err));
      return null;
    }
  }, [baseName, readScope]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const submit = useCallback(async () => {
    setSubmitting(true);
    try {
      const result = await reingestStale(baseName);
      message.success(t("kb_maintenance.stale_submitted", { n: result.queued.length }));
      if (result.skipped.length > 0) {
        message.warning(t("kb_maintenance.stale_skipped", { n: result.skipped.length }));
      }
      if (result.remaining > 0) {
        message.info(t("kb_maintenance.stale_remaining", { n: result.remaining }));
      }
    } catch (err) {
      message.error(
        err instanceof ApiError && err.status === 503
          ? t("knowledge_page.embedder_missing")
          : errMessage(err),
      );
    } finally {
      setSubmitting(false);
      await refresh();
    }
  }, [baseName, message, refresh, t]);

  const confirmReingest = useCallback(
    (count: number) =>
      new Promise<void>((resolve) => {
        modal.confirm({
          title: t("kb_maintenance.stale_confirm_title", { count }),
          content: t("kb_maintenance.stale_confirm_body"),
          okText: t("kb_maintenance.stale_reingest"),
          cancelText: t("kb_maintenance.stale_confirm_later"),
          okButtonProps: { "data-testid": "kb-stale-confirm-ok" },
          onOk: async () => {
            await submit();
            resolve();
          },
          onCancel: () => resolve(),
        });
      }),
    [modal, submit, t],
  );

  return { stale, error, submitting, refresh, confirmReingest };
}

interface StaleDocumentsLineProps {
  state: StaleDocuments;
  readonly: boolean;
}

/** 「N 份文档还是按旧的切块参数切的。」 + 「全部重新摄取」. */
export function StaleDocumentsLine({ state, readonly }: StaleDocumentsLineProps): ReactElement {
  const { t } = useTranslation();
  const { stale, error, submitting, confirmReingest } = state;
  return (
    <div data-testid="kb-settings-stale" style={{ marginBottom: 16 }}>
      {error !== null ? (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t("kb_maintenance.stale_unknown", { error })}
        </Text>
      ) : stale === null ? null : stale === 0 ? (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t("kb_maintenance.stale_none")}
        </Text>
      ) : (
        <Space size={8} wrap>
          <Text type="warning" style={{ fontSize: 12 }}>
            {t("kb_maintenance.stale_line", { count: stale })}
          </Text>
          <ReadonlyTooltip on={readonly}>
            <Button
              size="small"
              loading={submitting}
              disabled={readonly}
              onClick={() => void confirmReingest(stale)}
              data-testid="kb-settings-reingest-stale"
            >
              {t("kb_maintenance.stale_reingest")}
            </Button>
          </ReadonlyTooltip>
        </Space>
      )}
    </div>
  );
}
