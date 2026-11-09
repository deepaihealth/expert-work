/**
 * B-278 — banner on the detail page of a deleted agent version.
 *
 * The page is read-only while it shows. Two ways out, both tenant-admin only
 * (backend permission ``manifest:delete``; gated in the UI like the other
 * admin-only controls — ``isSystemAdmin || roles.includes("admin")`` — with
 * the server as the authority):
 *
 * - 「恢复」: confirm → ``POST .../restore`` → the parent reloads. Triggers the
 *   delete switched off stay off; when any do, a dialog says how many and
 *   points to the Triggers page.
 * - 「彻底删除」: :class:`PurgeAgentModal`; a clean purge returns to the list.
 *
 * ``purging`` (a purge that didn't finish) gets its own wording — "click
 * 彻底删除 again to finish" — and no 「恢复」: the server refuses to restore a
 * half-purged name (409 ``AGENT_PURGING``).
 *
 * Both endpoints act on the caller's home tenant (no ``?tenant_id=``), so a
 * system admin who switched into another tenant gets them greyed out.
 */
import { useCallback, useState } from "react";
import { Alert, App, Button, Space } from "antd";
import { RotateCcw, Trash2 } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { errMessage } from "../../api/client";
import { restoreAgent } from "../../api/agents";
import { useAuth } from "../../auth/AuthContext";
import { PurgeAgentModal } from "../../components/PurgeAgentModal";
import { ReadonlyTooltip } from "../../components/ReadonlyTooltip";
import { useIsTenantSwitched } from "../../tenant/useIsTenantSwitched";

interface DeletedAgentBannerProps {
  name: string;
  version: string;
  /** ``record.status === "purging"``: the last purge didn't finish. */
  purging?: boolean;
  /** Called after a restore — the parent refetches the detail. */
  onRestored: () => void;
}

export function DeletedAgentBanner({
  name,
  version,
  purging = false,
  onRestored,
}: DeletedAgentBannerProps) {
  const { t } = useTranslation();
  const { message, modal } = App.useApp();
  const navigate = useNavigate();
  const { identity } = useAuth();
  const isTenantSwitched = useIsTenantSwitched();
  const [purgeOpen, setPurgeOpen] = useState(false);

  const canManage =
    (identity?.isSystemAdmin ?? false) || (identity?.roles.includes("admin") ?? false);

  const confirmRestore = useCallback(() => {
    modal.confirm({
      title: t("agent_lifecycle.restore_confirm_title", { name, version }),
      content: t("agent_lifecycle.restore_confirm_body"),
      okText: t("agent_lifecycle.restore"),
      cancelText: t("common.cancel"),
      onOk: async () => {
        try {
          const result = await restoreAgent(name, version);
          message.success(t("agent_lifecycle.restore_ok", { name, version }));
          if (result.triggers_disabled > 0) {
            modal.confirm({
              title: t("agent_lifecycle.restore_triggers_title"),
              content: t("agent_lifecycle.restore_triggers_body", {
                count: result.triggers_disabled,
              }),
              okText: t("agent_lifecycle.restore_triggers_cta"),
              cancelText: t("agent_lifecycle.restore_triggers_dismiss"),
              onOk: () => navigate("/triggers"),
            });
          }
          onRestored();
        } catch (err) {
          message.error(t("agent_lifecycle.restore_failed", { error: errMessage(err) }));
        }
      },
    });
  }, [modal, message, t, name, version, navigate, onRestored]);

  const bannerDescription = purging
    ? canManage
      ? t("agent_lifecycle.purging_banner_desc")
      : t("agent_lifecycle.purging_banner_desc_no_permission")
    : canManage
      ? t("agent_lifecycle.deleted_banner_desc")
      : t("agent_lifecycle.deleted_banner_desc_no_permission");

  return (
    <>
      <Alert
        type="error"
        showIcon
        style={{ marginBottom: 16 }}
        message={
          purging
            ? t("agent_lifecycle.purging_banner_title")
            : t("agent_lifecycle.deleted_banner_title")
        }
        description={bannerDescription}
        action={
          canManage ? (
            <Space direction="vertical" size={8}>
              {!purging && (
                <ReadonlyTooltip on={isTenantSwitched}>
                  <Button
                    size="small"
                    block
                    icon={<RotateCcw size={14} strokeWidth={1.75} />}
                    disabled={isTenantSwitched}
                    onClick={confirmRestore}
                    data-testid="agent-restore-btn"
                  >
                    {t("agent_lifecycle.restore")}
                  </Button>
                </ReadonlyTooltip>
              )}
              <ReadonlyTooltip on={isTenantSwitched}>
                <Button
                  size="small"
                  block
                  danger
                  icon={<Trash2 size={14} strokeWidth={1.75} />}
                  disabled={isTenantSwitched}
                  onClick={() => setPurgeOpen(true)}
                  data-testid="agent-purge-btn"
                >
                  {t("agent_lifecycle.purge")}
                </Button>
              </ReadonlyTooltip>
            </Space>
          ) : undefined
        }
        data-testid="agent-deleted-banner"
      />
      <PurgeAgentModal
        open={purgeOpen}
        name={name}
        version={version}
        onClose={() => setPurgeOpen(false)}
        onPurged={() => {
          setPurgeOpen(false);
          navigate("/agents");
        }}
      />
    </>
  );
}
