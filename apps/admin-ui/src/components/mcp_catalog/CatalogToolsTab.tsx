/**
 * Platform server Tools tab — Stream MCP platform-servers.
 *
 * Live-probes the server (``POST .../{id}/tools``) and lists every advertised
 * tool. Each tool shows its name + description and expands to its input-schema
 * parameters ({@link ToolParamsTable}, shared with the tenant MCP page). A
 * per-tool enable Switch persists the platform-curated denylist via ``disabled_tools`` (default all
 * enabled); disabled tools are filtered from ``list_tools`` at runtime so no
 * tenant agent sees them. A refresh button re-probes.
 *
 * ``oauth2`` servers are not probeable platform-side (per-user token), so the
 * tab shows an explanatory note instead of a list.
 */
import { useCallback, useEffect, useState, type ReactNode } from "react";
import {
  Alert,
  App,
  Button,
  Collapse,
  Empty,
  Space,
  Spin,
  Switch,
  Tag,
  Typography,
} from "antd";
import { RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  listCatalogTools,
  updatePlatformCatalogEntry,
  type McpCatalogEntry,
  type McpCatalogTool,
} from "../../api/mcp-catalog";
import { ApiError } from "../../api/client";
import { ToolParamsTable } from "./ToolParamsTable";

const { Text } = Typography;

type ProbeStatus = "loading" | "ok" | "unreachable" | "not_probeable";

export interface CatalogToolsTabProps {
  entry: McpCatalogEntry;
  /** Fires after a per-tool toggle persists, with the updated catalog entry. */
  onUpdated: (updated: McpCatalogEntry) => void;
  /** Fires after a successful probe with the advertised tool count (tab badge). */
  onLoaded?: (count: number) => void;
}

export function CatalogToolsTab({
  entry,
  onUpdated,
  onLoaded,
}: CatalogToolsTabProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();

  const [status, setStatus] = useState<ProbeStatus>("loading");
  const [tools, setTools] = useState<McpCatalogTool[]>([]);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [disabled, setDisabled] = useState<ReadonlySet<string>>(
    new Set(entry.disabled_tools ?? []),
  );
  const [saving, setSaving] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setStatus("loading");
    try {
      const result = await listCatalogTools(entry.id);
      if (result.status === "ok") {
        setTools(result.tools);
        setStatus("ok");
        onLoaded?.(result.tools.length);
      } else {
        setTools([]);
        setErrorCode(result.error);
        setStatus(result.status);
      }
    } catch {
      setTools([]);
      setStatus("unreachable");
    }
  }, [entry.id, onLoaded]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const toggleTool = useCallback(
    async (name: string, enabled: boolean) => {
      const next = new Set(disabled);
      if (enabled) next.delete(name);
      else next.add(name);
      setSaving(name);
      try {
        const updated = await updatePlatformCatalogEntry(entry.id, {
          disabled_tools: [...next],
        });
        setDisabled(next);
        onUpdated(updated);
      } catch (err) {
        const msg =
          err instanceof ApiError
            ? `${err.code}: ${err.message}`
            : err instanceof Error
              ? err.message
              : "unknown error";
        message.error(msg);
      } finally {
        setSaving(null);
      }
    },
    [disabled, entry.id, onUpdated, message],
  );

  const header = (
    <div
      style={{
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
        marginBottom: 12,
      }}
    >
      <Space size={8}>
        <Text strong>{t("mcp_catalog.tools_title")}</Text>
        {status === "ok" && <Tag>{tools.length}</Tag>}
      </Space>
      <Button
        size="small"
        icon={<RefreshCw size={13} strokeWidth={1.6} />}
        onClick={() => void refresh()}
        loading={status === "loading"}
        data-testid="ct-refresh"
      >
        {t("mcp_catalog.tools_refresh")}
      </Button>
    </div>
  );

  let body: ReactNode;
  if (status === "loading") {
    body = (
      <div style={{ textAlign: "center", padding: "32px 0" }}>
        <Spin />
      </div>
    );
  } else if (status === "not_probeable") {
    body = (
      <Alert
        type="info"
        showIcon
        data-testid="ct-oauth"
        message={t("mcp_catalog.tools_oauth_note")}
      />
    );
  } else if (status === "unreachable") {
    body = (
      <Alert
        type="error"
        showIcon
        data-testid="ct-unreachable"
        message={t("mcp_catalog.tools_unreachable")}
        description={errorCode ?? undefined}
      />
    );
  } else if (tools.length === 0) {
    body = <Empty description={t("mcp_catalog.tools_none")} />;
  } else {
    body = (
      <Collapse
        data-testid="ct-list"
        items={tools.map((tool) => ({
          key: tool.name,
          label: (
            <Space
              size={10}
              onClick={(e) => e.stopPropagation()}
              style={{ cursor: "default" }}
            >
              <Switch
                size="small"
                checked={!disabled.has(tool.name)}
                loading={saving === tool.name}
                onChange={(checked) => void toggleTool(tool.name, checked)}
                aria-label={t("mcp_catalog.tool_enable_aria", { name: tool.name })}
                data-testid={`ct-toggle-${tool.name}`}
              />
              <Text strong style={{ fontFamily: "var(--ew-font-mono, monospace)" }}>
                {tool.name}
              </Text>
              {tool.description && (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {tool.description}
                </Text>
              )}
            </Space>
          ),
          children: (
            <ToolParamsTable schema={tool.input_schema} testId={`ct-params-${tool.name}`} />
          ),
        }))}
      />
    );
  }

  return (
    <div data-testid="ct-root">
      {header}
      {body}
    </div>
  );
}
