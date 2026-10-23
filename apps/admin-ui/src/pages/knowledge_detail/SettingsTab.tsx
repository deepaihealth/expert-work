/**
 * Settings tab — KB commercial uplift.
 *
 * Edit the base's description + retrieval defaults (PATCH). The embedding model
 * is read-only (platform-level) with a re-index action; renaming is
 * intentionally not offered (agents reference a base by name).
 */
import { useCallback, useState } from "react";
import {
  App,
  Alert,
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  Select,
  Space,
  Switch,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";

import {
  reindexBase,
  updateBase,
  type KnowledgeBase,
  type RetrievalMethod,
} from "../../api/knowledge";
import { ApiError } from "../../api/client";
import { ReadonlyTooltip } from "../../components/ReadonlyTooltip";
import { thresholdPausedNotice } from "./thresholdPausedNotice";

const { Text } = Typography;

interface SettingsFormValues {
  description?: string | null;
  chunk_max_tokens?: number;
  chunk_overlap_tokens?: number;
  retrieval_top_k?: number;
  retrieval_score_threshold?: number | null;
  retrieval_method?: RetrievalMethod;
  rerank_enabled?: boolean;
  rerank_score_threshold?: number | null;
}

function errMessage(err: unknown): string {
  return err instanceof ApiError
    ? `${err.code}: ${err.message}`
    : err instanceof Error
      ? err.message
      : "unknown error";
}

interface SettingsTabProps {
  base: KnowledgeBase;
  onSaved: () => Promise<void> | void;
  /** Cross-tenant W4(review C-2)— 只读态(切入态 ∪ "*" 聚合深链外租户
   *  读),由 KnowledgeDetail 统一判定下传;保存/重建索引一律置灰。 */
  readonly: boolean;
}

export function SettingsTab({ base, onSaved, readonly }: SettingsTabProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [form] = Form.useForm<SettingsFormValues>();
  const [saving, setSaving] = useState(false);
  const [reindexing, setReindexing] = useState(false);
  const [confirming, setConfirming] = useState(false);

  const config = base.retrieval_config;
  const initialValues: SettingsFormValues = {
    description: base.description ?? undefined,
    chunk_max_tokens: base.chunk_max_tokens,
    chunk_overlap_tokens: base.chunk_overlap_tokens,
    retrieval_top_k: config?.top_k,
    retrieval_score_threshold: config?.score_threshold ?? null,
    retrieval_method: config?.method,
    rerank_enabled: config?.rerank_enabled,
    rerank_score_threshold: config?.rerank_score_threshold ?? null,
  };

  const handleSave = useCallback(
    async (values: SettingsFormValues) => {
      setSaving(true);
      // B-206 K-7b —— 重排分数下限一送出就算「在当前重排模型下标定」:只在改过这一栏时送,
      // 存别的设置不会把别的模型下标定的旧下限算成新模型的。
      const thresholdEdited = form.isFieldTouched("rerank_score_threshold");
      try {
        await updateBase(base.name, {
          description: values.description ?? null,
          chunk_max_tokens: values.chunk_max_tokens,
          chunk_overlap_tokens: values.chunk_overlap_tokens,
          retrieval_top_k: values.retrieval_top_k,
          retrieval_score_threshold: values.retrieval_score_threshold ?? null,
          retrieval_method: values.retrieval_method,
          rerank_enabled: values.rerank_enabled,
          ...(thresholdEdited
            ? { rerank_score_threshold: values.rerank_score_threshold ?? null }
            : {}),
        });
        form.setFields([{ name: "rerank_score_threshold", touched: false }]);
        message.success(t("knowledge_page.settings_saved"));
        await onSaved();
      } catch (err) {
        message.error(errMessage(err));
      } finally {
        setSaving(false);
      }
    },
    [base.name, form, t, message, onSaved],
  );

  /** B-206 K-7b —— 确认库上存的下限在当前重排模型下也成立:不送值,只让服务端改绑模型,
   *  不会把别人刚改过的值改回本页打开时的旧值。 */
  const handleConfirmThreshold = useCallback(async () => {
    setConfirming(true);
    try {
      await updateBase(base.name, { rerank_threshold_confirm: true });
      message.success(t("knowledge_page.rerank_threshold_confirmed"));
      await onSaved();
    } catch (err) {
      message.error(errMessage(err));
    } finally {
      setConfirming(false);
    }
  }, [base.name, t, message, onSaved]);

  const handleReindex = useCallback(async () => {
    setReindexing(true);
    try {
      await reindexBase(base.name);
      message.success(t("knowledge_page.reindex_started"));
      await onSaved();
    } catch (err) {
      message.error(errMessage(err));
    } finally {
      setReindexing(false);
    }
  }, [base.name, t, message, onSaved]);

  const embedding = base.embedding_model ?? t("knowledge_page.embedding_unset");

  return (
    <div data-testid="knowledge-settings-tab" style={{ maxWidth: 560 }}>
      <Card size="small" title={t("knowledge_page.settings_title")} style={{ marginBottom: 16 }}>
        <Form<SettingsFormValues>
          form={form}
          layout="vertical"
          initialValues={initialValues}
          onFinish={handleSave}
        >
          <Form.Item name="description" label={t("knowledge_page.field_description")}>
            <Input.TextArea
              rows={2}
              placeholder={t("knowledge_page.field_description_placeholder")}
              aria-label={t("knowledge_page.field_description")}
            />
          </Form.Item>
          <Form.Item
            name="chunk_max_tokens"
            label={t("knowledge_page.field_chunk_max")}
            tooltip={t("knowledge_page.field_chunk_hint")}
          >
            <InputNumber
              min={1}
              style={{ width: "100%" }}
              aria-label={t("knowledge_page.field_chunk_max")}
            />
          </Form.Item>
          <Form.Item
            name="chunk_overlap_tokens"
            label={t("knowledge_page.field_chunk_overlap")}
            tooltip={t("knowledge_page.field_chunk_hint")}
          >
            <InputNumber
              min={0}
              style={{ width: "100%" }}
              aria-label={t("knowledge_page.field_chunk_overlap")}
            />
          </Form.Item>
          <Form.Item
            name="retrieval_top_k"
            label={t("knowledge_page.field_top_k")}
            tooltip={t("knowledge_page.field_top_k_hint")}
          >
            <InputNumber
              min={1}
              max={50}
              style={{ width: "100%" }}
              aria-label={t("knowledge_page.field_top_k")}
            />
          </Form.Item>
          <Form.Item
            name="retrieval_score_threshold"
            label={t("knowledge_page.field_threshold")}
            tooltip={t("knowledge_page.field_threshold_hint")}
          >
            <InputNumber
              min={0}
              max={1}
              step={0.05}
              style={{ width: "100%" }}
              aria-label={t("knowledge_page.field_threshold")}
            />
          </Form.Item>
          <Form.Item
            name="retrieval_method"
            label={t("knowledge_page.field_method")}
            tooltip={t("knowledge_page.field_method_hint")}
          >
            <Select
              aria-label={t("knowledge_page.field_method")}
              data-testid="kb-settings-method"
              options={[
                { value: "hybrid", label: t("knowledge_page.method_hybrid") },
                { value: "vector", label: t("knowledge_page.method_vector") },
                { value: "keyword", label: t("knowledge_page.method_keyword") },
              ]}
            />
          </Form.Item>
          <Form.Item
            name="rerank_enabled"
            label={t("knowledge_page.field_rerank")}
            tooltip={t("knowledge_page.field_rerank_hint")}
            valuePropName="checked"
          >
            <Switch aria-label={t("knowledge_page.field_rerank")} />
          </Form.Item>
          {config?.rerank_threshold_paused && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 8 }}
              message={thresholdPausedNotice(
                t,
                config.rerank_threshold_model,
                config.current_rerank_model,
              )}
              action={
                <ReadonlyTooltip on={readonly}>
                  <Button
                    size="small"
                    loading={confirming}
                    disabled={readonly}
                    onClick={() => void handleConfirmThreshold()}
                    data-testid="kb-settings-rerank-threshold-confirm"
                  >
                    {t("knowledge_page.rerank_threshold_confirm")}
                  </Button>
                </ReadonlyTooltip>
              }
              data-testid="kb-settings-rerank-threshold-paused"
            />
          )}
          <Form.Item
            name="rerank_score_threshold"
            label={t("knowledge_page.field_rerank_threshold")}
            tooltip={t("knowledge_page.field_rerank_threshold_hint")}
          >
            <InputNumber
              min={0}
              max={1}
              step={0.05}
              style={{ width: "100%" }}
              placeholder={t("knowledge_page.field_rerank_threshold_placeholder")}
              aria-label={t("knowledge_page.field_rerank_threshold")}
              data-testid="kb-settings-rerank-threshold"
            />
          </Form.Item>
          <ReadonlyTooltip on={readonly}>
            <Button
              type="primary"
              htmlType="submit"
              loading={saving}
              disabled={readonly}
              data-testid="kb-settings-save"
            >
              {t("common.save")}
            </Button>
          </ReadonlyTooltip>
          <Text type="secondary" style={{ display: "block", marginTop: 8, fontSize: 12 }}>
            {t("knowledge_page.settings_rename_note")}
          </Text>
        </Form>
      </Card>

      <Card size="small" title={t("knowledge_page.settings_embedding_title")}>
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Text>
            {t("knowledge_page.stat_embedding")}: <Text code>{embedding}</Text>
          </Text>
          {base.needs_reindex && (
            <Alert type="warning" showIcon message={t("knowledge_page.needs_reindex_banner")} />
          )}
          <ReadonlyTooltip on={readonly}>
            <Button
              loading={reindexing}
              disabled={base.reindexing || readonly}
              onClick={() => void handleReindex()}
              data-testid="kb-settings-reindex"
            >
              {t("knowledge_page.reindex_button")}
            </Button>
          </ReadonlyTooltip>
        </Space>
      </Card>
    </div>
  );
}
