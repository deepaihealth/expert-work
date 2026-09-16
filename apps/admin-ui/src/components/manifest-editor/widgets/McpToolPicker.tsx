/**
 * McpToolPicker — selects MCP servers + (optionally) per-server tools.
 *
 * Selecting a server IS enabling MCP — there is no separate enable checkbox.
 * Per server, the tool scope is explicit: "all tools" (default) or "specific"
 * (then pick tools). Built for scale: a server search box, and per server a
 * tool search + select-all/clear + a height-capped scroll list.
 *
 * Controlled via a single ``onChange(servers, allowTools, argBindings)`` so
 * server, tool and binding edits land in one manifest patch (no stale-read
 * double write).
 *
 *   source = "available" (default) — the tenant's opted-in/custom servers.
 *   source = "catalog"             — published platform connectors (templates).
 *
 * B-61 — per-parameter bindings. Inside the tool sub-modal, every tool that is
 * currently in the agent's scope gets a "bound parameters" disclosure: one row
 * per parameter of the tool's own ``input_schema``, each a choice between
 * "auto (model fills it in)" and one of the agent's DECLARED prompt variables.
 * Free-text variable names are deliberately impossible here — a name nothing
 * declares is exactly how a binding silently matches nothing.
 */
import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  App,
  Button,
  Checkbox,
  Input,
  Modal,
  Select,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { Settings } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { ArgBindingFields } from "../form_model";
import {
  AUTO,
  bindingInScope,
  paramsOf,
  toolInScope,
  type ToolParam,
} from "./mcp_arg_bindings";

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
  /** B-61 — the manifest's ``tools[].arg_bindings``, verbatim. */
  argBindings?: ArgBindingFields[];
  /** Names declared in ``system_prompt.variables`` — the ONLY things a
   *  parameter may be bound to. */
  promptVariables?: string[];
  onChange: (
    servers: string[],
    allowTools: string[],
    argBindings: ArgBindingFields[],
  ) => void;
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
  argBindings = [],
  promptVariables = [],
  onChange,
  source = "available",
}: McpToolPickerProps) {
  const { t } = useTranslation();
  // The drop-a-binding confirm rides App.useApp()'s modal, not the static
  // ``Modal.confirm`` (project convention — the static one does not render
  // under test).
  const { modal } = App.useApp();
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
  // "only selected" filter inside the tool sub-modal; reset on every open.
  const [onlySelected, setOnlySelected] = useState(false);
  // Tool names whose binding editor is expanded; reset on every modal open.
  // Pure presentation — the bindings themselves live in props.
  const [expandedBindings, setExpandedBindings] = useState<string[]>([]);

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
        : // ``/available`` rejects the "*" aggregate with 400 (review C-2) —
          // collapse it to the home tenant like the tools probe below.
          listAvailableMcpServers(concreteTenantScope(apiTenantScope)).then((data) =>
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
                    .map((x) => ({
                      name: x.name,
                      description: x.description,
                      // B-61 — carry the schema through; without it the binding
                      // editor has no parameters to offer for catalog servers.
                      input_schema: x.input_schema,
                    }))
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
  //
  // Every edit funnels through ``emit``: it drops the bindings the new
  // server/tool selection has put out of scope (see ``bindingInScope`` — the
  // manifest validator rejects those outright) and, when that drops anything,
  // asks first and names what is going.
  //
  // That confirm is not there to second-guess the operator: turning MCP off
  // SHOULD take its bindings with it. It is there because bindings had no
  // representation on this page until now, so "it deleted something I could
  // not see" was the only way that could read.
  //
  // DO NOT narrow this to ``nextServers.length === 0`` ("only when MCP is being
  // turned off"). That is the literal wording of the requirement but not its
  // reason: the reason is that something invisible was about to go silently,
  // and that is equally true of unchecking one bound tool, or of checking the
  // first tool while the agent was on "all tools" (which pushes every OTHER
  // tool's bindings out of scope — the least obvious path of the three).
  // Narrowing it puts back exactly the silence the requirement exists to
  // remove. Reviewed and kept deliberately (2026-09-16).
  const emit = (
    nextServers: string[],
    nextAllow: string[],
    nextBindings: ArgBindingFields[],
  ): void => {
    const kept = nextBindings.filter((b) =>
      bindingInScope(b, nextServers, nextAllow),
    );
    const dropped = nextBindings.filter(
      (b) => !bindingInScope(b, nextServers, nextAllow),
    );
    if (dropped.length === 0) {
      onChange(nextServers, nextAllow, kept);
      return;
    }
    // 数的是**参数**,不是 arg_bindings 条目 —— 一个条目可以绑好几个参数,而用户
    // 要衡量的是「有几个参数要回到模型自己填」。标题的 N 与下面的列表都从这一份
    // 摊平结果来(带上 tool/server,列表才用得上它),所以「说 3 条、列 4 行」不是
    // 靠两段代码碰巧一样,而是结构上产生不出来。
    const droppedParams = dropped.flatMap((b) =>
      Object.entries(b.args).map(([param, variable]) => ({
        server: b.server,
        tool: b.tool,
        param,
        variable,
      })),
    );
    modal.confirm({
      title: t("agent_form.mcp_bind_drop_title", {
        count: droppedParams.length,
      }),
      okText: t("agent_form.mcp_bind_drop_ok"),
      cancelText: t("agent_form.mcp_bind_drop_cancel"),
      content: (
        <div>
          <ul style={{ margin: "0 0 8px", paddingLeft: 18 }}>
            {droppedParams.map((row) => (
              <li key={`${row.server}/${row.tool}/${row.param}`}>
                {t("agent_form.mcp_bind_drop_item", {
                  tool: row.tool,
                  param: row.param,
                  variable: row.variable,
                })}
              </li>
            ))}
          </ul>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t("agent_form.mcp_bind_drop_hint")}
          </Text>
        </div>
      ),
      onOk: () => onChange(nextServers, nextAllow, kept),
    });
  };

  const toggleServer = (row: ServerRow, on: boolean): void => {
    if (on) {
      emit([...servers, row.name], allowTools, argBindings);
      fetchTools(row);
    } else {
      const names = new Set(toolNamesOf(row.name));
      emit(
        servers.filter((s) => s !== row.name),
        allowTools.filter((a) => !names.has(a)),
        argBindings,
      );
    }
  };

  const selectedCountOf = (name: string): number => {
    const names = new Set(toolNamesOf(name));
    return allowTools.filter((a) => names.has(a)).length;
  };

  const toggleTool = (toolName: string, on: boolean): void =>
    emit(
      servers,
      on ? [...allowTools, toolName] : allowTools.filter((a) => a !== toolName),
      argBindings,
    );

  const selectAllTools = (toolList: McpTool[]): void =>
    emit(
      servers,
      Array.from(new Set([...allowTools, ...toolList.map((x) => x.name)])),
      argBindings,
    );

  const clearTools = (toolList: McpTool[]): void => {
    const names = new Set(toolList.map((x) => x.name));
    emit(
      servers,
      allowTools.filter((a) => !names.has(a)),
      argBindings,
    );
  };

  // ── B-61: per-parameter bindings ─────────────────────────────────────────
  const argsOf = (server: string, tool: string): Record<string, string> =>
    argBindings.find((b) => b.server === server && b.tool === tool)?.args ?? {};

  /** Bind ``param`` to ``variable``, or unbind it when ``variable`` is null.
   *  A tool whose last parameter is unbound loses its whole entry — the
   *  manifest requires ``args`` to be non-empty. */
  const setBinding = (
    server: string,
    tool: string,
    param: string,
    variable: string | null,
  ): void => {
    const current = argsOf(server, tool);
    const nextArgs =
      variable === null
        ? Object.fromEntries(
            Object.entries(current).filter(([key]) => key !== param),
          )
        : { ...current, [param]: variable };
    const others = argBindings.filter(
      (b) => !(b.server === server && b.tool === tool),
    );
    emit(
      servers,
      allowTools,
      Object.keys(nextArgs).length === 0
        ? others
        : [...others, { server, tool, args: nextArgs }],
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
                          setOnlySelected(false);
                          setExpandedBindings([]);
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
        // 反馈(2026-08-27):默认 520 宽读长描述太挤——放宽到 880,窄屏由
        // maxWidth 兜底;列表相应加高(62vh)。
        width={880}
        style={{ maxWidth: "94vw" }}
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
    const byQuery = tq
      ? tools.filter((x) => x.name.toLowerCase().includes(tq))
      : tools;
    const shown = onlySelected
      ? byQuery.filter((x) => allowTools.includes(x.name))
      : byQuery;
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
            style={{ width: 240 }}
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
          <Checkbox
            data-testid={`af-mcp-only-selected-${row.name}`}
            checked={onlySelected}
            onChange={(e) => setOnlySelected(e.target.checked)}
          >
            <Text style={{ fontSize: 12 }}>
              {t("agent_form.mcp_only_selected")}
            </Text>
          </Checkbox>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t("agent_form.mcp_selected_count", { count: selectedCount })}
          </Text>
        </Space>
        <div
          style={{
            maxHeight: "62vh",
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
              <div key={tool.name}>
                {/* BUG-15 — 描述从 hover Tooltip 改为行内单行截断:窄弹窗里
                    Tooltip 会翻转盖住相邻工具名;hover title 仍可看全文。 */}
                <Checkbox
                  data-testid={`af-mcp-tool-${tool.name}`}
                  checked={allowTools.includes(tool.name)}
                  onChange={(e) => toggleTool(tool.name, e.target.checked)}
                  style={{ display: "flex", alignItems: "flex-start" }}
                >
                  <span style={{ minWidth: 0, display: "block" }}>
                    <Text style={{ fontSize: 13 }}>{tool.name}</Text>
                    {tool.description && (
                      <Text
                        type="secondary"
                        title={tool.description}
                        // 两行 clamp(SkillPicker BUG-6 同款):比单行截断少
                        // 依赖 hover;title 兜长文。不用 antd ellipsis prop——
                        // 它的多行形态要 tooltip 配置,正是 BUG-15 摘掉的遮挡层。
                        style={{
                          display: "-webkit-box",
                          WebkitLineClamp: 2,
                          WebkitBoxOrient: "vertical",
                          fontSize: 12,
                          overflow: "hidden",
                        }}
                      >
                        {tool.description}
                      </Text>
                    )}
                  </span>
                </Checkbox>
                {/* 绑定编辑器是 Checkbox 的兄弟,不是它的 label 内容 ——
                    下拉框放进 label 里点一下就会连带勾掉工具。 */}
                {renderBindings(row, tool)}
              </div>
            ))
          )}
        </div>
      </div>
    );
  }

  // ── B-61: the per-parameter binding editor for one tool ──────────────────
  function renderBindings(row: ServerRow, tool: McpTool) {
    const params = paramsOf(tool);
    // 没有 schema 就没有参数可绑;不在范围内的工具绑了也会被 manifest 校验拒掉。
    if (params.length === 0) return null;
    if (!toolInScope(row.name, tool.name, servers, allowTools)) return null;
    const bound = argsOf(row.name, tool.name);
    const boundCount = Object.keys(bound).length;
    const open = expandedBindings.includes(tool.name);
    // 读屏要能答「展开的是哪一块」,所以 aria-expanded 必须配一个 aria-controls
    // 指向真实存在的 id。
    const panelId = `af-mcp-bind-panel-${row.name}-${tool.name}`;
    return (
      <div style={{ marginLeft: 24 }}>
        <Button
          type="link"
          size="small"
          style={{ paddingLeft: 0 }}
          data-testid={`af-mcp-bind-toggle-${tool.name}`}
          aria-label={t("agent_form.mcp_bind_open", { tool: tool.name })}
          aria-expanded={open}
          aria-controls={panelId}
          onClick={() =>
            setExpandedBindings((prev) =>
              prev.includes(tool.name)
                ? prev.filter((x) => x !== tool.name)
                : [...prev, tool.name],
            )
          }
        >
          {t("agent_form.mcp_bind_label")}
          {boundCount > 0 && ` · ${t("agent_form.mcp_bind_count", { count: boundCount })}`}
        </Button>
        {open &&
          (promptVariables.length === 0 ? (
            <Text
              type="secondary"
              id={panelId}
              data-testid={`af-mcp-bind-no-vars-${tool.name}`}
              style={{ display: "block", fontSize: 12, paddingBottom: 4 }}
            >
              {t("agent_form.mcp_bind_no_variables")}
            </Text>
          ) : (
            <div
              id={panelId}
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 4,
                paddingBottom: 6,
              }}
            >
              <Text type="secondary" style={{ fontSize: 12 }}>
                {t("agent_form.mcp_bind_hint")}
              </Text>
              {params.map((param: ToolParam) => {
                const id = `af-mcp-bind-${row.name}-${tool.name}-${param.name}`;
                return (
                  <div
                    key={param.name}
                    data-testid={`af-mcp-bind-row-${tool.name}-${param.name}`}
                    style={{ display: "flex", alignItems: "center", gap: 8 }}
                  >
                    <span style={{ fontSize: 12, minWidth: 180 }}>
                      <label htmlFor={id}>{param.name}</label>
                      {param.required && (
                        // antd 自己的必填星号同款:视觉标记,读屏不念。
                        <Text
                          type="danger"
                          aria-hidden="true"
                          title={t("agent_form.mcp_bind_required")}
                          style={{ marginLeft: 2 }}
                        >
                          *
                        </Text>
                      )}
                    </span>
                    <Select
                      id={id}
                      size="small"
                      style={{ minWidth: 220 }}
                      value={bound[param.name] ?? AUTO}
                      onChange={(value: string) =>
                        setBinding(
                          row.name,
                          tool.name,
                          param.name,
                          value === AUTO ? null : value,
                        )
                      }
                      options={[
                        { value: AUTO, label: t("agent_form.mcp_bind_auto") },
                        ...promptVariables.map((name) => ({
                          value: name,
                          label: name,
                        })),
                      ]}
                    />
                  </div>
                );
              })}
            </div>
          ))}
      </div>
    );
  }
}
