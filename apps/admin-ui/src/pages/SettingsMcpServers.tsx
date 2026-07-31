/**
 * Settings — MCP 服务器统一列表。
 *
 * 一张表合并两个不相交来源:
 *   - 自定义服务器(`tenant_mcp_server`,`listMcpServers()` 全明细,可编辑)。
 *   - 平台服务器(租户已启用的 allowlist,`/available` 里 source="platform",
 *     经后端增强携带 display_name/auth_type/catalog_id;只读、平台托管)。
 *
 * 平台行:bearer/none 可"测试"+"移出";oauth2 挂"需你授权 →"跳授权页、不给测试
 * (后端探测返回 409)。自定义行:测试 / 编辑 / 运行开关(运行中↔已停用)/ 删除。
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  Alert,
  App,
  Button,
  Popconfirm,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { Plug } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import {
  deleteMcpServer,
  listAvailableMcpServers,
  listMcpServerTools,
  listMcpServers,
  updateMcpServer,
  type AvailableMcpServer,
  type McpServer,
  type McpTool,
} from "../api/mcp-servers";
import { disablePlatformServer } from "../api/mcp-catalog";
import { ApiError } from "../api/client";
import { concreteTenantScope, useTenantScope } from "../tenant/TenantScopeContext";
import { CreateMcpServerDrawer } from "../components/CreateMcpServerDrawer";
import { AddMcpServerDrawer } from "../components/mcp_catalog/AddMcpServerDrawer";
import { PageHeader } from "../components/PageHeader";
import { buildUnifiedRows, type UnifiedRow } from "./mcpServerRows";

type ProbeState =
  | { kind: "idle" }
  | { kind: "testing" }
  | { kind: "connected"; count: number; tools: McpTool[] }
  | { kind: "unreachable" };

function errMsg(err: unknown): string {
  if (err instanceof ApiError) return `${err.code}: ${err.message}`;
  if (err instanceof Error) return err.message;
  return "unknown error";
}

export function SettingsMcpServers() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const navigate = useNavigate();
  // Cross-tenant W3 — lists ride the ambient scope; the per-server tools
  // probe is a detail read (concrete UUID only, "*" 400s).
  const { apiTenantScope } = useTenantScope();

  const [rows, setRows] = useState<UnifiedRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const [addOpen, setAddOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [editing, setEditing] = useState<McpServer | null>(null);

  const [probes, setProbes] = useState<Record<string, ProbeState>>({});
  // Mirror the latest probe state in a ref so the stable ``probe`` callback
  // reads current status. antd memoizes the action cell, which would
  // otherwise freeze a stale ``probes`` closure on the Test button's onClick
  // and make a forced re-probe silently no-op on the second click.
  const probesRef = useRef(probes);
  probesRef.current = probes;

  const reload = useCallback(() => {
    setLoading(true);
    setError(null);
    // The custom-server list is the primary content; the platform allowlist
    // (``/available``) is supplementary — degrade to no platform rows if it
    // fails rather than blanking the whole page on a partial outage.
    Promise.all([
      listMcpServers(apiTenantScope),
      listAvailableMcpServers(apiTenantScope).catch(() => [] as AvailableMcpServer[]),
    ]).then(
      ([servers, available]) => {
        setRows(buildUnifiedRows(servers, available));
        setLoading(false);
      },
      (err: unknown) => {
        setError(err instanceof Error ? err.message : "unknown error");
        setLoading(false);
      },
    );
  }, [apiTenantScope]);

  useEffect(() => {
    reload();
  }, [reload]);

  const probe = useCallback(
    // `key` is the source-qualified `UnifiedRow.key` (probes map key, avoids
    // tenant/platform name collisions); `name` is the bare server name sent
    // to the API.
    async (key: string, name: string, opts?: { force?: boolean }) => {
      const current = probesRef.current[key];
      // Always skip while a probe is in flight (prevents double-fire). A
      // cached ``connected`` result is skipped only for passive callers
      // (row-expand); the explicit Test button passes ``force`` so a click
      // always re-probes.
      if (current?.kind === "testing") return;
      if (!opts?.force && current?.kind === "connected") return;
      setProbes((prev) => ({ ...prev, [key]: { kind: "testing" } }));
      try {
        const tools = await listMcpServerTools(name, concreteTenantScope(apiTenantScope));
        setProbes((prev) => ({
          ...prev,
          [key]: { kind: "connected", count: tools.length, tools },
        }));
      } catch {
        setProbes((prev) => ({ ...prev, [key]: { kind: "unreachable" } }));
      }
    },
    [apiTenantScope],
  );

  const handleToggle = useCallback(
    async (row: McpServer) => {
      try {
        await updateMcpServer(row.name, { enabled: !row.enabled });
        reload();
      } catch (err) {
        message.error(errMsg(err));
      }
    },
    [message, reload],
  );

  const handleDelete = useCallback(
    async (name: string) => {
      try {
        await deleteMcpServer(name);
        reload();
      } catch (err) {
        message.error(errMsg(err));
      }
    },
    [message, reload],
  );

  const handleRemovePlatform = useCallback(
    async (catalogId: string | null) => {
      if (catalogId === null) {
        message.error(t("mcp_servers.remove_unavailable"));
        return;
      }
      try {
        await disablePlatformServer(catalogId);
        reload();
      } catch (err) {
        message.error(errMsg(err));
      }
    },
    [message, reload, t],
  );

  const openCreate = useCallback(() => setAddOpen(true), []);
  const openEdit = useCallback((row: McpServer) => {
    setEditing(row);
    setEditOpen(true);
  }, []);
  const closeEdit = useCallback(() => {
    setEditOpen(false);
    setEditing(null);
  }, []);

  const renderProbeStatus = useCallback(
    (key: string, staticTag: ReactNode) => {
      const s = probes[key] ?? { kind: "idle" };
      if (s.kind === "idle") return staticTag;
      if (s.kind === "testing") {
        return (
          <Space size={4}>
            <Spin size="small" />
            <span>{t("mcp_servers.testing")}</span>
          </Space>
        );
      }
      if (s.kind === "connected") {
        return <Tag color="green">{t("mcp_servers.connected", { count: s.count })}</Tag>;
      }
      return <Tag color="red">{t("mcp_servers.unreachable")}</Tag>;
    },
    [probes, t],
  );

  const columns: ColumnsType<UnifiedRow> = [
    {
      title: t("mcp_servers.col_name"),
      key: "name",
      render: (_: unknown, row: UnifiedRow) => {
        const name = row.source === "tenant" ? row.server.name : row.displayName;
        const sourceTag =
          row.source === "tenant" ? (
            <Tag>{t("mcp_servers.source_custom")}</Tag>
          ) : (
            <Tag color="blue">{t("mcp_servers.source_platform")}</Tag>
          );
        return (
          <Space size={6}>
            <Typography.Text strong>{name}</Typography.Text>
            {sourceTag}
          </Space>
        );
      },
    },
    {
      title: t("mcp_servers.col_transport"),
      key: "transport",
      render: (_: unknown, row: UnifiedRow) =>
        row.source === "tenant" ? (
          <Tag>{row.server.transport === "streamable_http" ? "Streamable HTTP" : "SSE"}</Tag>
        ) : (
          <span style={{ color: "var(--ew-text-tertiary, #666)" }}>—</span>
        ),
    },
    {
      title: t("mcp_servers.col_url"),
      key: "url",
      ellipsis: true,
      render: (_: unknown, row: UnifiedRow) =>
        row.source === "tenant" ? (
          <Tooltip title={row.server.url}>
            <Typography.Text ellipsis style={{ maxWidth: 200 }}>
              {row.server.url}
            </Typography.Text>
          </Tooltip>
        ) : (
          <Typography.Text type="secondary">{t("mcp_servers.platform_hosted")}</Typography.Text>
        ),
    },
    {
      title: t("mcp_servers.col_auth"),
      key: "auth",
      render: (_: unknown, row: UnifiedRow) => {
        const auth = row.source === "tenant" ? row.server.auth_type : row.authType;
        const color = auth === "bearer" ? "blue" : auth === "oauth2" ? "geekblue" : "default";
        const label = auth === "bearer" ? "Bearer" : auth === "oauth2" ? "OAuth" : "None";
        return <Tag color={color}>{label}</Tag>;
      },
    },
    {
      title: t("mcp_servers.col_status"),
      key: "status",
      render: (_: unknown, row: UnifiedRow) => {
        if (row.source === "platform") {
          return renderProbeStatus(
            row.key,
            <Tag color="green">{t("mcp_servers.status_enabled_platform")}</Tag>,
          );
        }
        return renderProbeStatus(
          row.key,
          <Tag color={row.server.enabled ? "green" : "default"}>
            {row.server.enabled
              ? t("mcp_servers.status_enabled")
              : t("mcp_servers.status_disabled")}
          </Tag>,
        );
      },
    },
    {
      title: t("mcp_servers.col_tools"),
      key: "tools",
      render: (_: unknown, row: UnifiedRow) => {
        const s = probes[row.key];
        if (s?.kind === "connected") return <span>{s.count}</span>;
        return <span style={{ color: "var(--ew-text-tertiary, #666)" }}>—</span>;
      },
    },
    {
      title: t("mcp_servers.col_actions"),
      key: "actions",
      render: (_: unknown, row: UnifiedRow) => {
        if (row.source === "platform") {
          if (row.catalogId === null) {
            // Degraded row: the catalog entry was deleted while the tenant
            // still had it allowlisted. Test would 404 (nothing to probe)
            // and Remove has no catalog id to act on — show neither as a
            // live affordance; Remove stays visible but disabled so the
            // stale entry is still explainable, not silently broken.
            return (
              <Space size={4}>
                <Tooltip title={t("mcp_servers.remove_unavailable")}>
                  <span>
                    <Button size="small" disabled data-testid={`ms-remove-${row.name}`}>
                      {t("mcp_servers.remove")}
                    </Button>
                  </span>
                </Tooltip>
              </Space>
            );
          }
          const isOauth = row.authType === "oauth2";
          return (
            <Space size={4}>
              {isOauth ? (
                <Button
                  size="small"
                  type="link"
                  data-testid={`ms-authorize-${row.name}`}
                  onClick={() => navigate("/settings/mcp-oauth")}
                >
                  {t("mcp_servers.needs_authorize")}
                </Button>
              ) : (
                <Button
                  size="small"
                  data-testid={`ms-test-${row.name}`}
                  loading={probes[row.key]?.kind === "testing"}
                  onClick={() => void probe(row.key, row.name, { force: true })}
                >
                  {t("mcp_servers.test")}
                </Button>
              )}
              <Popconfirm
                title={t("mcp_servers.remove_confirm", { name: row.displayName })}
                onConfirm={() => void handleRemovePlatform(row.catalogId)}
              >
                <Button size="small" data-testid={`ms-remove-${row.name}`}>
                  {t("mcp_servers.remove")}
                </Button>
              </Popconfirm>
            </Space>
          );
        }
        const s = row.server;
        return (
          <Space size={4}>
            <Button
              size="small"
              data-testid={`ms-test-${s.name}`}
              loading={probes[row.key]?.kind === "testing"}
              onClick={() => void probe(row.key, s.name, { force: true })}
            >
              {t("mcp_servers.test")}
            </Button>
            <Button size="small" data-testid={`ms-edit-${s.name}`} onClick={() => openEdit(s)}>
              {t("mcp_servers.edit")}
            </Button>
            <Button
              size="small"
              data-testid={`ms-toggle-${s.name}`}
              onClick={() => void handleToggle(s)}
            >
              {s.enabled ? t("mcp_servers.act_stop") : t("mcp_servers.act_run")}
            </Button>
            <Popconfirm
              title={t("mcp_servers.delete_confirm", { name: s.name })}
              onConfirm={() => void handleDelete(s.name)}
            >
              <Button size="small" danger data-testid={`ms-delete-${s.name}`}>
                {t("mcp_servers.delete")}
              </Button>
            </Popconfirm>
          </Space>
        );
      },
    },
  ];

  const expandedRowRender = useCallback(
    (row: UnifiedRow) => {
      const name = row.source === "tenant" ? row.server.name : row.name;
      const s = probes[row.key] ?? { kind: "idle" };
      if (s.kind === "idle" || s.kind === "testing") {
        return (
          <div style={{ padding: "8px 0" }}>
            <Space size={4}>
              <Spin size="small" />
              <span>{t("mcp_servers.tools_loading")}</span>
            </Space>
          </div>
        );
      }
      if (s.kind === "unreachable") {
        return (
          <div style={{ padding: "8px 0" }}>
            <Tag color="red">{t("mcp_servers.unreachable")}</Tag>
          </div>
        );
      }
      if (s.tools.length === 0) {
        return (
          <div
            style={{ padding: "8px 0", color: "var(--ew-text-tertiary, #666)" }}
            data-testid={`ms-tools-${name}`}
          >
            {t("mcp_servers.no_tools")}
          </div>
        );
      }
      return (
        <div style={{ padding: "8px 0" }} data-testid={`ms-tools-${name}`}>
          <Space size={[4, 8]} wrap>
            {s.tools.map((tool) => (
              <Tooltip key={tool.name} title={tool.description || undefined}>
                <Tag style={{ cursor: "default" }}>{tool.name}</Tag>
              </Tooltip>
            ))}
          </Space>
        </div>
      );
    },
    [probes, t],
  );

  const emptyText = (
    <div style={{ textAlign: "center", padding: "32px 0" }} data-testid="ms-empty">
      <Plug size={32} strokeWidth={1.25} style={{ opacity: 0.35, marginBottom: 8 }} />
      <div style={{ fontWeight: 600, marginBottom: 4 }}>{t("mcp_servers.empty_title")}</div>
      <div
        style={{
          color: "var(--ew-text-tertiary, #666)",
          marginBottom: 16,
          maxWidth: 360,
          margin: "0 auto 16px",
        }}
      >
        {t("mcp_servers.empty_hint")}
      </div>
      <Button type="primary" onClick={openCreate}>
        {t("mcp_servers.add")}
      </Button>
    </div>
  );

  return (
    <div data-testid="ms-root">
      <PageHeader
        icon={<Plug size={18} strokeWidth={1.5} />}
        title={t("mcp_servers.page_title")}
        subtitle={t("mcp_servers.subtitle")}
        actions={
          <Button type="primary" data-testid="ms-add" onClick={openCreate}>
            {t("mcp_servers.add")}
          </Button>
        }
      />

      {error !== null && (
        <Alert
          type="error"
          showIcon
          data-testid="ms-error"
          message={t("mcp_servers.failed_to_load")}
          description={error}
          style={{ marginBottom: 16 }}
        />
      )}

      <Table<UnifiedRow>
        data-testid="ms-table"
        rowKey="key"
        loading={loading}
        dataSource={rows}
        pagination={false}
        locale={{ emptyText }}
        columns={columns}
        expandable={{
          expandedRowRender,
          // oauth2 platform rows have no working probe endpoint (backend
          // 409s) and intentionally show no Test button. `rowExpandable`
          // removes the chevron (antd renders it visibility:hidden with no
          // click affordance); `onExpand` is also guarded as a second line
          // of defense, since antd still wires the icon's onClick
          // regardless of `rowExpandable`. Together these ensure probe()
          // can never fire for an oauth2 platform row.
          rowExpandable: (row) => !(row.source === "platform" && row.authType === "oauth2"),
          onExpand: (expanded, row) => {
            if (!expanded || (row.source === "platform" && row.authType === "oauth2")) return;
            void probe(row.key, row.source === "tenant" ? row.server.name : row.name);
          },
        }}
      />

      <AddMcpServerDrawer
        open={addOpen}
        onClose={() => setAddOpen(false)}
        onSaved={() => {
          setAddOpen(false);
          reload();
        }}
        onEnabledChange={reload}
      />

      <CreateMcpServerDrawer
        open={editOpen}
        onClose={closeEdit}
        onSaved={() => {
          closeEdit();
          reload();
        }}
        editing={editing}
      />
    </div>
  );
}
