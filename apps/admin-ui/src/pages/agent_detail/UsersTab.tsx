/**
 * Users tab — the top of the user → conversation → run drill-down
 * (``docs/design/conversation-centric-ia.md`` §3 primitive ②, M2).
 *
 * Lists every end-user with ≥1 conversation on this agent (the per-user
 * instance dimension of the per-user persistent-agent product form),
 * with their conversation / run rollup, token totals, and last-active
 * clock. A row drills into the user detail (conversations + memory +
 * artifacts + usage).
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Card, Empty, Space, Table, Tag, Tooltip, Typography } from "antd";
import type { TableColumnsType } from "antd";
import { AlertTriangle, Users } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";

import type { AgentDetailResponse } from "../../api/agents";
import { ApiError } from "../../api/client";
import { useTenantScope } from "../../tenant/TenantScopeContext";
import { listAgentUsers, type AgentUserItem, type AgentUserList } from "../../api/users";
import { formatCompact } from "../../utils/runFormat";

const { Text } = Typography;

interface UsersTabProps {
  detail: AgentDetailResponse;
}

export function UsersTab({ detail }: UsersTabProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { name, version } = detail.record;

  const [data, setData] = useState<AgentUserList | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Stream N — the lists are scope-aware ("*" aggregates every tenant),
  // so the ambient scope rides along for a switched-in system_admin.
  const { apiTenantScope } = useTenantScope();

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await listAgentUsers(name, version, { tenantScope: apiTenantScope }));
    } catch (err) {
      setError(err instanceof ApiError ? `${err.code}: ${err.message}` : String(err));
    } finally {
      setLoading(false);
    }
  }, [name, version, apiTenantScope]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const columns: TableColumnsType<AgentUserItem> = useMemo(
    () => [
      {
        title: t("users_tab.column_user"),
        key: "user",
        render: (_: unknown, record) => (
          <Space direction="vertical" size={0}>
            <Text strong>{record.display_name ?? t("users_tab.unnamed")}</Text>
            <Tooltip title={record.user_id}>
              <Text code style={{ fontSize: 11 }}>
                {record.user_id.slice(0, 8)}…
              </Text>
            </Tooltip>
          </Space>
        ),
      },
      {
        title: t("users_tab.column_conversations"),
        dataIndex: "conversation_count",
        key: "conversations",
        width: 110,
      },
      {
        title: t("users_tab.column_runs"),
        key: "runs",
        width: 120,
        render: (_: unknown, record) => (
          <Space size={6}>
            <Text>{record.run_count}</Text>
            {record.error_count > 0 && (
              <Tooltip title={t("conversations_page.error_count", { count: record.error_count })}>
                <Space size={2} data-testid={`user-error-${record.user_id}`}>
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
        title: t("users_tab.column_tokens"),
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
              <Text style={{ fontSize: 12 }} data-testid={`user-tokens-${record.user_id}`}>
                {formatCompact(tk.total_tokens)}
              </Text>
            </Tooltip>
          );
        },
      },
      {
        title: t("users_tab.column_last_active"),
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
          <Users size={15} strokeWidth={1.5} />
          {t("users_tab.title")}
        </Space>
      }
      data-testid="users-tab-root"
    >
      {error !== null && (
        <Alert
          type="error"
          showIcon
          message={error}
          style={{ marginBottom: 12 }}
          data-testid="users-tab-error"
        />
      )}
      <Table<AgentUserItem>
        size="small"
        columns={columns}
        dataSource={data?.items ?? []}
        rowKey={(record) => record.user_id}
        loading={loading}
        pagination={{ total: data?.total ?? 0, showSizeChanger: false, pageSize: 50 }}
        onRow={(record) => ({
          onClick: () =>
            navigate(
              `/agents/${encodeURIComponent(name)}/${encodeURIComponent(version)}/users/${encodeURIComponent(record.user_id)}`,
              // Display name rides on router state — the detail page has
              // no single-user endpoint to re-resolve it (M2 scope).
              { state: { displayName: record.display_name ?? undefined } },
            ),
          style: { cursor: "pointer" },
        })}
        locale={{ emptyText: <Empty description={t("users_tab.empty")} /> }}
        data-testid="users-tab-table"
      />
    </Card>
  );
}
