/**
 * Agents list page — Stream H.1b PR 1 (read) + H.2 PR 2 (create).
 *
 * Hooks straight into the live ``/v1/agents`` endpoint and threads the
 * current :ref:`TenantScopeContext` through so a system_admin's
 * "All tenants" choice flips to the cross-tenant aggregate without
 * extra plumbing.
 *
 * H.2 PR 2 adds the **Create** button + ``CreateAgentModal`` (Monaco
 * YAML); on success the list refreshes and the new agent's detail page
 * loads.
 *
 * Product-grade pass: rows open the detail page, status is localised, the
 * owner (``created_by``) shows, the raw tenant column appears only in the
 * cross-tenant view, and a name search + status filter + per-row quick
 * actions (playground / edit / runs) make the list usable at scale.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  App,
  Button,
  Dropdown,
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
import {
  Activity,
  Ban,
  Bot,
  CircleCheck,
  Globe2,
  MoreHorizontal,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  Store,
  Trash2,
} from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";

import {
  listAgents,
  getAgent,
  disableAgent,
  enableAgent,
  type AgentRecord,
  type AgentList,
} from "../api/agents";
import { ApiError } from "../api/client";
import { useTenantScope } from "../tenant/TenantScopeContext";
import { useIsTenantSwitched } from "../tenant/useIsTenantSwitched";
import { CreateAgentModal } from "../components/CreateAgentModal";
import { DeleteAgentModal } from "../components/DeleteAgentModal";
import { PageHeader } from "../components/PageHeader";

const { Text } = Typography;

const STATUS_COLOR: Record<string, string> = {
  active: "success",
  draft: "warning",
  archived: "default",
  deleted: "error",
};

//: Statuses offered in the filter — the closed set the backend assigns.
const STATUS_OPTIONS = ["active", "draft", "archived", "deleted"] as const;

function agentPath(record: AgentRecord, tab: string): string {
  return `/agents/${encodeURIComponent(record.name)}/${encodeURIComponent(record.version)}/${tab}`;
}

function errMessage(err: unknown): string {
  return err instanceof ApiError
    ? `${err.code}: ${err.message}`
    : err instanceof Error
      ? err.message
      : "unknown error";
}

/** Per-row "..." action menu. The list payload doesn't carry the agent-level
 *  kill-switch flag (only ``GET /v1/agents/{name}/{version}`` does — see
 *  :func:`getAgent`), so disable/enable is lazily resolved from the detail
 *  endpoint the first time a row's menu opens; before that (and on a failed
 *  fetch) it defaults to showing "disable", the common case since most
 *  agents are enabled. Disable affects *all* versions of the name and
 *  bulk-cancels in-flight runs, so it's danger-confirmed; enable is cheap
 *  and reversible (re-disabling is one click away) so it fires directly. */
function AgentRowActions({
  record,
  onNavigate,
  onDeleteClick,
  onChanged,
}: {
  record: AgentRecord;
  onNavigate: (tab: string) => void;
  onDeleteClick: () => void;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const { message, modal } = App.useApp();
  // Track C W2 — 切入态只读:删除/禁用/启用是写操作,而且按 name 打到
  // 归属租户(不带 tenant_id)——切入态下会误伤自家同名 Agent,必须置灰。
  const isTenantSwitched = useIsTenantSwitched();
  const [disabled, setDisabled] = useState<boolean | null>(null);

  const loadDisabledState = useCallback(() => {
    getAgent(record.name, record.version).then(
      (detail) => setDisabled(detail.disabled ?? false),
      () => setDisabled(false),
    );
  }, [record.name, record.version]);

  const runEnable = useCallback(async () => {
    try {
      await enableAgent(record.name);
      message.success(t("agents_page.enable_success", { name: record.name }));
      onChanged();
    } catch (err) {
      message.error(t("agents_page.enable_failed", { error: errMessage(err) }));
    }
  }, [record.name, message, t, onChanged]);

  const confirmDisable = useCallback(() => {
    modal.confirm({
      title: t("agents_page.disable_confirm_title"),
      content: t("agents_page.disable_confirm_body", { name: record.name }),
      okButtonProps: { danger: true },
      okText: t("agents_page.action_disable"),
      cancelText: t("common.cancel"),
      onOk: async () => {
        try {
          const result = await disableAgent(record.name);
          message.success(
            t("agents_page.disable_success", {
              name: record.name,
              count: result.cancelled_runs ?? 0,
            }),
          );
          onChanged();
        } catch (err) {
          message.error(t("agents_page.disable_failed", { error: errMessage(err) }));
        }
      },
    });
  }, [modal, message, t, record.name, onChanged]);

  // A soft-deleted version can't be played/edited/disabled/re-deleted —
  // its menu keeps only the read-only run history entry.
  const isDeleted = record.status === "deleted";

  return (
    <Dropdown
      trigger={["click"]}
      onOpenChange={(nextOpen) => {
        if (nextOpen && !isDeleted && disabled === null) loadDisabledState();
      }}
      menu={{
        items: isDeleted
          ? [
              {
                key: "runs",
                icon: <Activity size={14} strokeWidth={1.5} />,
                label: t("agents_page.action_runs"),
              },
            ]
          : [
          {
            key: "playground",
            icon: <Play size={14} strokeWidth={1.5} />,
            label: t("agents_page.action_playground"),
          },
          {
            key: "manifest",
            icon: <Pencil size={14} strokeWidth={1.5} />,
            label: t("agents_page.action_edit"),
          },
          {
            key: "runs",
            icon: <Activity size={14} strokeWidth={1.5} />,
            label: t("agents_page.action_runs"),
          },
          disabled
            ? {
                key: "enable",
                icon: <CircleCheck size={14} strokeWidth={1.5} />,
                disabled: isTenantSwitched,
                label: (
                  <Tooltip
                    title={
                      isTenantSwitched ? t("common.tenant_switched_readonly") : undefined
                    }
                  >
                    {t("agents_page.action_enable")}
                  </Tooltip>
                ),
              }
            : {
                key: "disable",
                icon: <Ban size={14} strokeWidth={1.5} />,
                disabled: isTenantSwitched,
                label: (
                  <Tooltip
                    title={
                      isTenantSwitched ? t("common.tenant_switched_readonly") : undefined
                    }
                  >
                    {t("agents_page.action_disable")}
                  </Tooltip>
                ),
              },
          { type: "divider" as const },
          {
            key: "delete",
            danger: true,
            icon: <Trash2 size={14} strokeWidth={1.5} />,
            disabled: isTenantSwitched,
            label: (
              <Tooltip
                title={isTenantSwitched ? t("common.tenant_switched_readonly") : undefined}
              >
                {t("agents_page.action_delete")}
              </Tooltip>
            ),
          },
        ],
        onClick: ({ key }) => {
          if (key === "delete") {
            onDeleteClick();
            return;
          }
          if (key === "disable") {
            confirmDisable();
            return;
          }
          if (key === "enable") {
            void runEnable();
            return;
          }
          onNavigate(String(key));
        },
      }}
    >
      <Button
        type="text"
        size="small"
        aria-label={t("agents_page.column_actions")}
        icon={<MoreHorizontal size={16} strokeWidth={1.5} />}
        data-testid={`agent-row-actions-${record.name}`}
      />
    </Dropdown>
  );
}

export function AgentsList() {
  const { t } = useTranslation();
  const { scope, apiTenantScope } = useTenantScope();
  const navigate = useNavigate();
  const [data, setData] = useState<AgentList | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<AgentRecord | null>(null);
  const [nameFilter, setNameFilter] = useState<string>("");
  // "default" = every status EXCEPT deleted (the everyday view — a
  // soft-deleted version is history, not a working agent); "all" = truly
  // everything incl. deleted. Both fetch without a server-side status
  // param (the backend filter is single-status only) — "default" drops
  // deleted rows client-side below.
  const [statusFilter, setStatusFilter] = useState<string>("default");

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listAgents({
        tenantScope: apiTenantScope,
        name: nameFilter.trim() || undefined,
        status:
          statusFilter === "default" || statusFilter === "all"
            ? undefined
            : statusFilter,
      });
      setData(
        statusFilter === "default"
          ? { ...result, items: result.items.filter((a) => a.status !== "deleted") }
          : result,
      );
    } catch (err) {
      const message =
        err instanceof ApiError
          ? `${err.code}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "unknown error";
      setError(message);
    } finally {
      setLoading(false);
    }
  }, [apiTenantScope, nameFilter, statusFilter]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const isCrossTenant = data?.cross_tenant ?? false;

  const statusLabel = useCallback(
    (status: string) => t(`agents_page.status_${status}`, { defaultValue: status }),
    [t],
  );

  const columns: TableColumnsType<AgentRecord> = useMemo(() => {
    const cols: TableColumnsType<AgentRecord> = [
      {
        title: t("agents_page.column_name"),
        dataIndex: "name",
        key: "name",
        render: (name: string, record) => (
          <Space size={6}>
            <Bot size={14} strokeWidth={1.5} />
            <strong>{name}</strong>
            <Text type="secondary" style={{ fontSize: 12 }}>
              v{record.version}
            </Text>
          </Space>
        ),
      },
      {
        title: t("agents_page.column_status"),
        dataIndex: "status",
        key: "status",
        width: 120,
        render: (status: string) => (
          <Tag color={STATUS_COLOR[status] ?? "default"}>{statusLabel(status)}</Tag>
        ),
      },
      {
        title: t("agents_page.column_owner"),
        dataIndex: "created_by",
        key: "created_by",
        width: 200,
        render: (owner: string) =>
          owner ? (
            <Text style={{ fontSize: 13 }}>{owner}</Text>
          ) : (
            <Text type="secondary">—</Text>
          ),
      },
    ];

    // Raw tenant UUID is noise inside a single tenant (every row is the same);
    // only a system_admin's cross-tenant aggregate needs it to tell rows apart.
    if (isCrossTenant) {
      cols.push({
        title: t("agents_page.column_tenant"),
        dataIndex: "tenant_id",
        key: "tenant_id",
        width: 160,
        render: (tenantId: string) => (
          <Tooltip title={tenantId}>
            <Text code style={{ fontSize: 12 }}>
              {tenantId.slice(0, 8)}…
            </Text>
          </Tooltip>
        ),
      });
    }

    cols.push(
      {
        title: t("agents_page.column_created"),
        dataIndex: "created_at",
        key: "created_at",
        width: 190,
        render: (iso: string) => (
          <Text type="secondary" style={{ fontSize: 12 }}>
            {new Date(iso).toLocaleString()}
          </Text>
        ),
      },
      {
        title: t("agents_page.column_actions"),
        key: "actions",
        width: 64,
        align: "right",
        render: (_: unknown, record) => (
          // Stop the cell click bubbling to the row's navigate-to-overview.
          <span onClick={(e) => e.stopPropagation()}>
            <AgentRowActions
              record={record}
              onNavigate={(tab) => navigate(agentPath(record, tab))}
              onDeleteClick={() => setDeleteTarget(record)}
              onChanged={refresh}
            />
          </span>
        ),
      },
    );

    return cols;
  }, [t, statusLabel, isCrossTenant, navigate, refresh]);

  return (
    <div>
      <PageHeader
        icon={<Bot size={18} strokeWidth={1.5} />}
        title={t("agents_page.page_title")}
        actions={
          <>
            {isCrossTenant && (
              <Tag
                icon={<Globe2 size={12} strokeWidth={1.5} />}
                color="purple"
                data-testid="cross-tenant-banner"
              >
                {t("agents_page.cross_tenant_banner")}
              </Tag>
            )}
            <Input.Search
              allowClear
              placeholder={t("agents_page.search_placeholder")}
              aria-label={t("agents_page.search_placeholder")}
              data-testid="agents-search"
              onSearch={(value) => setNameFilter(value)}
              style={{ width: 200 }}
            />
            <Select<string>
              value={statusFilter}
              onChange={(v) => setStatusFilter(v)}
              style={{ width: 160 }}
              aria-label={t("agents_page.filter_status")}
              data-testid="agents-status-filter"
              options={[
                { value: "default", label: t("agents_page.filter_status_default") },
                { value: "all", label: t("agents_page.filter_status_all") },
                ...STATUS_OPTIONS.map((s) => ({ value: s, label: statusLabel(s) })),
              ]}
            />
            <button
              type="button"
              onClick={refresh}
              disabled={loading}
              aria-label={t("common.refresh")}
              data-testid="agents-refresh"
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
            <button
              type="button"
              onClick={() => navigate("/agent-template-marketplace")}
              data-testid="agents-from-template"
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
                cursor: "pointer",
              }}
            >
              <Store size={14} strokeWidth={1.5} />
              {t("agents_page.from_template")}
            </button>
            <button
              type="button"
              onClick={() => setCreateOpen(true)}
              data-testid="agents-create"
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                padding: "4px 10px",
                border: "1px solid var(--ew-color-brand-500)",
                borderRadius: 6,
                background: "var(--ew-color-brand-500)",
                color: "var(--ew-on-brand)",
                fontSize: 13,
                cursor: "pointer",
              }}
            >
              <Plus size={14} strokeWidth={1.75} />
              {t("agents_page.create")}
            </button>
          </>
        }
      />

      {error && (
        <Alert
          type="error"
          showIcon
          message={t("agents_page.failed_to_load")}
          description={error}
          style={{ marginBottom: 16 }}
          data-testid="agents-error"
        />
      )}

      <Table<AgentRecord>
        columns={columns}
        dataSource={data?.items ?? []}
        rowKey={(record) => `${record.tenant_id}/${record.id}`}
        loading={loading}
        onRow={(record) => ({
          onClick: () => navigate(agentPath(record, "overview")),
          style: { cursor: "pointer" },
        })}
        pagination={{
          total: data?.total ?? 0,
          showSizeChanger: false,
          pageSize: 50,
        }}
        locale={{
          emptyText: (
            <Empty
              description={
                scope === "*"
                  ? t("agents_page.empty_cross")
                  : t("agents_page.empty_home")
              }
            />
          ),
        }}
        data-testid="agents-table"
      />

      <CreateAgentModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(created) => {
          setCreateOpen(false);
          const { name, version } = created.record;
          navigate(
            `/agents/${encodeURIComponent(name)}/${encodeURIComponent(version)}/overview`,
          );
        }}
      />

      <DeleteAgentModal
        open={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        name={deleteTarget?.name ?? ""}
        version={deleteTarget?.version ?? ""}
        onDeleted={() => {
          setDeleteTarget(null);
          refresh();
        }}
      />
    </div>
  );
}
