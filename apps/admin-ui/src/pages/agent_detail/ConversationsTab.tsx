/**
 * Conversations tab — the conversation-centric operations view for one
 * agent (``docs/design/conversation-centric-ia.md``).
 *
 * Replaces the flat per-agent Runs tab: a conversation is the natural
 * unit ``(user_id, session_id=thread_id)``, so this lists the agent's
 * conversations (grouped by thread) with their run rollup — run count,
 * error / pending signals, tokens, last active — and drills into a
 * conversation detail, which then drills into an individual run.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Card, Empty, Input, Select, Space, Table, Tag, Tooltip, Typography } from "antd";
import type { TableColumnsType } from "antd";
import { AlertTriangle, MessagesSquare, Search } from "lucide-react";
import { useLocation, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";

import type { AgentDetailResponse } from "../../api/agents";
import { ApiError } from "../../api/client";
import {
  listConversations,
  type ConversationList,
  type ConversationListItem,
  type ConversationStatus,
} from "../../api/conversations";
import { formatCompact } from "../../utils/runFormat";

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
];

const PAGE_SIZE = 50;

interface ConversationsTabProps {
  detail: AgentDetailResponse;
}

export function ConversationsTab({ detail }: ConversationsTabProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const { name, version } = detail.record;

  const [data, setData] = useState<ConversationList | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<ConversationStatus | undefined>(undefined);
  // Full-text search (IA M4) — the server's q spans title AND message
  // content (transcript mirror), same box as the global browser.
  const [search, setSearch] = useState("");
  const [q, setQ] = useState<string | undefined>(undefined);
  const [page, setPage] = useState(1);

  useEffect(() => {
    const handle = setTimeout(() => setQ(search.trim() || undefined), 300);
    return () => clearTimeout(handle);
  }, [search]);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listConversations({
        agentName: name,
        agentVersion: version,
        status: statusFilter,
        q,
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
      });
      setData(result);
    } catch (err) {
      setError(err instanceof ApiError ? `${err.code}: ${err.message}` : String(err));
    } finally {
      setLoading(false);
    }
  }, [name, version, statusFilter, q, page]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // A filter change restarts pagination.
  useEffect(() => {
    setPage(1);
  }, [statusFilter, q]);

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
        title: t("conversations_page.column_user"),
        dataIndex: "user_id",
        key: "user",
        width: 130,
        render: (uid: string | null) =>
          uid ? (
            <Tooltip title={uid}>
              <Text code style={{ fontSize: 12 }}>
                {uid.slice(0, 8)}…
              </Text>
            </Tooltip>
          ) : (
            <Text type="secondary">—</Text>
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
                <Space size={2} data-testid={`conversation-error-${record.thread_id}`}>
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
              <Text style={{ fontSize: 12 }}>{formatCompact(tk.total_tokens)}</Text>
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
    [t],
  );

  return (
    <Card
      title={
        <Space size={8}>
          <MessagesSquare size={15} strokeWidth={1.5} />
          {t("conversations_tab.title")}
        </Space>
      }
      extra={
        <Space size={8}>
          <Input
            allowClear
            size="small"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t("conversations_page.search_placeholder")}
            aria-label={t("conversations_page.search_placeholder")}
            prefix={<Search size={13} strokeWidth={1.5} />}
            style={{ width: 220 }}
            data-testid="conversations-tab-search"
          />
          <Select<ConversationStatus | "all">
            value={statusFilter ?? "all"}
            onChange={(v) => setStatusFilter(v === "all" ? undefined : (v as ConversationStatus))}
            style={{ width: 150 }}
            size="small"
            aria-label={t("conversations_page.filter_status")}
            data-testid="conversations-tab-status-filter"
            options={[
              { value: "all", label: t("conversations_page.filter_status_all") },
              ...STATUS_OPTIONS.map((s) => ({ value: s, label: s })),
            ]}
          />
        </Space>
      }
      data-testid="conversations-tab-root"
    >
      {error !== null && (
        <Alert
          type="error"
          showIcon
          message={error}
          style={{ marginBottom: 12 }}
          data-testid="conversations-tab-error"
        />
      )}
      <Table<ConversationListItem>
        size="small"
        columns={columns}
        dataSource={data?.items ?? []}
        rowKey={(record) => record.thread_id}
        loading={loading}
        pagination={{
          current: page,
          total: data?.total ?? 0,
          showSizeChanger: false,
          pageSize: PAGE_SIZE,
          onChange: setPage,
        }}
        onRow={(record) => ({
          // ``from`` + ``fromLabel`` restore this tab (page included) instead
          // of the detail page's generic agent fallback.
          onClick: () =>
            navigate(`/conversations/${encodeURIComponent(record.thread_id)}`, {
              state: { from: `${location.pathname}${location.search}`, fromLabel: name },
            }),
          style: { cursor: "pointer" },
        })}
        locale={{ emptyText: <Empty description={t("conversations_tab.empty")} /> }}
        data-testid="conversations-tab-table"
      />
    </Card>
  );
}
