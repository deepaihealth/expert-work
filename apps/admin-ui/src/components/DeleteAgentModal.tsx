/**
 * DeleteAgentModal — the high-risk, type-to-confirm gate for
 * ``DELETE /v1/agents/{name}/{version}`` (soft delete: flips this exact
 * version's status to ``DELETED``; other versions of the agent are
 * untouched). B-278: the detail page of a deleted version can restore it or
 * purge the whole name.
 *
 * Mirrors ``user_profile/PurgeUserModal``'s type-to-confirm pattern: the
 * admin must type the agent's ``name`` verbatim to arm the danger button.
 */
import { useState } from "react";
import { Alert, App, Input, Modal, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { deleteAgent } from "../api/agents";

const { Paragraph, Text } = Typography;

function errMessage(err: unknown): string {
  return err instanceof ApiError
    ? `${err.code}: ${err.message}`
    : err instanceof Error
      ? err.message
      : "unknown error";
}

interface DeleteAgentModalProps {
  open: boolean;
  onClose: () => void;
  name: string;
  version: string;
  /** Called after a successful delete — the caller closes the modal and
   *  refreshes the list. */
  onDeleted: () => void;
}

export function DeleteAgentModal({
  open,
  onClose,
  name,
  version,
  onDeleted,
}: DeleteAgentModalProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [confirmText, setConfirmText] = useState("");
  const [busy, setBusy] = useState(false);

  // Armed only on an exact, non-empty match — never arm on an empty name.
  const armed = name.length > 0 && confirmText.trim() === name;

  const close = () => {
    if (busy) return;
    setConfirmText("");
    onClose();
  };

  const onConfirm = async () => {
    if (!armed || busy) return;
    setBusy(true);
    try {
      await deleteAgent(name, version);
      message.success(t("agents_page.delete_success", { name, version }));
      setConfirmText("");
      onDeleted();
      // Intentionally leave `busy` set: the parent closes (unmounts, via
      // destroyOnHidden) this modal right after onDeleted() — resetting it
      // here would briefly re-arm the button before that happens.
    } catch (err) {
      message.error(t("agents_page.delete_failed", { error: errMessage(err) }));
      setBusy(false); // a failed delete is retryable — re-enable the button
    }
  };

  return (
    <Modal
      open={open}
      onCancel={close}
      onOk={onConfirm}
      okText={t("agents_page.action_delete")}
      cancelText={t("common.cancel")}
      okButtonProps={{
        danger: true,
        disabled: !armed,
        loading: busy,
        "data-testid": "delete-agent-confirm-ok",
      }}
      title={t("agents_page.delete_title")}
      destroyOnHidden
      data-testid="delete-agent-modal"
    >
      <Alert
        type="error"
        showIcon
        message={t("agents_page.delete_warning", { name, version })}
        description={t("agents_page.delete_warning_desc")}
        style={{ marginBottom: 16 }}
      />
      <Paragraph style={{ marginBottom: 6 }}>
        {t("agents_page.delete_type_to_confirm")} <Text code>{name}</Text>
      </Paragraph>
      <Input
        value={confirmText}
        onChange={(e) => setConfirmText(e.target.value)}
        onPressEnter={onConfirm}
        placeholder={name}
        disabled={busy}
        data-testid="delete-agent-confirm-input"
      />
    </Modal>
  );
}
