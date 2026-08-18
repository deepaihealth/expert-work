/**
 * SessionSidebarItem — one row in ``SessionSidebar``'s list: title + relative
 * time, the current-thread running dot, and the hover-reveal rename /
 * archive / purge actions. Split out of ``SessionSidebar.tsx`` to keep that
 * file under the 400-line budget (debug console redesign PR-A Task 6).
 */
import { Button, List, Popconfirm } from "antd";
import { Pencil, Trash2, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { ThreadMeta } from "../../api/sessions";
import { ReadonlyTooltip } from "../ReadonlyTooltip";
import { relativeTime } from "./relative_time";

export interface SessionSidebarItemProps {
  session: ThreadMeta;
  isCurrent: boolean;
  /** A run is in flight — disables clicking any row (nothing to switch to
   *  mid-run) and, when it's the current thread, shows the running dot. */
  running: boolean;
  readOnly: boolean;
  busy: boolean;
  onResume: (session: ThreadMeta) => void;
  onRenameStart: (session: ThreadMeta) => void;
  onArchive: (threadId: string) => void;
  onPurge: (threadId: string) => void;
}

export function SessionSidebarItem({
  session,
  isCurrent,
  running,
  readOnly,
  busy,
  onResume,
  onRenameStart,
  onArchive,
  onPurge,
}: SessionSidebarItemProps) {
  const { t } = useTranslation();
  const title = session.title?.trim() || `${session.thread_id.slice(0, 8)}…`;
  const showDot = isCurrent && running;

  return (
    <List.Item
      className="ew-session-item"
      data-testid={`console-session-item-${session.thread_id}`}
      title={running ? t("playground.running") : undefined}
      style={{
        padding: "8px 8px",
        borderRadius: 6,
        cursor: running ? "not-allowed" : "pointer",
        opacity: running && !isCurrent ? 0.6 : 1,
        background: isCurrent ? "var(--ew-surface-selected)" : undefined,
      }}
      onClick={() => onResume(session)}
    >
      <div className="ew-session-item__body">
        <div className="ew-session-item__title" title={title}>
          {showDot && (
            <span
              className="ew-session-running-dot"
              data-testid="console-session-running-dot"
              aria-label={t("console.sidebar_running_dot")}
            />
          )}
          {title}
        </div>
        <div className="ew-session-item__meta">
          {relativeTime(session.updated_at, t)}
        </div>
      </div>
      <div className="ew-session-item__acts" onClick={(e) => e.stopPropagation()}>
        <ReadonlyTooltip on={readOnly}>
          <Button
            type="text"
            size="small"
            icon={<Pencil size={13} strokeWidth={1.75} />}
            aria-label={`${t("session_history.rename")}：${title}`}
            loading={busy}
            disabled={readOnly}
            onClick={() => onRenameStart(session)}
            data-testid={`console-session-rename-${session.thread_id}`}
          />
        </ReadonlyTooltip>
        <ReadonlyTooltip on={readOnly}>
          <Popconfirm
            title={t("session_history.archive_confirm")}
            okText={t("session_history.archive")}
            cancelText={t("session_history.cancel")}
            onConfirm={() => onArchive(session.thread_id)}
            disabled={readOnly}
          >
            <Button
              type="text"
              size="small"
              icon={<X size={13} strokeWidth={1.75} />}
              aria-label={`${t("session_history.archive")}：${title}`}
              disabled={readOnly}
              data-testid={`console-session-archive-${session.thread_id}`}
            />
          </Popconfirm>
        </ReadonlyTooltip>
        <ReadonlyTooltip on={readOnly}>
          <Popconfirm
            title={t("session_history.purge_confirm")}
            description={t("session_history.purge_warning")}
            okText={t("session_history.purge")}
            okButtonProps={{ danger: true }}
            cancelText={t("session_history.cancel")}
            onConfirm={() => onPurge(session.thread_id)}
            disabled={readOnly}
          >
            <Button
              type="text"
              size="small"
              danger
              icon={<Trash2 size={13} strokeWidth={1.75} />}
              aria-label={`${t("session_history.purge")}：${title}`}
              disabled={readOnly}
              data-testid={`console-session-purge-${session.thread_id}`}
            />
          </Popconfirm>
        </ReadonlyTooltip>
      </div>
    </List.Item>
  );
}
