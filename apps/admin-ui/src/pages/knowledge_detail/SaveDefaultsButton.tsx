/**
 * 「存为库默认参数」 — B-175 P2-PR4 (master plan step 5; B-175 v2 §7 命中测试).
 *
 * Saves the retrieval test's parameters that differ from the base's defaults
 * (``saveDefaults``) after a confirmation listing each change. A write to the
 * caller's own tenant (PATCH binds the base by name there), so it is off on
 * another tenant's base; off too when nothing differs. A refusal (a viewer
 * has no ``manifest:write``) shows as an error, as on the other tabs.
 */
import { useCallback, useState } from "react";
import { App, Button, Popconfirm, Typography } from "antd";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";

import { ApiError } from "../../api/client";
import { updateBase, type KnowledgeBase } from "../../api/knowledge";
import { ReadonlyTooltip } from "../../components/ReadonlyTooltip";
import {
  defaultsBody,
  defaultsChanges,
  type DefaultsChange,
  type TestParams,
} from "./saveDefaults";

const { Text } = Typography;

const FIELD_LABEL: Record<DefaultsChange["field"], string> = {
  retrieval_top_k: "knowledge_page.field_top_k",
  retrieval_method: "knowledge_page.field_method",
  retrieval_score_threshold: "knowledge_page.field_threshold",
  rerank_enabled: "knowledge_page.field_rerank",
  rerank_score_threshold: "knowledge_page.field_rerank_threshold",
};

function valueText(t: TFunction, change: DefaultsChange, value: DefaultsChange["to"]): string {
  if (value === null) return t("knowledge_retrieval.value_none");
  if (typeof value === "boolean") {
    return t(value ? "knowledge_retrieval.value_on" : "knowledge_retrieval.value_off");
  }
  if (change.field === "retrieval_method") return t(`knowledge_page.method_${value}`);
  return String(value);
}

function errMessage(err: unknown): string {
  return err instanceof ApiError
    ? `${err.code}: ${err.message}`
    : err instanceof Error
      ? err.message
      : "unknown error";
}

interface SaveDefaultsButtonProps {
  base: KnowledgeBase;
  params: TestParams;
  readonly: boolean;
  /** The base as the PATCH returned it. */
  onSaved?: (next: KnowledgeBase) => void;
}

export function SaveDefaultsButton({ base, params, readonly, onSaved }: SaveDefaultsButtonProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [saving, setSaving] = useState(false);
  const changes = defaultsChanges(base.retrieval_config, params);

  const save = useCallback(async () => {
    setSaving(true);
    try {
      const next = await updateBase(base.name, defaultsBody(changes));
      message.success(t("knowledge_retrieval.save_defaults_done"));
      onSaved?.(next);
    } catch (err) {
      message.error(errMessage(err));
    } finally {
      setSaving(false);
    }
  }, [base.name, changes, message, onSaved, t]);

  const off = readonly || changes.length === 0;
  return (
    <ReadonlyTooltip
      on={off}
      title={readonly ? undefined : t("knowledge_retrieval.save_defaults_same")}
    >
      <Popconfirm
        title={t("knowledge_retrieval.save_defaults_confirm")}
        description={
          <div data-testid="kb-test-save-defaults-changes">
            {changes.map((change) => (
              <div key={change.field}>
                {t(FIELD_LABEL[change.field])}: {valueText(t, change, change.from)} →{" "}
                {valueText(t, change, change.to)}
              </div>
            ))}
            {changes.some((c) => c.field === "rerank_score_threshold") && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                {t("knowledge_retrieval.save_defaults_rerank_note")}
              </Text>
            )}
          </div>
        }
        okText={t("knowledge_retrieval.save_defaults")}
        cancelText={t("common.cancel")}
        onConfirm={() => void save()}
        disabled={off}
      >
        <Button size="small" loading={saving} disabled={off} data-testid="kb-test-save-defaults">
          {t("knowledge_retrieval.save_defaults")}
        </Button>
      </Popconfirm>
    </ReadonlyTooltip>
  );
}
