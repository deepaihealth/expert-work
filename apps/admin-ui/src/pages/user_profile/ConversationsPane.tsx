/**
 * UserProfile — Conversations pane. Agent-agnostic (keyed on ``userId``),
 * with an in-tab agent filter (from ``listAgents``) and an active-since
 * date-range filter (the backend takes a single ``since`` instant, so the
 * range's start is what narrows the list).
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  DatePicker,
  Empty,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { TableColumnsType } from "antd";
import type { Dayjs } from "dayjs";
import { AlertTriangle } from "lucide-react";
import { useLocation, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { listAgents } from "../../api/agents";
import {
  listConversations,
  type ConversationListItem,
} from "../../api/conversations";
import { concreteTenantScope, useTenantScope } from "../../tenant/TenantScopeContext";
import { formatCompact } from "../../utils/runFormat";
import { useLoad } from "./useLoad";

const { Text } = Typography;
const { RangePicker } = DatePicker;

const STATUS_COLOR: Record<string, string> = {
  active: "processing",
  paused: "warning",
  completed: "success",
  failed: "error",
  cancelled: "default",
  archived: "default",
};

export function ConversationsPane({ userId }: { userId: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  // Cross-tenant W3 — user detail is single-tenant semantics: concrete UUID only.
  const { apiTenantScope } = useTenantScope();

  const [agentName, setAgentName] = useState<string | undefined>(undefined);
  const [since, setSince] = useState<string | undefined>(undefined);
  const [agentNames, setAgentNames] = useState<string[]>([]);

  // Populate the agent filter once (distinct names across versions).
  useEffect(() => {
    let cancelled = false;
    listAgents({ limit: 100 })
      .then((res) => {
        if (cancelled) return;
        setAgentNames([...new Set(res.items.map((a) => a.name))].sort());
      })
      .catch(() => {
        // Agent filter is a convenience — leave it empty on failure.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const load = useCallback(
    // Cross-tenant W3 — user detail is single-tenant semantics: concrete UUID only.
    () =>
      listConversations({
        userId,
        agentName,
        since,
        tenantScope: concreteTenantScope(apiTenantScope),
      }),
    [userId, agentName, since, apiTenantScope],
  );
  const { data, loading, error } = useLoad(load);

  const onRange = (range: [Dayjs | null, Dayjs | null] | null) => {
    const start = range?.[0];
    setSince(start ? start.startOf("day").toISOString() : undefined);
  };

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
        title: t("conversations_page.column_agent"),
        key: "agent",
        width: 150,
        render: (_: unknown, record) =>
          record.agent_name ? (
            <Text style={{ fontSize: 12 }}>{record.agent_name}</Text>
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
        width: 100,
        render: (_: unknown, record) => (
          <Space size={6}>
            <Text style={{ fontVariantNumeric: "tabular-nums" }}>{record.run_count}</Text>
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
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
        <Select<string | undefined>
          allowClear
          placeholder={t("user_profile.filter_agent")}
          value={agentName}
          onChange={(value) => setAgentName(value)}
          options={agentNames.map((n) => ({ value: n, label: n }))}
          style={{ minWidth: 200 }}
          data-testid="user-conversations-agent"
        />
        <RangePicker
          onChange={(range) => onRange(range as [Dayjs | null, Dayjs | null] | null)}
          placeholder={[t("user_profile.filter_range"), t("user_profile.filter_range")]}
          data-testid="user-conversations-range"
        />
      </div>
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
        locale={{ emptyText: <Empty description={t("user_profile.conversations_empty")} /> }}
        data-testid="user-conversations-table"
      />
    </div>
  );
}
