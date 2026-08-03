/**
 * Rename + Delete confirmation modals — Capability Uplift Sprint #3 PR C.
 *
 * Both flows produce a new SkillVersion (D3 immutability). Delete asks
 * for the file path to be typed back as confirmation — same pattern as
 * the rest of the destructive surfaces in Admin UI.
 */
import { useCallback, useState } from "react";
import { Alert, App, Button, Form, Input, Modal, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { ApiError } from "../../api/client";
import { type SkillVersion } from "../../api/skills";
import { type SkillApi } from "../../api/skillApi";
import { ReadonlyTooltip } from "../../components/ReadonlyTooltip";

const { Text } = Typography;

// ─── Rename ──────────────────────────────────────────────────────────

interface RenameModalProps {
  api: SkillApi;
  open: boolean;
  skillId: string;
  versionNumber: number;
  oldPath: string;
  /** Cross-tenant W4(D2)— 权威读口径:URL ``?tenant_id=`` 原样透传优先;
   *  无 URL 参数时取 ambient scope("*" 折叠成 undefined),由 SkillDetail
   *  统一下传。 */
  readScope: string | undefined;
  /** Cross-tenant W4(D2)— 只读态(切入态 ∪ "*" 聚合深链外租户读),由
   *  SkillDetail 统一判定下传;重命名是写操作,置灰。 */
  readonly: boolean;
  onClose: () => void;
  onRenamed: (newVersion: SkillVersion, newPath: string) => void;
}

export function RenameModal({
  api,
  open,
  skillId,
  versionNumber,
  oldPath,
  readScope,
  readonly,
  onClose,
  onRenamed,
}: RenameModalProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  // Cross-tenant W4(D2)— the pre-rename read takes the page's readScope
  // (writes are not scope-aware).
  const [form] = Form.useForm<{ newPath: string }>();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleClose = useCallback(() => {
    form.resetFields();
    setError(null);
    onClose();
  }, [form, onClose]);

  const handleSubmit = useCallback(async () => {
    setError(null);
    let values: { newPath: string };
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    const newPath = values.newPath.trim();
    if (newPath === oldPath) {
      setError("new path must differ from current path");
      return;
    }

    setSubmitting(true);
    try {
      const original = await api.getSupportingFile(
        skillId,
        versionNumber,
        oldPath,
        readScope,
      );
      const newVersion = await api.renameSupportingFile(
        skillId,
        versionNumber,
        oldPath,
        newPath,
        original,
      );
      message.success(t("skills.file_renamed", { version: newVersion.version }));
      onRenamed(newVersion, newPath);
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
  }, [
    api,
    form,
    handleClose,
    message,
    oldPath,
    onRenamed,
    skillId,
    t,
    versionNumber,
    readScope,
  ]);

  return (
    <Modal
      open={open}
      title={t("skills.file_rename_modal_title", { path: oldPath })}
      onCancel={handleClose}
      destroyOnHidden
      footer={[
        <Button key="cancel" onClick={handleClose} disabled={submitting}>
          {t("skills.file_action_cancel")}
        </Button>,
        <ReadonlyTooltip
          key="submit" on={readonly}>
          <Button
            type="primary"
            onClick={handleSubmit}
            loading={submitting}
            disabled={readonly}
            data-testid="skill-rename-submit"
          >
            {t("skills.file_rename_submit")}
          </Button>
        </ReadonlyTooltip>,
      ]}
    >
      {error !== null && (
        <Alert
          type="error"
          showIcon
          message={t("skills.file_save_failed")}
          description={error}
          style={{ marginBottom: 12 }}
          data-testid="skill-rename-error"
        />
      )}
      <Form
        form={form}
        layout="vertical"
        initialValues={{ newPath: oldPath }}
      >
        <Form.Item
          name="newPath"
          label={t("skills.file_rename_new_path_label")}
          rules={[{ required: true }]}
        >
          <Input data-testid="skill-rename-new-path" />
        </Form.Item>
      </Form>
    </Modal>
  );
}

// ─── Delete ──────────────────────────────────────────────────────────

interface DeleteConfirmModalProps {
  api: SkillApi;
  open: boolean;
  skillId: string;
  versionNumber: number;
  path: string;
  /** Cross-tenant W4(D2)— 只读态(切入态 ∪ "*" 聚合深链外租户读),由
   *  SkillDetail 统一判定下传;删除是写操作,置灰。 */
  readonly: boolean;
  onClose: () => void;
  onDeleted: (newVersion: SkillVersion) => void;
}

export function DeleteConfirmModal({
  api,
  open,
  skillId,
  versionNumber,
  path,
  readonly,
  onClose,
  onDeleted,
}: DeleteConfirmModalProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [typed, setTyped] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleClose = useCallback(() => {
    setTyped("");
    setError(null);
    onClose();
  }, [onClose]);

  const canDelete = typed === path;

  const handleSubmit = useCallback(async () => {
    if (!canDelete) return;
    setSubmitting(true);
    setError(null);
    try {
      const newVersion = await api.deleteSupportingFile(skillId, versionNumber, path);
      message.success(t("skills.file_deleted", { version: newVersion.version }));
      onDeleted(newVersion);
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
  }, [
    api,
    canDelete,
    handleClose,
    message,
    onDeleted,
    path,
    skillId,
    t,
    versionNumber,
  ]);

  return (
    <Modal
      open={open}
      title={t("skills.file_delete_confirm_title", { path })}
      onCancel={handleClose}
      destroyOnHidden
      footer={[
        <Button key="cancel" onClick={handleClose} disabled={submitting}>
          {t("skills.file_action_cancel")}
        </Button>,
        <ReadonlyTooltip
          key="submit" on={readonly}>
          <Button
            danger
            type="primary"
            onClick={handleSubmit}
            loading={submitting}
            disabled={!canDelete || readonly}
            data-testid="skill-delete-submit"
          >
            {t("skills.file_action_delete")}
          </Button>
        </ReadonlyTooltip>,
      ]}
    >
      {error !== null && (
        <Alert
          type="error"
          showIcon
          message={t("skills.file_save_failed")}
          description={error}
          style={{ marginBottom: 12 }}
          data-testid="skill-delete-error"
        />
      )}
      <Text style={{ fontSize: 13, display: "block", marginBottom: 12 }}>
        {t("skills.file_delete_confirm_body")}
      </Text>
      <Text type="secondary" style={{ fontSize: 12, display: "block", marginBottom: 6 }}>
        {t("skills.file_delete_confirm_input_hint")}
      </Text>
      <Input
        value={typed}
        onChange={(e) => setTyped(e.target.value)}
        placeholder={path}
        data-testid="skill-delete-confirm-input"
        style={{ fontFamily: "var(--ew-font-mono)", fontSize: 12 }}
      />
    </Modal>
  );
}
