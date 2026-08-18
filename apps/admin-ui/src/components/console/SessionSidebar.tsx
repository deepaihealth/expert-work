/**
 * SessionSidebar — the debug console's always-visible left-hand session
 * list (debug console redesign PR-A Task 6).
 *
 * Ports the browse / search / rename / archive / purge logic out of the
 * old ``SessionHistoryDrawer`` into a sidebar with no Drawer chrome:
 *   - the 6-value status ``<Select>`` becomes a 2-value Active/Archived
 *     ``<Segmented>`` (the old Select's virtual dropdown never renders its
 *     options under jsdom; a Segmented's options are plain DOM, so this is
 *     also the more testable choice — and the spec only ever needed the
 *     two states).
 *   - the current thread is highlighted, and while a run is in flight on
 *     it a small breathing dot shows next to its title; clicking any row
 *     is disabled for the duration (nothing to switch to mid-run).
 *   - every write control (new / rename / archive / purge) is gated by
 *     ``readOnly`` (the switched-in tenant scope, funnelled down from the
 *     parent) via the shared ``ReadonlyTooltip`` — the list itself stays
 *     browsable and clickable.
 *
 * Row rendering (rename/archive/purge actions) lives in
 * ``SessionSidebarItem.tsx`` to keep this file under the 400-line budget.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { App, Button, Empty, Input, List, Modal, Segmented, Typography } from "antd";
import { MoreHorizontal, Plus } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  archiveSession,
  listSessions,
  purgeSession,
  renameSession,
  type ThreadMeta,
} from "../../api/sessions";
import { useTenantScope } from "../../tenant/TenantScopeContext";
import { ReadonlyTooltip } from "../ReadonlyTooltip";
import { SessionSidebarItem } from "./SessionSidebarItem";
import "./SessionSidebar.css";

const PAGE_SIZE = 50;

/** What changed, for the parent to react to (refresh a title / fall back
 *  to the draft when the current thread was just purged). */
export interface SessionChange {
  kind: "rename" | "archive" | "purge";
  threadId: string;
  title?: string;
}

export interface SessionSidebarProps {
  agentName: string;
  currentThreadId: string | null;
  /** 当前会话是否有 run 在跑(R3:活动点 + 禁用切换)。 */
  running: boolean;
  onNew: () => void;
  onResume: (session: ThreadMeta) => void;
  /** 切入态只读:新建 / 改名 / 归档 / 删除置灰,列表仍可看可切。 */
  readOnly?: boolean;
  /** 会话被改名 / 归档 / 删除后回调(父级刷新标题 / 若删的是当前会话则回到草稿)。 */
  onChanged?: (change: SessionChange) => void;
  /** 列表刷新触发器:父级 thread 从 null 变成新建的会话时 +1,让新会话出现在列表顶部。 */
  reloadTick?: number;
}

export function SessionSidebar({
  agentName,
  currentThreadId,
  running,
  onNew,
  onResume,
  readOnly = false,
  onChanged,
  reloadTick = 0,
}: SessionSidebarProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const { apiTenantScope } = useTenantScope();

  const [sessions, setSessions] = useState<ThreadMeta[]>([]);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<"" | "archived">("");
  const [hasMore, setHasMore] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<ThreadMeta | null>(null);
  const [renameValue, setRenameValue] = useState("");
  // Bumped after a mutation (rename/archive/purge) to force a reload.
  const [refreshTick, setRefreshTick] = useState(0);
  const offsetRef = useRef(0);

  // Debounce the search box → server `q`.
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
          orderBy: "last_activity",
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

  // Reload from the top whenever the search / status filter changes, a
  // mutation bumps `refreshTick`, or the parent bumps `reloadTick` (a new
  // session was created elsewhere).
  useEffect(() => {
    offsetRef.current = 0;
    void load(false);
  }, [debouncedQuery, statusFilter, refreshTick, reloadTick, load]);

  const refresh = useCallback(() => setRefreshTick((n) => n + 1), []);

  const handleResume = useCallback(
    (session: ThreadMeta) => {
      if (running) return;
      onResume(session);
    },
    [running, onResume],
  );

  const submitRename = useCallback(async () => {
    if (!renaming) return;
    const title = renameValue.trim();
    if (!title) return;
    const threadId = renaming.thread_id;
    setBusyId(threadId);
    try {
      await renameSession(threadId, title);
      message.success(t("session_history.rename_ok"));
      setRenaming(null);
      refresh();
      onChanged?.({ kind: "rename", threadId, title });
    } catch {
      message.error(t("session_history.action_failed"));
    } finally {
      setBusyId(null);
    }
  }, [renaming, renameValue, message, t, refresh, onChanged]);

  const handleArchive = useCallback(
    async (threadId: string) => {
      setBusyId(threadId);
      try {
        await archiveSession(threadId);
        message.success(t("session_history.archive_ok"));
        refresh();
        onChanged?.({ kind: "archive", threadId });
      } catch {
        message.error(t("session_history.action_failed"));
      } finally {
        setBusyId(null);
      }
    },
    [message, t, refresh, onChanged],
  );

  const handlePurge = useCallback(
    async (threadId: string) => {
      setBusyId(threadId);
      try {
        await purgeSession(threadId);
        message.success(t("session_history.purge_ok"));
        refresh();
        onChanged?.({ kind: "purge", threadId });
      } catch {
        message.error(t("session_history.action_failed"));
      } finally {
        setBusyId(null);
      }
    },
    [message, t, refresh, onChanged],
  );

  const startRename = useCallback((s: ThreadMeta) => {
    setRenameValue(s.title ?? "");
    setRenaming(s);
  }, []);

  const empty = useMemo(
    () => (
      <Empty
        description={
          debouncedQuery
            ? t("session_history.empty_search")
            : t("session_history.empty")
        }
        style={{ marginTop: 48 }}
        data-testid="console-session-empty"
      />
    ),
    [debouncedQuery, t],
  );

  return (
    <div
      data-testid="console-session-sidebar"
      style={{ display: "flex", flexDirection: "column", height: "100%" }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "8px 8px 4px",
        }}
      >
        <Typography.Text strong style={{ fontSize: 13 }}>
          {t("console.sidebar_title")}
        </Typography.Text>
        <ReadonlyTooltip on={readOnly}>
          <Button
            type="primary"
            size="small"
            icon={<Plus size={12} strokeWidth={1.75} />}
            onClick={onNew}
            disabled={running || readOnly}
            data-testid="playground-new-session"
          >
            {t("console.sidebar_new")}
          </Button>
        </ReadonlyTooltip>
      </div>

      <div style={{ display: "flex", gap: 8, padding: "0 8px 8px" }}>
        <Input.Search
          placeholder={t("console.sidebar_search")}
          aria-label={t("console.sidebar_search")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          allowClear
          loading={loading}
          style={{ flex: 1 }}
          data-testid="console-session-search"
        />
        <Segmented
          value={statusFilter}
          onChange={(value) => setStatusFilter(value as "" | "archived")}
          options={[
            { value: "", label: t("console.sidebar_filter_active") },
            { value: "archived", label: t("console.sidebar_filter_archived") },
          ]}
          data-testid="console-session-filter"
        />
      </div>

      <div style={{ flex: 1, overflowY: "auto", padding: "0 8px" }}>
        {sessions.length === 0 && !loading ? (
          empty
        ) : (
          <List
            size="small"
            dataSource={sessions}
            loading={loading && sessions.length === 0}
            data-testid="console-session-list"
            renderItem={(s) => (
              <SessionSidebarItem
                key={s.thread_id}
                session={s}
                isCurrent={s.thread_id === currentThreadId}
                running={running}
                readOnly={readOnly}
                busy={busyId === s.thread_id}
                onResume={handleResume}
                onRenameStart={startRename}
                onArchive={handleArchive}
                onPurge={handlePurge}
              />
            )}
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
            data-testid="console-session-load-more"
          >
            {t("session_history.load_more")}
          </Button>
        )}
      </div>

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
          data-testid="console-session-rename-input"
        />
      </Modal>
    </div>
  );
}
