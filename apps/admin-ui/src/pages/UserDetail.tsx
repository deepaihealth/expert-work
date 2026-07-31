/**
 * User detail — one ``(agent, user_id)`` per-user instance
 * (``docs/design/conversation-centric-ia.md`` §3 primitive ②, M2).
 *
 * The middle layer of the user → conversation → run drill-down. Four
 * tabs assemble the user's persistent assets from existing per-user
 * endpoints: conversations (agent+user filtered), long-term memory and
 * artifacts (tenant-admin governance view via ``?user_id=`` — both are
 * cross-agent per-user assets, stated in the UI), and current-month
 * token usage.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Card, Empty, Skeleton, Space, Table, Tabs, Tag, Tooltip, Typography } from "antd";
import type { TableColumnsType } from "antd";
import { AlertTriangle, UserRound } from "lucide-react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import {
  listConversations,
  type ConversationListItem,
} from "../api/conversations";
import { listMemories, type MemoryItem, type MemoryKind } from "../api/memory";
import { getUsageTokens } from "../api/usage";
import { getTenantUser } from "../api/users";
import { PageHeader } from "../components/PageHeader";
import { useTenantScope } from "../tenant/TenantScopeContext";
import { ArtifactsPane } from "./user_detail/ArtifactsPane";
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

/** Generic best-effort loader — each tab fails independently. */
function useLoad<T>(load: () => Promise<T>): {
  data: T | null;
  loading: boolean;
  error: string | null;
} {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    load()
      .then((result) => {
        if (!cancelled) setData(result);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? `${err.code}: ${err.message}` : String(err));
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [load]);
  return { data, loading, error };
}

function ConversationsPane({
  agentName,
  agentVersion,
  userId,
}: {
  agentName: string;
  agentVersion: string;
  userId: string;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const load = useCallback(
    () => listConversations({ agentName, agentVersion, userId }),
    [agentName, agentVersion, userId],
  );
  const { data, loading, error } = useLoad(load);

  const columns: TableColumnsType<ConversationListItem> = useMemo(
    () => [
      {
        title: t("conversations_page.column_conversation"),
        key: "conversation",
        render: (_: unknown, record) => (
          <Space direction="vertical" size={0}>
            <Text strong>{record.title ?? t("conversations_page.untitled")}</Text>
            <Text code style={{ fontSize: 11 }}>
              {record.thread_id.slice(0, 8)}…
            </Text>
          </Space>
        ),
      },
      {
        title: t("conversations_page.column_status"),
        dataIndex: "status",
        key: "status",
        width: 120,
        render: (status: string) => <Tag color={STATUS_COLOR[status] ?? "default"}>{status}</Tag>,
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
                <AlertTriangle size={13} strokeWidth={1.5} color="var(--ew-status-error, #f5222d)" />
              </Tooltip>
            )}
          </Space>
        ),
      },
      {
        title: t("conversations_page.column_tokens"),
        key: "tokens",
        width: 90,
        render: (_: unknown, record) =>
          record.tokens && record.tokens.total_tokens > 0 ? (
            <Text style={{ fontSize: 12 }}>{formatCompact(record.tokens.total_tokens)}</Text>
          ) : (
            <Text type="secondary">—</Text>
          ),
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
    [t],
  );

  return (
    <div data-testid="user-conversations-pane">
      {error !== null && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
      <Table<ConversationListItem>
        size="small"
        columns={columns}
        dataSource={data?.items ?? []}
        rowKey={(r) => r.thread_id}
        loading={loading}
        pagination={{ total: data?.total ?? 0, showSizeChanger: false, pageSize: 50 }}
        onRow={(record) => ({
          // ``from`` + ``fromLabel`` let the detail page's back link return
          // here instead of falling through to the thread's agent.
          onClick: () =>
            navigate(`/conversations/${encodeURIComponent(record.thread_id)}`, {
              state: { from: `${location.pathname}${location.search}`, fromLabel: t("nav.users") },
            }),
          style: { cursor: "pointer" },
        })}
        locale={{ emptyText: <Empty description={t("user_detail.conversations_empty")} /> }}
        data-testid="user-conversations-table"
      />
    </div>
  );
}

function MemoryPane({ userId }: { userId: string }) {
  const { t } = useTranslation();
  const load = useCallback(() => listMemories({ userId }), [userId]);
  const { data, loading, error } = useLoad(load);

  const columns: TableColumnsType<MemoryItem> = useMemo(
    () => [
      {
        title: t("memory_tab.col_kind"),
        dataIndex: "kind",
        key: "kind",
        width: 110,
        render: (kind: MemoryKind) => (
          <Tag color={kind === "fact" ? "blue" : "purple"} bordered={false}>
            {kind}
          </Tag>
        ),
      },
      { title: t("memory_tab.col_content"), dataIndex: "content", key: "content", ellipsis: true },
      {
        title: t("memory_tab.col_created"),
        dataIndex: "created_at",
        key: "created_at",
        width: 200,
        render: (iso: string) => (
          <Text type="secondary" style={{ fontSize: 12 }}>
            {new Date(iso).toLocaleString()}
          </Text>
        ),
      },
    ],
    [t],
  );

  return (
    <div data-testid="user-memory-pane">
      {/* Memory is a cross-agent per-user asset (Mini-ADR H-13). */}
      <Alert type="info" showIcon message={t("user_detail.memory_scope_note")} style={{ marginBottom: 12 }} />
      {error !== null && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
      <Table<MemoryItem>
        size="small"
        columns={columns}
        dataSource={data?.items ?? []}
        rowKey="id"
        loading={loading}
        pagination={false}
        locale={{ emptyText: <Empty description={t("user_detail.memory_empty")} /> }}
        data-testid="user-memory-table"
      />
    </div>
  );
}

function UsagePane({ userId }: { userId: string }) {
  const { t } = useTranslation();
  const load = useCallback(() => getUsageTokens({ userId }), [userId]);
  const { data, loading, error } = useLoad(load);

  if (loading) return <Skeleton active paragraph={{ rows: 3 }} />;
  if (error !== null) return <Alert type="error" showIcon message={error} />;
  if (data === null) return null;
  const total = data.total;
  const totalTokens = total.input_tokens + total.output_tokens;

  return (
    <div data-testid="user-usage-pane">
      {/* Usage is the user's tenant-wide month (all agents). */}
      <Alert type="info" showIcon message={t("user_detail.usage_scope_note")} style={{ marginBottom: 12 }} />
      <Card size="small" title={t("user_detail.usage_month", { month: data.month })}>
        <dl
          style={{
            display: "grid",
            gridTemplateColumns: "160px 1fr",
            rowGap: 8,
            columnGap: 16,
            margin: 0,
            fontSize: 13,
          }}
        >
          <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("user_detail.usage_total")}</dt>
          <dd style={{ margin: 0 }} data-testid="user-usage-total">
            {formatCompact(totalTokens)}
          </dd>
          <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("user_detail.usage_in_out")}</dt>
          <dd style={{ margin: 0 }}>
            {formatCompact(total.input_tokens)} / {formatCompact(total.output_tokens)}
          </dd>
          <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("user_detail.usage_by_model")}</dt>
          <dd style={{ margin: 0 }}>
            {data.by_model.length > 0
              ? data.by_model
                  .map((g) => `${g.key}: ${formatCompact(g.input_tokens + g.output_tokens)}`)
                  .join(" · ")
              : "—"}
          </dd>
        </dl>
      </Card>
    </div>
  );
}

export function UserDetail() {
  const { t } = useTranslation();
  const { name, version, userId } = useParams<{
    name: string;
    version: string;
    userId: string;
  }>();
  // Display name: router state from the Users tab paints instantly; the
  // registry fetch (fast-follow ``GET /v1/users/{id}``) covers direct URL
  // opens / refreshes. Best-effort — a 404 (never-registered user) keeps
  // the id fallback.
  const location = useLocation();
  const stateName = (location.state as { displayName?: string } | null)?.displayName;
  const { apiTenantScope } = useTenantScope();
  const [fetchedName, setFetchedName] = useState<string | null>(null);
  useEffect(() => {
    if (!userId) return;
    let cancelled = false;
    getTenantUser(userId, apiTenantScope)
      .then((u) => {
        if (!cancelled && u.display_name) setFetchedName(u.display_name);
      })
      .catch(() => {
        // 404 / 403 — keep the fallback title.
      });
    return () => {
      cancelled = true;
    };
  }, [userId, apiTenantScope]);
  const displayName = stateName ?? fetchedName ?? undefined;

  if (!name || !version || !userId) {
    return <Empty description="Missing route params" style={{ marginTop: 80 }} />;
  }

  return (
    <div data-testid="user-detail-root">
      <PageHeader
        icon={<UserRound size={18} strokeWidth={1.5} />}
        title={displayName ?? `${userId.slice(0, 12)}…`}
        backTo={{
          label: name,
          to: `/agents/${encodeURIComponent(name)}/${encodeURIComponent(version)}/users`,
        }}
        subtitle={
          <Space size={8} wrap>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {name} v{version}
            </Text>
            <Text code style={{ fontSize: 12 }}>
              {userId}
            </Text>
          </Space>
        }
      />
      <Tabs
        defaultActiveKey="conversations"
        items={[
          {
            key: "conversations",
            label: t("user_detail.tab_conversations"),
            children: (
              <ConversationsPane agentName={name} agentVersion={version} userId={userId} />
            ),
          },
          {
            key: "memory",
            label: t("user_detail.tab_memory"),
            children: <MemoryPane userId={userId} />,
          },
          {
            key: "artifacts",
            label: t("user_detail.tab_artifacts"),
            children: <ArtifactsPane userId={userId} />,
          },
          {
            key: "usage",
            label: t("user_detail.tab_usage"),
            children: <UsagePane userId={userId} />,
          },
        ]}
      />
    </div>
  );
}
