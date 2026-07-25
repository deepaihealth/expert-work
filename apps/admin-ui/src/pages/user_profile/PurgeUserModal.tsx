/**
 * PurgeUserModal — the high-risk, type-to-confirm gate for
 * ``POST /v1/users/{id}:purge`` (Phase 3a).
 *
 * Irreversibly cascade-purges a user's data + assets — external end-user,
 * employee (console member), or the caller themself; purging is decoupled
 * from account deletion, which stays a members-page-only concern (this
 * endpoint never touches Keycloak / roles / ``tenant_member``). To arm the
 * danger button the admin must type the user's ``subject_id`` verbatim; a
 * self-purge (``isSelf``) additionally shows a reinforced warning. The 409
 * handler below is now a defensive fallback — the backend no longer rejects
 * employees — kept in case some other conflict ever surfaces here.
 *
 * Deletion-hygiene PR5 generalizes the type-to-confirm interaction so the
 * members page can reuse it for the one-shot deactivate + purge: all new
 * props (``confirmTarget`` / ``onSubmit`` / ``copy``) are optional and
 * default to the original /users profile behavior.
 */
import { useState, type ReactNode } from "react";
import { Alert, App, Input, Modal, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { ApiError } from "../../api/client";
import { isMemberPurgePartial, type MemberPurgeResult } from "../../api/members";
import { purgeUser, type PurgeSummary } from "../../api/users";
import { errMessage } from "./useLoad";

const { Paragraph, Text } = Typography;

/** Copy overrides for reusing the modal outside the /users profile.
 *  Every field falls back to the original ``user_profile.purge_*`` copy. */
export interface PurgeModalCopy {
  /** Modal title. */
  title?: string;
  /** Danger button label. */
  okText?: string;
  /** Replaces the default warning ``Alert`` block entirely. */
  body?: ReactNode;
  /** The type-to-confirm prompt (the target is rendered next to it). */
  typeToConfirm?: string;
  /** Success toast. */
  done?: string;
  /** Partial-failure toast (modal stays open for a retry). */
  partial?: string;
}

interface PurgeUserModalProps {
  open: boolean;
  onClose: () => void;
  userId: string;
  /** The value the admin must type to arm the delete (the passed-in user id). */
  subjectId: string;
  displayName?: string;
  /** ``true`` when the target is the caller's own data — shows a reinforced warning. */
  isSelf?: boolean;
  /** Called after a successful purge — the caller navigates away. */
  onPurged: () => void;
  /** Overrides the value the admin must type (default: ``subjectId``). */
  confirmTarget?: string;
  /** Overrides the default ``purgeUser`` call (the members one-shot purge). */
  onSubmit?: () => Promise<PurgeSummary | MemberPurgeResult>;
  copy?: PurgeModalCopy;
}

export function PurgeUserModal({
  open,
  onClose,
  userId,
  subjectId,
  displayName,
  isSelf = false,
  onPurged,
  confirmTarget,
  onSubmit,
  copy,
}: PurgeUserModalProps) {
  const { t } = useTranslation();
  const { message, modal } = App.useApp();
  const [confirmText, setConfirmText] = useState("");
  const [busy, setBusy] = useState(false);

  // Armed only on an exact, non-empty match — never arm on an empty target.
  const target = confirmTarget ?? subjectId;
  const armed = target.length > 0 && confirmText.trim() === target;

  const close = () => {
    if (busy) return;
    setConfirmText("");
    onClose();
  };

  const onConfirm = async () => {
    if (!armed || busy) return;
    setBusy(true);
    try {
      const result = await (onSubmit ? onSubmit() : purgeUser(userId));
      // Best-effort: the endpoint returns 200 even when some steps failed.
      // ``ok`` only exists on PurgeSummary — a MemberPurgeResult carries
      // per-step failure booleans instead (folded by isMemberPurgePartial).
      const ok = "ok" in result ? result.ok : !isMemberPurgePartial(result);
      if (!ok) {
        // Partial purge — stay on the page so the "retry" hint is actionable
        // (re-purge is idempotent). Keep the input armed for a one-click retry.
        message.warning(copy?.partial ?? t("user_profile.purge_partial"));
        setBusy(false);
        return;
      }
      message.success(copy?.done ?? t("user_profile.purge_done"));
      setConfirmText("");
      onPurged();
      // Intentionally leave `busy` set: the parent unmounts this modal on
      // navigation, and resetting it here would briefly re-arm the button
      // (a fast second Enter could otherwise re-fire the purge).
    } catch (err) {
      if (onSubmit === undefined && err instanceof ApiError && err.status === 409) {
        // Defensive fallback (default purgeUser path only — the "go to the
        // members page" hint would mislead on the members page itself) — the
        // backend no longer 409s for employees, but keep the hint in case
        // some other conflict ever surfaces here.
        modal.warning({
          title: t("user_profile.purge_employee_title"),
          content: t("user_profile.purge_employee_body"),
        });
      } else {
        modal.error({
          title: t("user_profile.purge_failed_title"),
          content: errMessage(err),
        });
      }
      setBusy(false); // a failed purge is retryable — re-enable the button
    }
  };

  const who = displayName ? `${subjectId} (${displayName})` : subjectId;

  return (
    <Modal
      open={open}
      onCancel={close}
      onOk={onConfirm}
      okText={copy?.okText ?? t("user_profile.purge_confirm_btn")}
      cancelText={t("common.cancel")}
      okButtonProps={{ danger: true, disabled: !armed, loading: busy, "data-testid": "purge-confirm-ok" }}
      title={copy?.title ?? t("user_profile.purge_title")}
      destroyOnClose
      data-testid="purge-user-modal"
    >
      {isSelf && (
        <Alert
          type="error"
          showIcon
          message={t("user_profile.purge_self_warning")}
          style={{ marginBottom: 16 }}
          data-testid="purge-self-warning"
        />
      )}
      {copy?.body ?? (
        <Alert
          type="error"
          showIcon
          message={t("user_profile.purge_warning", { who })}
          description={
            <>
              <Paragraph style={{ marginBottom: 4 }}>{t("user_profile.purge_deletes")}</Paragraph>
              <Paragraph style={{ marginBottom: 4 }} type="secondary">
                {t("user_profile.purge_anonymizes")}
              </Paragraph>
              <Paragraph style={{ marginBottom: 0 }} type="secondary">
                {t("user_profile.purge_archive_note")}
              </Paragraph>
            </>
          }
          style={{ marginBottom: 16 }}
        />
      )}
      <Paragraph style={{ marginBottom: 6 }}>
        {copy?.typeToConfirm ?? t("user_profile.purge_type_to_confirm")}{" "}
        <Text code>{target}</Text>
      </Paragraph>
      <Input
        value={confirmText}
        onChange={(e) => setConfirmText(e.target.value)}
        onPressEnter={onConfirm}
        placeholder={target}
        disabled={busy}
        data-testid="purge-confirm-input"
      />
    </Modal>
  );
}
