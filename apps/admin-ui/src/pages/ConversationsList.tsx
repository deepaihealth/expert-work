/**
 * Global conversations browser — the top-level operations entry
 * (``docs/design/conversation-centric-ia.md`` §3 primitive ③).
 *
 * Replaces the flat cross-agent ``/runs`` list: a conversation
 * (``thread_meta`` + its ``agent_run`` rollup) is the operational unit,
 * so the browser lists conversations across agents with status / user /
 * free-text filters and drills into ``/conversations/:threadId`` — which
 * then drills into the per-run detail. Mirrors the previous ``RunsList``
 * shell (cross-tenant banner, URL-owned ``?user_id=`` filter, debounced
 * search) so the operational UX carries over.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Checkbox,
  Empty,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { TableColumnsType } from "antd";
import { AlertTriangle, Globe2, MessagesSquare, RefreshCw, Search } from "lucide-react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { listAgents } from "../api/agents";
import {
  listConversations,
  type ConversationList,
  type ConversationListItem,
  type ConversationStatus,
} from "../api/conversations";
import { ApiError } from "../api/client";
import { useTenantScope } from "../tenant/TenantScopeContext";
import { PageHeader } from "../components/PageHeader";
import { formatCompact } from "../utils/runFormat";

const { Text } = Typography;

const STATUS_COLOR: Record<string, string> = {
  active: "processing",
  paused: "warning",
  completed: "success",
  failed: "error",
  cancelled: "default",
  archived: "default",
};

const STATUS_OPTIONS: ConversationStatus[] = [
  "active",
  "paused",
  "completed",
  "failed",
  "cancelled",
  "archived",
];

const PAGE_SIZE = 50;

/** Activity-window presets for the ``since`` filter — hours → i18n key. */
const TIME_WINDOWS: ReadonlyArray<{ hours: number; labelKey: string }> = [
  { hours: 1, labelKey: "conversations_page.window_1h" },
  { hours: 24, labelKey: "conversations_page.window_24h" },
  { hours: 168, labelKey: "conversations_page.window_7d" },
];

export function ConversationsList() {
  const { t } = useTranslation();
  const { scope, apiTenantScope } = useTenantScope();
  const navigate = useNavigate();
  const location = useLocation();
  const [data, setData] = useState<ConversationList | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [agentOptions, setAgentOptions] = useState<string[]>([]);
  // Monitoring mode — silently re-poll the list every 30s while enabled.
  const [autoRefresh, setAutoRefresh] = useState(false);
  // Monotonic request id — a late response from a superseded request
  // (filter/page changed mid-flight) must not overwrite the newer one.
  const requestSeq = useRef(0);

  // Every filter is URL-owned, so the browser view is shareable, survives
  // refresh, and the conversation-detail back link restores it verbatim
  // (the detail page navigates back to ``location.state.from``).
  const [searchParams, setSearchParams] = useSearchParams();
  const statusFilter = (searchParams.get("status") as ConversationStatus | null) ?? undefined;
  const agentFilter = searchParams.get("agent") ?? undefined;
  const errorsOnly = searchParams.get("errors") === "1";
  const pendingOnly = searchParams.get("pending") === "1";
  const windowHours = Number(searchParams.get("window")) || undefined;
  const q = searchParams.get("q") ?? undefined;
  const page = Math.max(1, Number(searchParams.get("page")) || 1);
  const userFilter = searchParams.get("user_id") ?? undefined;

  // The search box itself is local; it debounces into ``?q=`` below.
  const [search, setSearch] = useState(q ?? "");

  const setParam = useCallback(
    (key: string, value: string | undefined, opts?: { push?: boolean }) => {
      setSearchParams(
        (prev) => {
          if (value === undefined) {
            prev.delete(key);
          } else {
            prev.set(key, value);
          }
          // Any filter change restarts pagination — page N of the old
          // filter set is meaningless under the new one.
          if (key !== "page") {
            prev.delete("page");
          }
          return prev;
        },
        { replace: !opts?.push },
      );
    },
    [setSearchParams],
  );

  // user_id keeps push semantics: it's a row-click drill-down, so browser
  // back undoes the filter.
  const setUserFilter = useCallback(
    (id: string) => setParam("user_id", id, { push: true }),
    [setParam],
  );
  const clearUserFilter = useCallback(
    () => setParam("user_id", undefined, { push: true }),
    [setParam],
  );

  // Debounce the search box into the server ``q`` param (substring match on
  // the conversation title — server-side so it spans all pages).
  useEffect(() => {
    const handle = setTimeout(() => {
      const next = search.trim() || undefined;
      if (next !== q) setParam("q", next);
    }, 300);
    return () => clearTimeout(handle);
  }, [search, q, setParam]);

  // A tenant-scope switch invalidates the pager position (different data
  // set); deep-linked ``?page=`` on first mount stays intact.
  const scopeRef = useRef(apiTenantScope);
  useEffect(() => {
    if (scopeRef.current !== apiTenantScope) {
      scopeRef.current = apiTenantScope;
      setParam("page", undefined);
    }
  }, [apiTenantScope, setParam]);

  const refresh = useCallback(async (opts?: { silent?: boolean }) => {
    const seq = ++requestSeq.current;
    if (!opts?.silent) {
      setLoading(true);
    }
    setError(null);
    try {
      const result = await listConversations({
        tenantScope: apiTenantScope,
        status: statusFilter,
        agentName: agentFilter,
        hasError: errorsOnly,
        hasPending: pendingOnly,
        since: windowHours
          ? new Date(Date.now() - windowHours * 3_600_000).toISOString()
          : undefined,
        q,
        userId: userFilter,
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
      });
      if (seq !== requestSeq.current) return;
      setData(result);
    } catch (err) {
      if (seq !== requestSeq.current) return;
      const message =
        err instanceof ApiError
          ? `${err.code}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "unknown error";
      setError(message);
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [
    apiTenantScope,
    statusFilter,
    agentFilter,
    errorsOnly,
    pendingOnly,
    windowHours,
    q,
    userFilter,
    page,
  ]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 30s silent poll while auto-refresh is on — the monitoring-wall mode.
  // ``silent`` keeps the table from flashing its loading state each tick.
  useEffect(() => {
    if (!autoRefresh) return;
    const id = setInterval(() => {
      void refresh({ silent: true });
    }, 30_000);
    return () => clearInterval(id);
  }, [autoRefresh, refresh]);

  // Agent-filter options — best-effort; a failure just leaves the
  // dropdown empty (the list itself is unaffected).
  useEffect(() => {
    let cancelled = false;
    listAgents({ tenantScope: apiTenantScope, limit: 100 })
      .then((result) => {
        if (cancelled) return;
        setAgentOptions([...new Set(result.items.map((a) => a.name))].sort());
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [apiTenantScope]);

  const isCrossTenant = data?.cross_tenant ?? false;

  const columns: TableColumnsType<ConversationListItem> = useMemo(
    () => [
      {
        title: t("conversations_page.column_conversation"),
        key: "conversation",
        render: (_: unknown, record) => (
          <Space direction="vertical" size={0}>
            <Text strong>{record.title ?? t("conversations_page.untitled")}</Text>
            <Tooltip title={record.thread_id}>
              <Text code style={{ fontSize: 11 }}>
                {record.thread_id.slice(0, 8)}…
              </Text>
            </Tooltip>
          </Space>
        ),
      },
      {
        title: t("conversations_page.column_agent"),
        dataIndex: "agent_name",
        key: "agent",
        width: 180,
        render: (name: string | null, record) => {
          if (name === null) {
            return <Text type="secondary">—</Text>;
          }
          return (
            <Space size={6}>
              <Text strong>{name}</Text>
              {record.agent_version && (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  v{record.agent_version}
                </Text>
              )}
            </Space>
          );
        },
      },
      {
        title: t("conversations_page.column_user"),
        dataIndex: "user_id",
        key: "user",
        width: 130,
        render: (uid: string | null) => {
          if (!uid) return <Text type="secondary">—</Text>;
          // Click filters the list to this user (URL ?user_id=…).
          // stopPropagation so it doesn't also trigger the row navigation.
          return (
            <Tooltip title={t("conversations_page.filter_user_tip")}>
              <span
                role="button"
                tabIndex={0}
                data-testid={`conversation-user-${uid}`}
                onClick={(e) => {
                  e.stopPropagation();
                  setUserFilter(uid);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.stopPropagation();
                    setUserFilter(uid);
                  }
                }}
                style={{ cursor: "pointer" }}
              >
                <Text code style={{ fontSize: 12, color: "var(--ew-accent-cyan, #13c2c2)" }}>
                  {uid.slice(0, 8)}…
                </Text>
              </span>
            </Tooltip>
          );
        },
      },
      {
        title: t("conversations_page.column_status"),
        dataIndex: "status",
        key: "status",
        width: 120,
        render: (status: string) => (
          <Tag color={STATUS_COLOR[status] ?? "default"}>
            {t(`conversations_page.status_${status}`, { defaultValue: status })}
          </Tag>
        ),
      },
      {
        title: t("conversations_page.column_runs"),
        key: "runs",
        width: 110,
        render: (_: unknown, record) => (
          <Space size={6}>
            <Text>{record.run_count}</Text>
            {record.error_count > 0 && (
              <Tooltip title={t("conversations_page.error_count", { count: record.error_count })}>
                <Space size={2} data-testid={`conversations-page-error-${record.thread_id}`}>
                  <AlertTriangle
                    size={13}
                    strokeWidth={1.5}
                    color="var(--ew-status-error, #f5222d)"
                  />
                </Space>
              </Tooltip>
            )}
            {record.pending_count > 0 && (
              <Tooltip
                title={t("conversations_page.pending_count", { count: record.pending_count })}
              >
                <Tag color="warning" style={{ marginInlineEnd: 0 }}>
                  {record.pending_count}
                </Tag>
              </Tooltip>
            )}
          </Space>
        ),
      },
      {
        title: t("conversations_page.column_tokens"),
        key: "tokens",
        width: 90,
        render: (_: unknown, record) => {
          const tk = record.tokens;
          if (!tk || tk.total_tokens === 0) return <Text type="secondary">—</Text>;
          return (
            <Tooltip
              title={t("runs_page.tokens_tip", {
                input: tk.input_tokens,
                output: tk.output_tokens,
                calls: tk.llm_calls,
              })}
            >
              <Text style={{ fontSize: 12 }} data-testid={`conversation-tokens-${record.thread_id}`}>
                {formatCompact(tk.total_tokens)}
              </Text>
            </Tooltip>
          );
        },
      },
      {
        title: t("conversations_page.column_last_active"),
        dataIndex: "last_run_at",
        key: "last_run_at",
        width: 190,
        render: (iso: string | null) =>
          iso ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {new Date(iso).toLocaleString()}
            </Text>
          ) : (
            <Text type="secondary">—</Text>
          ),
      },
    ],
    [t, setUserFilter],
  );

  return (
    <div>
      <PageHeader
        icon={<MessagesSquare size={18} strokeWidth={1.5} />}
        title={t("conversations_page.page_title")}
        actions={
          <>
            {isCrossTenant && (
              <Tag
                icon={<Globe2 size={12} strokeWidth={1.5} />}
                color="purple"
                data-testid="cross-tenant-banner"
              >
                {t("conversations_page.cross_tenant_banner")}
              </Tag>
            )}
            {userFilter && (
              <Tag
                closable
                onClose={clearUserFilter}
                color="cyan"
                data-testid="conversations-user-filter-chip"
              >
                {t("conversations_page.filter_user_active", { user: userFilter.slice(0, 8) })}
              </Tag>
            )}
            <Checkbox
              checked={errorsOnly}
              onChange={(e) => setParam("errors", e.target.checked ? "1" : undefined)}
              data-testid="conversations-errors-only"
            >
              {t("conversations_page.filter_errors_only")}
            </Checkbox>
            <Checkbox
              checked={pendingOnly}
              onChange={(e) => setParam("pending", e.target.checked ? "1" : undefined)}
              data-testid="conversations-pending-only"
            >
              {t("conversations_page.filter_pending_only")}
            </Checkbox>
            <Checkbox
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
              data-testid="conversations-auto-refresh"
            >
              {t("conversations_page.auto_refresh")}
            </Checkbox>
            <Input
              allowClear
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={t("conversations_page.search_placeholder")}
              aria-label={t("conversations_page.search_placeholder")}
              prefix={<Search size={14} strokeWidth={1.5} />}
              style={{ width: 220 }}
              data-testid="conversations-search"
            />
            <Select<string>
              allowClear
              showSearch
              value={agentFilter}
              onChange={(v) => setParam("agent", v || undefined)}
              placeholder={t("conversations_page.filter_agent")}
              aria-label={t("conversations_page.filter_agent")}
              style={{ width: 190 }}
              data-testid="conversations-agent-filter"
              options={agentOptions.map((name) => ({ value: name, label: name }))}
            />
            <Select<ConversationStatus | "all">
              value={statusFilter ?? "all"}
              onChange={(v) => setParam("status", v === "all" ? undefined : v)}
              style={{ width: 160 }}
              aria-label={t("conversations_page.filter_status")}
              data-testid="conversations-status-filter"
              options={[
                { value: "all", label: t("conversations_page.filter_status_all") },
                ...STATUS_OPTIONS.map((s) => ({
                  value: s,
                  label: t(`conversations_page.status_${s}`),
                })),
              ]}
            />
            <Select<number | "all">
              value={windowHours ?? "all"}
              onChange={(v) => setParam("window", v === "all" ? undefined : String(v))}
              style={{ width: 140 }}
              aria-label={t("conversations_page.filter_window")}
              data-testid="conversations-window-filter"
              options={[
                { value: "all", label: t("conversations_page.window_all") },
                ...TIME_WINDOWS.map((w) => ({ value: w.hours, label: t(w.labelKey) })),
              ]}
            />
            <button
              type="button"
              onClick={() => void refresh()}
              disabled={loading}
              aria-label={t("common.refresh")}
              data-testid="conversations-refresh"
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                padding: "4px 10px",
                border: "1px solid var(--ew-border-default)",
                borderRadius: 6,
                background: "var(--ew-surface-raised)",
                color: "var(--ew-text-primary)",
                fontSize: 13,
                cursor: loading ? "wait" : "pointer",
              }}
            >
              <RefreshCw size={14} strokeWidth={1.5} />
              {loading ? t("common.loading") : t("common.refresh")}
            </button>
          </>
        }
      />

      {error !== null && (
        <Alert
          type="error"
          showIcon
          message={t("conversations_page.failed_to_load")}
          description={error}
          style={{ marginBottom: 16 }}
          data-testid="conversations-error"
        />
      )}

      <Table<ConversationListItem>
        columns={columns}
        dataSource={data?.items ?? []}
        rowKey={(record) => record.thread_id}
        loading={loading}
        pagination={{
          current: page,
          total: data?.total ?? 0,
          showSizeChanger: false,
          pageSize: PAGE_SIZE,
          // Paging pushes history so browser back returns to the prior page.
          onChange: (p) => setParam("page", p <= 1 ? undefined : String(p), { push: true }),
          showTotal: (n) => t("conversations_page.pager_total", { total: n }),
        }}
        onRow={(record) => ({
          // ``from`` lets the detail page's back link restore this exact
          // view — filters and page included (they all live in the URL).
          onClick: () =>
            navigate(`/conversations/${encodeURIComponent(record.thread_id)}`, {
              state: { from: `${location.pathname}${location.search}` },
            }),
          style: { cursor: "pointer" },
        })}
        locale={{
          emptyText: (
            <Empty
              description={
                scope === "*"
                  ? t("conversations_page.empty_cross")
                  : t("conversations_page.empty_home")
              }
            />
          ),
        }}
        data-testid="conversations-table"
      />
    </div>
  );
}
