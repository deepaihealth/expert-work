/**
 * Skills tab — Stream H.6 PR 2.
 *
 * Skills *authored by* this agent: ``GET /v1/skills`` narrowed by
 * ``created_by_agent_name`` (Mini-ADR H-11). The tenant-wide skill
 * library lives on the global /skills page — this tab is the agent's
 * provenance slice, not its "available skills" (that set is curated
 * tenant-wide and would mislead about ownership here).
 *
 * Versions are agent-name scoped (a skill is not bound to one agent
 * version), so the subtitle names just the agent.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Card, Empty, Space, Table, Tag, Typography } from "antd";
import type { TableColumnsType } from "antd";
import { Sparkles } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";

import type { AgentDetailResponse } from "../../api/agents";
import { ApiError } from "../../api/client";
import { useTenantScope } from "../../tenant/TenantScopeContext";
import { listSkills, type SkillRecord } from "../../api/skills";

const { Text } = Typography;

const STATUS_COLOR: Record<string, string> = {
  draft: "warning",
  active: "success",
  stale: "default",
  archived: "default",
};

interface SkillsTabProps {
  detail: AgentDetailResponse;
}

export function SkillsTab({ detail }: SkillsTabProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { name } = detail.record;

  const [items, setItems] = useState<SkillRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Stream N — the lists are scope-aware ("*" aggregates every tenant),
  // so the ambient scope rides along for a switched-in system_admin.
  const { apiTenantScope } = useTenantScope();

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listSkills({ createdByAgentName: name, tenantScope: apiTenantScope });
      setItems(result.items);
    } catch (err) {
      setError(err instanceof ApiError ? `${err.code}: ${err.message}` : String(err));
    } finally {
      setLoading(false);
    }
  }, [name, apiTenantScope]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const columns: TableColumnsType<SkillRecord> = useMemo(
    () => [
      {
        title: t("skills_tab.col_name"),
        dataIndex: "name",
        key: "name",
        render: (skillName: string) => <Text strong>{skillName}</Text>,
      },
      {
        title: t("skills_tab.col_status"),
        dataIndex: "status",
        key: "status",
        width: 120,
        render: (status: string) => (
          <Tag color={STATUS_COLOR[status] ?? "default"}>{status}</Tag>
        ),
      },
      {
        title: t("skills_tab.col_visibility"),
        dataIndex: "visibility",
        key: "visibility",
        width: 140,
        render: (visibility: string) => <Tag bordered={false}>{visibility}</Tag>,
      },
      {
        title: t("skills_tab.col_version"),
        dataIndex: "latest_version",
        key: "latest_version",
        width: 100,
        render: (v: number) => <Text className="mono">v{v}</Text>,
      },
      {
        title: t("skills_tab.col_created"),
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
          <Sparkles size={15} strokeWidth={1.5} />
          {t("skills_tab.title")}
        </Space>
      }
      extra={
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t("skills_tab.authored_hint", { agent: name })}
        </Text>
      }
      data-testid="skills-tab-root"
    >
      {error !== null && (
        <Alert
          type="error"
          showIcon
          message={error}
          style={{ marginBottom: 12 }}
          data-testid="skills-tab-error"
        />
      )}
      <Table<SkillRecord>
        size="small"
        columns={columns}
        dataSource={items}
        rowKey="id"
        loading={loading}
        pagination={false}
        onRow={(record) => ({
          onClick: () => navigate(`/skills/${encodeURIComponent(record.id)}`),
          style: { cursor: "pointer" },
        })}
        locale={{ emptyText: <Empty description={t("skills_tab.empty")} /> }}
        data-testid="skills-tab-table"
      />
    </Card>
  );
}
