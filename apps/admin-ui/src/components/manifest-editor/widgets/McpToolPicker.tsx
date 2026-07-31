/**
 * McpToolPicker — selects MCP servers + (optionally) per-server tools.
 *
 * Selecting a server IS enabling MCP — there is no separate enable checkbox.
 * Per server, the tool scope is explicit: "all tools" (default) or "specific"
 * (then pick tools). Built for scale: a server search box, and per server a
 * tool search + select-all/clear + a height-capped scroll list.
 *
 * Controlled via a single ``onChange(servers, allowTools)`` so server and tool
 * edits land in one manifest patch (no stale-read double write).
 *
 *   source = "available" (default) — the tenant's opted-in/custom servers.
 *   source = "catalog"             — published platform connectors (templates).
 */
import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Input,
  Modal,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { Settings } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  listAvailableMcpServers,
  listMcpServerTools,
  type McpTool,
} from "../../../api/mcp-servers";
import {
  listPlatformCatalog,
  listCatalogTools,
} from "../../../api/mcp-catalog";
import { concreteTenantScope, useTenantScope } from "../../../tenant/TenantScopeContext";

const { Text } = Typography;

export type McpPickerSource = "available" | "catalog";

interface McpToolPickerProps {
  servers: string[];
  allowTools: string[];
  onChange: (servers: string[], allowTools: string[]) => void;
  source?: McpPickerSource;
}

interface ServerRow {
  name: string;
  label: string;
  tagText: string;
  tagColor: string;
  toolKey: string;
}

type ToolState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "loaded"; tools: McpTool[] }
  | { kind: "error" };

export function McpToolPicker({
  servers,
  allowTools,
  onChange,
  source = "available",
}: McpToolPickerProps) {
  const { t } = useTranslation();
  // Cross-tenant W3 — tenant-server list rides the ambient scope; the
  // per-server tools probe is a detail read (concrete UUID only).
  const { apiTenantScope } = useTenantScope();

  const [rows, setRows] = useState<ServerRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toolStates, setToolStates] = useState<Record<string, ToolState>>({});
  const [serverQuery, setServerQuery] = useState("");
  const [toolQuery, setToolQuery] = useState<Record<string, string>>({});
  // The server whose tool-selection sub-modal is open (null = closed).
  const [modalServer, setModalServer] = useState<ServerRow | null>(null);

  // ── Load selectable servers (source-dependent) ───────────────────────────
  useEffect(() => {
    let alive = true;
    setLoading(true);
    const load: Promise<ServerRow[]> =
      source === "catalog"
        ? listPlatformCatalog().then((entries) =>
            entries
              .filter((e) => e.enabled)
              .map((e) => ({
                name: e.name,
                label: e.display_name || e.name,
                tagText: t("agent_form.mcp_source_platform"),
                tagColor: "blue",
                toolKey: e.id,
              })),
          )
        : listAvailableMcpServers(apiTenantScope).then((data) =>
            data.map((s) => ({
              name: s.name,
              label: s.name,
              tagText:
                s.source === "platform"
                  ? t("agent_form.mcp_source_platform")
                  : t("agent_form.mcp_source_tenant"),
              tagColor: s.source === "platform" ? "blue" : "green",
              toolKey: s.name,
            })),
          );
    load.then(
      (data) => {
        if (!alive) return;
        setRows(data);
        setLoading(false);
      },
      (err: unknown) => {
        if (!alive) return;
        setError(err instanceof Error ? err.message : "unknown error");
        setLoading(false);
      },
    );
    return () => {
      alive = false;
    };
  }, [source, t, apiTenantScope]);

  // ── Per-server tool fetch ────────────────────────────────────────────────
  const fetchTools = useCallback(
    (row: ServerRow) => {
      const current = toolStates[row.name];
      if (current?.kind === "loaded" || current?.kind === "loading") return;
      setToolStates((prev) => ({ ...prev, [row.name]: { kind: "loading" } }));
      const req: Promise<McpTool[]> =
        source === "catalog"
          ? listCatalogTools(row.toolKey).then((res) =>
              res.status === "ok"
                ? res.tools
                    .filter((x) => !x.disabled)
                    .map((x) => ({ name: x.name, description: x.description }))
                : Promise.reject(new Error(res.error ?? "unreachable")),
            )
          : listMcpServerTools(row.toolKey, concreteTenantScope(apiTenantScope));
      req.then(
        (tools) =>
          setToolStates((prev) => ({
            ...prev,
            [row.name]: { kind: "loaded", tools },
          })),
        () =>
          setToolStates((prev) => ({ ...prev, [row.name]: { kind: "error" } })),
      );
    },
    [toolStates, source, apiTenantScope],
  );

  // Pre-load tools for already-selected servers (from the manifest) so their
  // scope derives correctly and the tool list is ready without a manual expand.
  // ``fetchTools`` self-guards against duplicate loads.
  useEffect(() => {
    for (const row of rows) {
      if (servers.includes(row.name)) fetchTools(row);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, servers]);

  const toolNamesOf = (name: string): string[] => {
    const st = toolStates[name];
    return st?.kind === "loaded" ? st.tools.map((x) => x.name) : [];
  };

  // ── Mutations (always one combined onChange) ─────────────────────────────
  const toggleServer = (row: ServerRow, on: boolean): void => {
    if (on) {
      onChange([...servers, row.name], allowTools);
      fetchTools(row);
    } else {
      const names = new Set(toolNamesOf(row.name));
      onChange(
        servers.filter((s) => s !== row.name),
        allowTools.filter((a) => !names.has(a)),
      );
    }
  };

  const selectedCountOf = (name: string): number => {
    const names = new Set(toolNamesOf(name));
    return allowTools.filter((a) => names.has(a)).length;
  };

  const toggleTool = (toolName: string, on: boolean): void =>
    onChange(
      servers,
      on ? [...allowTools, toolName] : allowTools.filter((a) => a !== toolName),
    );

  const selectAllTools = (toolList: McpTool[]): void =>
    onChange(
      servers,
      Array.from(new Set([...allowTools, ...toolList.map((x) => x.name)])),
    );

  const clearTools = (toolList: McpTool[]): void => {
    const names = new Set(toolList.map((x) => x.name));
    onChange(
      servers,
      allowTools.filter((a) => !names.has(a)),
    );
  };

  // ── Loading / error / empty ──────────────────────────────────────────────
  if (loading) {
    return (
      <div style={{ padding: "8px 0" }}>
        <Space size={4}>
          <Spin size="small" />
          <span>{t("agent_form.mcp_servers_loading")}</span>
        </Space>
      </div>
    );
  }
  if (error !== null) {
    return (
      <Alert
        type="error"
        showIcon
        message={t("agent_form.mcp_servers_load_failed")}
        description={error}
        style={{ marginBottom: 8 }}
      />
    );
  }
  if (rows.length === 0) {
    return (
      <div
        data-testid="af-mcp-empty"
        style={{
          color: "var(--ew-text-tertiary, #666)",
          fontSize: 13,
          padding: "4px 0",
        }}
      >
        {source === "catalog"
          ? t("agent_form.mcp_no_servers_catalog")
          : t("agent_form.mcp_no_servers_available")}
      </div>
    );
  }

  // ── Render ───────────────────────────────────────────────────────────────
  const checked = new Set(servers);
  const q = serverQuery.trim().toLowerCase();
  const visibleRows = q
    ? rows.filter(
        (r) =>
          r.name.toLowerCase().includes(q) || r.label.toLowerCase().includes(q),
      )
    : rows;

  return (
    <div>
      {rows.length > 6 && (
        <Input.Search
          allowClear
          size="small"
          data-testid="af-mcp-server-search"
          placeholder={t("agent_form.mcp_server_search")}
          value={serverQuery}
          onChange={(e) => setServerQuery(e.target.value)}
          style={{ marginBottom: 8, maxWidth: 280 }}
        />
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {visibleRows.map((row) => {
          const isChecked = checked.has(row.name);
          const count = selectedCountOf(row.name);
          return (
            <div key={row.name}>
              <Space size={6} align="center">
                <Checkbox
                  data-testid={`af-mcp-server-${row.name}`}
                  checked={isChecked}
                  onChange={(e) => toggleServer(row, e.target.checked)}
                >
                  <span style={{ fontWeight: 500 }}>{row.label}</span>
                </Checkbox>
                <Tag color={row.tagColor} style={{ fontSize: 11 }}>
                  {row.tagText}
                </Tag>

                {isChecked && (
                  <>
                    <Tooltip title={t("agent_form.mcp_choose_tools")}>
                      <Button
                        type="text"
                        size="small"
                        icon={<Settings size={14} strokeWidth={1.75} />}
                        data-testid={`af-mcp-choose-${row.name}`}
                        aria-label={t("agent_form.mcp_choose_tools")}
                        onClick={() => {
                          fetchTools(row);
                          setModalServer(row);
                        }}
                      />
                    </Tooltip>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {count === 0
                        ? t("agent_form.mcp_all_tools")
                        : t("agent_form.mcp_tools_count", { count })}
                    </Text>
                  </>
                )}
              </Space>
            </div>
          );
        })}
      </div>

      <Modal
        open={modalServer !== null}
        title={
          modalServer
            ? `${modalServer.label} · ${t("agent_form.mcp_choose_tools")}`
            : ""
        }
        okText={t("agent_form.mcp_done")}
        cancelButtonProps={{ style: { display: "none" } }}
        onOk={() => setModalServer(null)}
        onCancel={() => setModalServer(null)}
        destroyOnHidden
        data-testid="af-mcp-tool-modal"
      >
        {modalServer && (
          <div data-testid={`af-mcp-tools-${modalServer.name}`}>
            {renderToolPicker(modalServer)}
          </div>
        )}
      </Modal>
    </div>
  );

  // ── Per-server tool picker (rendered inside the sub-modal) ────────────────
  function renderToolPicker(row: ServerRow) {
    const state = toolStates[row.name] ?? { kind: "idle" };
    if (state.kind === "idle" || state.kind === "loading") {
      return (
        <Space size={4}>
          <Spin size="small" />
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t("agent_form.mcp_tools_loading")}
          </Text>
        </Space>
      );
    }
    if (state.kind === "error") {
      return (
        <Alert
          type="warning"
          showIcon
          message={t("agent_form.mcp_tools_unreachable")}
          style={{ fontSize: 12 }}
        />
      );
    }
    const tools = state.tools;
    const tq = (toolQuery[row.name] ?? "").trim().toLowerCase();
    const shown = tq
      ? tools.filter((x) => x.name.toLowerCase().includes(tq))
      : tools;
    const selectedCount = tools.filter((x) =>
      allowTools.includes(x.name),
    ).length;

    return (
      <div>
        <Space size={8} style={{ marginBottom: 6, flexWrap: "wrap" }}>
          <Input.Search
            allowClear
            size="small"
            data-testid={`af-mcp-tool-search-${row.name}`}
            placeholder={t("agent_form.mcp_tool_search")}
            value={toolQuery[row.name] ?? ""}
            onChange={(e) =>
              setToolQuery((prev) => ({ ...prev, [row.name]: e.target.value }))
            }
            style={{ width: 180 }}
          />
          <Button
            size="small"
            data-testid={`af-mcp-select-all-${row.name}`}
            onClick={() => selectAllTools(tools)}
          >
            {t("agent_form.mcp_select_all")}
          </Button>
          <Button
            size="small"
            data-testid={`af-mcp-clear-${row.name}`}
            onClick={() => clearTools(tools)}
          >
            {t("agent_form.mcp_clear")}
          </Button>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t("agent_form.mcp_selected_count", { count: selectedCount })}
          </Text>
        </Space>
        <div
          style={{
            maxHeight: "50vh",
            overflowY: "auto",
            display: "flex",
            flexDirection: "column",
            gap: 4,
          }}
        >
          {shown.length === 0 ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              —
            </Text>
          ) : (
            shown.map((tool) => (
              <Tooltip
                key={tool.name}
                title={tool.description || undefined}
                placement="right"
              >
                <Checkbox
                  data-testid={`af-mcp-tool-${tool.name}`}
                  checked={allowTools.includes(tool.name)}
                  onChange={(e) => toggleTool(tool.name, e.target.checked)}
                >
                  <Text style={{ fontSize: 13 }}>{tool.name}</Text>
                </Checkbox>
              </Tooltip>
            ))
          )}
        </div>
      </div>
    );
  }
}
