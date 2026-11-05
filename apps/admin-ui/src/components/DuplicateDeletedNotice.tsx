/**
 * B-278 — what create / template-fork show when the name+version belongs to a
 * deleted agent (``409 MANIFEST_DUPLICATE_DELETED``: it can be restored or
 * purged) or to one whose purge hasn't finished (``409 AGENT_PURGING``: try
 * again later / finish the purge). Instead of a dead-end "already exists",
 * say so and link to its detail page, where the buttons live.
 */
import { Alert, Button } from "antd";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";

import type { DuplicateDeletedTarget } from "../api/agents";

interface DuplicateDeletedNoticeProps {
  target: DuplicateDeletedTarget;
  /** Runs before navigating — e.g. close the hosting modal. */
  onNavigate?: () => void;
}

export function DuplicateDeletedNotice({ target, onNavigate }: DuplicateDeletedNoticeProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { name, version } = target;
  // The link needs both; the server sends them with the 409 (if it ever
  // doesn't, the message still stands on its own).
  const link =
    name !== null && version !== null ? (
      <Button
        type="link"
        size="small"
        style={{ padding: 0 }}
        onClick={() => {
          onNavigate?.();
          navigate(`/agents/${encodeURIComponent(name)}/${encodeURIComponent(version)}/overview`);
        }}
        data-testid="duplicate-deleted-link"
      >
        {t("agent_lifecycle.duplicate_deleted_link", { name, version })}
      </Button>
    ) : undefined;
  return (
    <Alert
      type="warning"
      showIcon
      message={
        target.kind === "purging"
          ? t("agent_lifecycle.duplicate_purging")
          : t("agent_lifecycle.duplicate_deleted")
      }
      description={link}
      style={{ marginBottom: 12 }}
      data-testid="duplicate-deleted-notice"
    />
  );
}
