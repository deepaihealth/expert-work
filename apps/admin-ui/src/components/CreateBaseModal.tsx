/**
 * Create-knowledge-base modal — KB commercial uplift.
 *
 * A flat form (few fields, no Advanced collapse): name + description, then the
 * chunking + retrieval defaults pre-filled with tuned values so a user can
 * just name a base and save. A Modal (not a Drawer) to match the platform's
 * create-modal convention.
 */
import { useCallback, useState } from "react";
import { App, Form, Input, InputNumber, Modal, Select, Switch } from "antd";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { createBase, type KnowledgeBase, type RetrievalMethod } from "../api/knowledge";

interface CreateFormValues {
  name: string;
  description?: string;
  chunkMaxTokens?: number;
  chunkOverlapTokens?: number;
  retrievalTopK?: number;
  retrievalScoreThreshold?: number | null;
  retrievalMethod?: RetrievalMethod;
  rerankEnabled?: boolean;
}

interface CreateBaseModalProps {
  open: boolean;
  onClose: () => void;
  onCreated: (base: KnowledgeBase) => void;
}

function errMessage(err: unknown): string {
  return err instanceof ApiError
    ? `${err.code}: ${err.message}`
    : err instanceof Error
      ? err.message
      : "unknown error";
}

export function CreateBaseModal({ open, onClose, onCreated }: CreateBaseModalProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [form] = Form.useForm<CreateFormValues>();
  const [creating, setCreating] = useState(false);

  const handleFinish = useCallback(
    async (values: CreateFormValues) => {
      setCreating(true);
      try {
        const created = await createBase(values);
        form.resetFields();
        onCreated(created);
      } catch (err) {
        const detail =
          err instanceof ApiError && err.status === 409
            ? t("knowledge_page.create_duplicate")
            : errMessage(err);
        message.error(detail);
      } finally {
        setCreating(false);
      }
    },
    [t, form, message, onCreated],
  );

  return (
    <Modal
      open={open}
      onCancel={onClose}
      onOk={() => form.submit()}
      confirmLoading={creating}
      title={t("knowledge_page.create_base")}
      okText={t("common.save")}
      destroyOnHidden
    >
      {/* testid on the content wrapper: antd forwards data-testid to the
          always-rendered (hidden) modal root, which Playwright treats as
          hidden ([memory:Monaco/Modal testid wrapper]). */}
      <div data-testid="kb-create-modal">
        <Form<CreateFormValues>
          form={form}
          layout="vertical"
          onFinish={handleFinish}
          // Pre-filled with the platform defaults (chunk 400 / no overlap — the
          // backend's DEFAULT_CHUNK_MAX_TOKENS / _OVERLAP_TOKENS, B-206 K-3 —
          // top-k 5, hybrid + rerank); the form posts them as they are, so they
          // must match. Threshold stays empty on purpose — "no cutoff" is the
          // safe default; a value risks dropping relevant hits.
          initialValues={{
            chunkMaxTokens: 400,
            chunkOverlapTokens: 0,
            retrievalTopK: 5,
            retrievalMethod: "hybrid",
            rerankEnabled: true,
          }}
        >
          <Form.Item
            name="name"
            label={t("knowledge_page.field_name")}
            rules={[{ required: true, min: 1, max: 128 }]}
          >
            <Input data-testid="kb-create-name" aria-label={t("knowledge_page.field_name")} />
          </Form.Item>
          <Form.Item name="description" label={t("knowledge_page.field_description")}>
            <Input.TextArea
              rows={2}
              placeholder={t("knowledge_page.field_description_placeholder")}
              aria-label={t("knowledge_page.field_description")}
            />
          </Form.Item>
          <Form.Item
            name="chunkMaxTokens"
            label={t("knowledge_page.field_chunk_max")}
            tooltip={t("knowledge_page.field_chunk_hint")}
          >
            <InputNumber
              min={128}
              max={1024}
              style={{ width: "100%" }}
              aria-label={t("knowledge_page.field_chunk_max")}
            />
          </Form.Item>
          <Form.Item
            name="chunkOverlapTokens"
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
            name="retrievalTopK"
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
            name="retrievalScoreThreshold"
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
            name="retrievalMethod"
            label={t("knowledge_page.field_method")}
            tooltip={t("knowledge_page.field_method_hint")}
          >
            <Select
              aria-label={t("knowledge_page.field_method")}
              data-testid="kb-create-method"
              options={[
                { value: "hybrid", label: t("knowledge_page.method_hybrid") },
                { value: "vector", label: t("knowledge_page.method_vector") },
                { value: "keyword", label: t("knowledge_page.method_keyword") },
              ]}
            />
          </Form.Item>
          <Form.Item
            name="rerankEnabled"
            label={t("knowledge_page.field_rerank")}
            tooltip={t("knowledge_page.field_rerank_hint")}
            valuePropName="checked"
          >
            <Switch aria-label={t("knowledge_page.field_rerank")} />
          </Form.Item>
        </Form>
      </div>
    </Modal>
  );
}
