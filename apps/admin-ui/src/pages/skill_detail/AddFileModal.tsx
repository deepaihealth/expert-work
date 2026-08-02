/**
 * Add supporting file modal — Capability Uplift Sprint #3 PR C.
 *
 * Two ways to seed a new file:
 *
 *   1. Paste text directly into the textarea (default — the common
 *      operator workflow for small reference docs / prompts).
 *   2. Upload a local file via ``<Upload>``. Picking a file overrides
 *      the textarea + mime detection (browser-derived).
 *
 * All path / extension / size limits are enforced server-side; this
 * modal only does a smoke check (path non-empty, no leading ``/``) to
 * give the user fast feedback before the round-trip.
 */
import { useCallback, useState } from "react";
import {
  Alert,
  App,
  Button,
  Form,
  Input,
  Modal,
  Space,
  Tooltip,
  Typography,
  Upload,
} from "antd";
import type { RcFile, UploadFile } from "antd/es/upload/interface";
import { Upload as UploadIcon } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../../api/client";
import { encodeUtf8Base64, type SkillVersion } from "../../api/skills";
import { type SkillApi } from "../../api/skillApi";
import { useIsTenantSwitched } from "../../tenant/useIsTenantSwitched";

const { Text } = Typography;

interface AddFileModalProps {
  api: SkillApi;
  open: boolean;
  skillId: string;
  versionNumber: number;
  onClose: () => void;
  /** Called after the PUT returns the new SkillVersion. The parent
   *  should ``setSelectedVersion`` to the returned version and ``setSelectedPath``
   *  to the path that was just added. */
  onAdded: (newVersion: SkillVersion, path: string) => void;
}

interface FormValues {
  path: string;
  content?: string;
}

async function fileToBase64(file: RcFile): Promise<{
  base64: string;
  size: number;
  mime: string;
}> {
  return await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== "string") {
        reject(new Error("Unexpected FileReader result"));
        return;
      }
      const comma = result.indexOf(",");
      const base64 = comma >= 0 ? result.slice(comma + 1) : result;
      resolve({ base64, size: file.size, mime: file.type || "application/octet-stream" });
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

export function AddFileModal({
  api,
  open,
  skillId,
  versionNumber,
  onClose,
  onAdded,
}: AddFileModalProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  // Cross-tenant W3 — 切入态只读:新增文件是写操作,置灰。
  const isTenantSwitched = useIsTenantSwitched();
  const [form] = Form.useForm<FormValues>();
  const [uploaded, setUploaded] = useState<UploadFile | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleClose = useCallback(() => {
    form.resetFields();
    setUploaded(null);
    setError(null);
    onClose();
  }, [form, onClose]);

  const handleSubmit = useCallback(async () => {
    setError(null);
    let values: FormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    const path = values.path.trim();
    if (path.startsWith("/") || path.includes("\\")) {
      setError("path must be relative (no leading / or backslashes)");
      return;
    }

    setSubmitting(true);
    try {
      let body: { content: string; size: number; mime: string };
      if (uploaded?.originFileObj) {
        const decoded = await fileToBase64(uploaded.originFileObj as RcFile);
        body = { content: decoded.base64, size: decoded.size, mime: decoded.mime };
      } else {
        const text = values.content ?? "";
        const utf8 = new TextEncoder().encode(text);
        body = {
          content: encodeUtf8Base64(text),
          size: utf8.byteLength,
          mime: path.endsWith(".md") ? "text/markdown" : "text/plain",
        };
      }
      const newVersion = await api.putSupportingFile(skillId, versionNumber, path, body);
      message.success(t("skills.file_saved", { version: newVersion.version }));
      onAdded(newVersion, path);
      handleClose();
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `${err.code}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "unknown error";
      setError(msg);
    } finally {
      setSubmitting(false);
    }
  }, [api, form, handleClose, message, onAdded, skillId, t, uploaded, versionNumber]);

  return (
    <Modal
      open={open}
      title={t("skills.file_add_modal_title")}
      onCancel={handleClose}
      destroyOnHidden
      footer={[
        <Button key="cancel" onClick={handleClose} disabled={submitting}>
          {t("skills.file_action_cancel")}
        </Button>,
        <Tooltip
          key="submit"
          title={isTenantSwitched ? t("common.tenant_switched_readonly") : undefined}
        >
          <Button
            type="primary"
            onClick={handleSubmit}
            loading={submitting}
            disabled={isTenantSwitched}
            data-testid="skill-add-file-submit"
          >
            {t("skills.file_add_submit")}
          </Button>
        </Tooltip>,
      ]}
    >
      {error !== null && (
        <Alert
          type="error"
          showIcon
          message={t("skills.file_save_failed")}
          description={error}
          style={{ marginBottom: 12 }}
          data-testid="skill-add-file-error"
        />
      )}
      <Form<FormValues> form={form} layout="vertical" requiredMark>
        <Form.Item
          name="path"
          label={t("skills.file_add_path_label")}
          rules={[{ required: true, message: t("skills.file_add_path_label") }]}
        >
          <Input
            placeholder={t("skills.file_add_path_placeholder")}
            data-testid="skill-add-file-path"
          />
        </Form.Item>

        {uploaded === null ? (
          <Form.Item name="content" label={t("skills.file_add_content_label")}>
            <Input.TextArea
              rows={6}
              placeholder="..."
              data-testid="skill-add-file-content"
              style={{ fontFamily: "var(--ew-font-mono)", fontSize: 12 }}
            />
          </Form.Item>
        ) : null}

        <Space direction="vertical" size={4} style={{ width: "100%" }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t("skills.file_add_upload_hint")}
          </Text>
          <Upload
            maxCount={1}
            beforeUpload={() => false}
            fileList={uploaded === null ? [] : [uploaded]}
            onChange={({ fileList }) => {
              setUploaded(fileList[0] ?? null);
            }}
            data-testid="skill-add-file-upload"
          >
            <Button icon={<UploadIcon size={13} strokeWidth={1.75} />}>
              {t("skills.file_add_upload_label")}
            </Button>
          </Upload>
        </Space>
      </Form>
    </Modal>
  );
}
