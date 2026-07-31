/**
 * Triggers tab — Stream H.6 PR 2.
 *
 * Triggers bound to this exact agent version: ``GET /v1/triggers``
 * narrowed by ``agent_name`` + ``agent_version`` (a trigger row binds
 * to a (name, version) pair). Read-only list — create/enable/disable
 * live on the global /triggers page; the row click jumps there.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Badge, Card, Empty, Space, Table, Tag, Typography } from "antd";
import type { TableColumnsType } from "antd";
import { Zap } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";

import type { AgentDetailResponse } from "../../api/agents";
import { ApiError } from "../../api/client";
import { useTenantScope } from "../../tenant/TenantScopeContext";
import { listTriggers, type TriggerRecord } from "../../api/triggers";

const { Text } = Typography;

interface TriggersTabProps {
  detail: AgentDetailResponse;
}

export function TriggersTab({ detail }: TriggersTabProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { name, version } = detail.record;

  const [items, setItems] = useState<TriggerRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Stream N — the lists are scope-aware ("*" aggregates every tenant),
  // so the ambient scope rides along for a switched-in system_admin.
  const { apiTenantScope } = useTenantScope();

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listTriggers({ agentName: name, agentVersion: version, tenantScope: apiTenantScope });
      setItems(result.items);
    } catch (err) {
      setError(err instanceof ApiError ? `${err.code}: ${err.message}` : String(err));
    } finally {
      setLoading(false);
    }
  }, [name, version, apiTenantScope]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const columns: TableColumnsType<TriggerRecord> = useMemo(
    () => [
      {
        title: t("triggers_tab.col_name"),
        dataIndex: "name",
        key: "name",
        render: (triggerName: string) => <Text strong>{triggerName}</Text>,
      },
      {
        title: t("triggers_tab.col_kind"),
        dataIndex: "kind",
        key: "kind",
        width: 110,
        render: (kind: string) => <Tag bordered={false}>{kind}</Tag>,
      },
      {
        title: t("triggers_tab.col_enabled"),
        dataIndex: "enabled",
        key: "enabled",
        width: 120,
        render: (enabled: boolean) => (
          <Badge
            status={enabled ? "success" : "default"}
            text={enabled ? t("triggers_tab.enabled") : t("triggers_tab.disabled")}
          />
        ),
      },
      {
        title: t("triggers_tab.col_source"),
        dataIndex: "source",
        key: "source",
        width: 110,
        render: (source: string) => (
          <Text type="secondary" style={{ fontSize: 12 }}>
            {source}
          </Text>
        ),
      },
      {
        title: t("triggers_tab.col_created"),
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
    <Card
      title={
        <Space size={8}>
          <Zap size={15} strokeWidth={1.5} />
          {t("triggers_tab.title")}
        </Space>
      }
      extra={
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t("triggers_tab.manage_hint")}
        </Text>
      }
      data-testid="triggers-tab-root"
    >
      {error !== null && (
        <Alert
          type="error"
          showIcon
          message={error}
          style={{ marginBottom: 12 }}
          data-testid="triggers-tab-error"
        />
      )}
      <Table<TriggerRecord>
        size="small"
        columns={columns}
        dataSource={items}
        rowKey="id"
        loading={loading}
        pagination={false}
        onRow={() => ({
          onClick: () => navigate("/triggers"),
          style: { cursor: "pointer" },
        })}
        locale={{ emptyText: <Empty description={t("triggers_tab.empty")} /> }}
        data-testid="triggers-tab-table"
      />
    </Card>
  );
}
