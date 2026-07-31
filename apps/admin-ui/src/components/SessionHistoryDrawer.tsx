/**
 * SessionHistoryDrawer — the Playground's "会话历史" surface.
 *
 * Replaces the old single ``<Select>`` resume dropdown with a browsable,
 * searchable list of the caller's threads for the current agent. Each row
 * shows a human title (auto from the first message, or renamed), the last
 * activity, a status badge and the run-as owner. Row actions: resume (click
 * the row), rename, archive (soft delete), and purge (hard delete, 2nd
 * confirm). Pagination is server-side (offset) so nothing is silently capped.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  App,
  Button,
  Drawer,
  Empty,
  Input,
  List,
  Modal,
  Popconfirm,
  Select,
  Space,
  Tag,
} from "antd";
import { MoreHorizontal, Pencil, Trash2, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  archiveSession,
  listSessions,
  purgeSession,
  renameSession,
  type ThreadMeta,
} from "../api/sessions";
import { useTenantScope } from "../tenant/TenantScopeContext";

const PAGE_SIZE = 50;

const STATUS_COLOR: Record<string, string> = {
  active: "green",
  paused: "gold",
  completed: "default",
  failed: "red",
  cancelled: "default",
  archived: "default",
};

/** Compact relative time ("3分钟前") from an ISO timestamp, localized via i18n
 *  keys. Falls back to the raw locale string for anything older than a week. */
function relativeTime(
  iso: string,
  t: (key: string, opts?: Record<string, unknown>) => string,
): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const secs = Math.floor((Date.now() - then) / 1000);
  if (secs < 60) return t("session_history.time_now");
  const mins = Math.floor(secs / 60);
  if (mins < 60) return t("session_history.time_minutes", { n: mins });
  const hours = Math.floor(mins / 60);
  if (hours < 24) return t("session_history.time_hours", { n: hours });
  const days = Math.floor(hours / 24);
  if (days < 7) return t("session_history.time_days", { n: days });
  return new Date(iso).toLocaleDateString();
}

export interface SessionHistoryDrawerProps {
  open: boolean;
  onClose: () => void;
  /** Only this agent's threads are listed (a thread is bound to one agent). */
  agentName: string;
  /** The currently open thread, highlighted in the list. */
  currentThreadId: string | null;
  /** Resume the picked thread (the drawer closes after). */
  onResume: (session: ThreadMeta) => void;
  /** Called after a rename / archive / purge so the parent can refresh. */
  onChanged?: () => void;
}

export function SessionHistoryDrawer({
  open,
  onClose,
  agentName,
  currentThreadId,
  onResume,
  onChanged,
}: SessionHistoryDrawerProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  // Stream N — the sessions list is scope-aware ("*" aggregates every
  // tenant), so the raw scope rides along for a switched-in system_admin.
  const { apiTenantScope } = useTenantScope();

  const [sessions, setSessions] = useState<ThreadMeta[]>([]);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  // "" = all (excludes archived); a status value narrows to it (incl.
  // "archived", which is how soft-deleted threads become visible again).
  const [statusFilter, setStatusFilter] = useState("");
  const [hasMore, setHasMore] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<ThreadMeta | null>(null);
  const [renameValue, setRenameValue] = useState("");
  // Bumped to force a reload after a mutation (rename/archive/purge).
  const [reloadTick, setReloadTick] = useState(0);
  const offsetRef = useRef(0);

  // Debounce the search box → server ``q``.
  useEffect(() => {
    const handle = setTimeout(() => setDebouncedQuery(query.trim()), 300);
    return () => clearTimeout(handle);
  }, [query]);

  const load = useCallback(
    async (append: boolean) => {
      setLoading(true);
      try {
        const offset = append ? offsetRef.current : 0;
        const page = await listSessions({
          agentName,
          q: debouncedQuery || undefined,
          status: statusFilter || undefined,
          limit: PAGE_SIZE,
          offset,
          tenantScope: apiTenantScope,
        });
        offsetRef.current = offset + page.length;
        setHasMore(page.length === PAGE_SIZE);
        setSessions((prev) => (append ? [...prev, ...page] : page));
      } catch {
        if (!append) setSessions([]);
      } finally {
        setLoading(false);
      }
    },
    [agentName, debouncedQuery, statusFilter, apiTenantScope],
  );

  // Reload from the top whenever the drawer opens, the search / status filter
  // changes, or a mutation bumps ``reloadTick``.
  useEffect(() => {
    if (!open) return;
    offsetRef.current = 0;
    void load(false);
  }, [open, debouncedQuery, statusFilter, reloadTick, load]);

  const triggerChanged = useCallback(() => {
    setReloadTick((n) => n + 1);
    onChanged?.();
  }, [onChanged]);

  const handleResume = useCallback(
    (session: ThreadMeta) => {
      onResume(session);
      onClose();
    },
    [onResume, onClose],
  );

  const submitRename = useCallback(async () => {
    if (!renaming) return;
    const title = renameValue.trim();
    if (!title) return;
    setBusyId(renaming.thread_id);
    try {
      await renameSession(renaming.thread_id, title);
      message.success(t("session_history.rename_ok"));
      setRenaming(null);
      triggerChanged();
    } catch {
      message.error(t("session_history.action_failed"));
    } finally {
      setBusyId(null);
    }
  }, [renaming, renameValue, message, t, triggerChanged]);

  const handleArchive = useCallback(
    async (threadId: string) => {
      setBusyId(threadId);
      try {
        await archiveSession(threadId);
        message.success(t("session_history.archive_ok"));
        triggerChanged();
      } catch {
        message.error(t("session_history.action_failed"));
      } finally {
        setBusyId(null);
      }
    },
    [message, t, triggerChanged],
  );

  const handlePurge = useCallback(
    async (threadId: string) => {
      setBusyId(threadId);
      try {
        await purgeSession(threadId);
        message.success(t("session_history.purge_ok"));
        triggerChanged();
      } catch {
        message.error(t("session_history.action_failed"));
      } finally {
        setBusyId(null);
      }
    },
    [message, t, triggerChanged],
  );

  const titleOf = useCallback(
    (s: ThreadMeta): string => s.title?.trim() || `${s.thread_id.slice(0, 8)}…`,
    [],
  );

  const empty = useMemo(
    () => (
      <Empty
        description={
          debouncedQuery
            ? t("session_history.empty_search")
            : t("session_history.empty")
        }
        style={{ marginTop: 48 }}
      />
    ),
    [debouncedQuery, t],
  );

  return (
    <Drawer
      open={open}
      onClose={onClose}
      title={t("session_history.title")}
      width={420}
      destroyOnHidden
      styles={{
        body: { padding: 12, display: "flex", flexDirection: "column" },
      }}
      data-testid="session-history-drawer"
    >
      <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
        <Input.Search
          placeholder={t("session_history.search_placeholder")}
          aria-label={t("session_history.search_placeholder")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          allowClear
          loading={loading}
          style={{ flex: 1 }}
          data-testid="session-history-search"
        />
        <Select
          value={statusFilter}
          onChange={setStatusFilter}
          aria-label={t("session_history.filter_status")}
          style={{ width: 108, flexShrink: 0 }}
          data-testid="session-history-status-filter"
          options={[
            { value: "", label: t("session_history.filter_all") },
            { value: "active", label: t("session_history.status_active") },
            { value: "paused", label: t("session_history.status_paused") },
            {
              value: "completed",
              label: t("session_history.status_completed"),
            },
            { value: "failed", label: t("session_history.status_failed") },
            {
              value: "cancelled",
              label: t("session_history.status_cancelled"),
            },
            {
              value: "archived",
              label: t("session_history.status_archived"),
            },
          ]}
        />
      </div>
      {sessions.length === 0 && !loading ? (
        empty
      ) : (
        <List
          size="small"
          dataSource={sessions}
          loading={loading && sessions.length === 0}
          data-testid="session-history-list"
          renderItem={(s) => {
            const isCurrent = s.thread_id === currentThreadId;
            const rowBusy = busyId === s.thread_id;
            return (
              <List.Item
                key={s.thread_id}
                style={{
                  padding: "8px 8px",
                  borderRadius: 6,
                  cursor: "pointer",
                  background: isCurrent
                    ? "var(--ew-surface-selected)"
                    : undefined,
                }}
                onClick={() => handleResume(s)}
                data-testid={`session-history-item-${s.thread_id}`}
                actions={[
                  // Each action sits in a stopPropagation span so a click on it
                  // (or its Popconfirm trigger) never bubbles to the row's
                  // resume handler — antd's Popconfirm swallows the child's own
                  // stopPropagation, so guard at the wrapper.
                  <span key="rename" onClick={(e) => e.stopPropagation()}>
                    <Button
                      type="text"
                      size="small"
                      icon={<Pencil size={13} strokeWidth={1.75} />}
                      aria-label={`${t("session_history.rename")}：${titleOf(s)}`}
                      loading={rowBusy}
                      onClick={() => {
                        setRenameValue(s.title ?? "");
                        setRenaming(s);
                      }}
                      data-testid={`session-history-rename-${s.thread_id}`}
                    />
                  </span>,
                  <span key="archive" onClick={(e) => e.stopPropagation()}>
                    <Popconfirm
                      title={t("session_history.archive_confirm")}
                      okText={t("session_history.archive")}
                      cancelText={t("session_history.cancel")}
                      onConfirm={() => handleArchive(s.thread_id)}
                    >
                      <Button
                        type="text"
                        size="small"
                        icon={<X size={13} strokeWidth={1.75} />}
                        aria-label={`${t("session_history.archive")}：${titleOf(s)}`}
                        data-testid={`session-history-archive-${s.thread_id}`}
                      />
                    </Popconfirm>
                  </span>,
                  <span key="purge" onClick={(e) => e.stopPropagation()}>
                    <Popconfirm
                      title={t("session_history.purge_confirm")}
                      description={t("session_history.purge_warning")}
                      okText={t("session_history.purge")}
                      okButtonProps={{ danger: true }}
                      cancelText={t("session_history.cancel")}
                      onConfirm={() => handlePurge(s.thread_id)}
                    >
                      <Button
                        type="text"
                        size="small"
                        danger
                        icon={<Trash2 size={13} strokeWidth={1.75} />}
                        aria-label={`${t("session_history.purge")}：${titleOf(s)}`}
                        data-testid={`session-history-purge-${s.thread_id}`}
                      />
                    </Popconfirm>
                  </span>,
                ]}
              >
                <List.Item.Meta
                  title={
                    <span style={{ fontSize: 13, fontWeight: 500 }}>
                      {titleOf(s)}
                    </span>
                  }
                  description={
                    <Space size={6} wrap style={{ fontSize: 11 }}>
                      <Tag
                        bordered={false}
                        color={STATUS_COLOR[s.status] ?? "default"}
                        style={{ fontSize: 10, marginInlineEnd: 0 }}
                      >
                        {t(`session_history.status_${s.status}`, {
                          defaultValue: s.status,
                        })}
                      </Tag>
                      <span style={{ color: "var(--ew-text-tertiary)" }}>
                        {relativeTime(s.updated_at, t)}
                      </span>
                    </Space>
                  }
                />
              </List.Item>
            );
          }}
        />
      )}
      {hasMore && (
        <Button
          block
          size="small"
          icon={<MoreHorizontal size={13} strokeWidth={1.75} />}
          loading={loading}
          onClick={() => void load(true)}
          style={{ marginTop: 8 }}
          data-testid="session-history-load-more"
        >
          {t("session_history.load_more")}
        </Button>
      )}

      <Modal
        open={renaming !== null}
        title={t("session_history.rename")}
        onCancel={() => setRenaming(null)}
        onOk={() => void submitRename()}
        okText={t("session_history.rename_ok_button")}
        cancelText={t("session_history.cancel")}
        okButtonProps={{ disabled: !renameValue.trim() }}
        confirmLoading={busyId !== null && busyId === renaming?.thread_id}
        destroyOnHidden
      >
        <Input
          value={renameValue}
          onChange={(e) => setRenameValue(e.target.value)}
          maxLength={200}
          placeholder={t("session_history.rename_placeholder")}
          aria-label={t("session_history.rename")}
          onPressEnter={() => void submitRename()}
          data-testid="session-history-rename-input"
        />
      </Modal>
    </Drawer>
  );
}
