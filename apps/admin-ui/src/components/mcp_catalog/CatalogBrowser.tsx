/**
 * Catalog browser — Stream W (tenant admin).
 *
 * Card list of ``listTenantCatalog()`` entries — the tenant-facing MCP
 * marketplace. Each card shows the connector's display_name, description,
 * category and required-tier badge. Entries with ``entitled === false`` render
 * a lock badge ("requires {tier} plan") and are not selectable.
 *
 * Platform-server model (P4): a tenant **opts in** to a fully-configured
 * platform server via an enable/disable toggle (``onToggleEnable``) — there is
 * no auth-schema form to fill. ``oauth2`` (per-user) connectors additionally
 * expose an "Authorize" action (``onAuthorize``) once the tenant has enabled
 * them, so each member can authorize their own account.
 */
import { useCallback, useState } from "react";
import {
  Alert,
  Badge,
  Button,
  Card,
  Empty,
  Space,
  Spin,
  Switch,
  Tag,
  Typography,
} from "antd";
import { Lock } from "lucide-react";
import { useTranslation } from "react-i18next";

import type {
  McpRequiredTier,
  TenantCatalogEntry,
} from "../../api/mcp-catalog";
import { useIsTenantSwitched } from "../../tenant/useIsTenantSwitched";
import { ReadonlyTooltip } from "../../components/ReadonlyTooltip";

const { Text, Paragraph } = Typography;

const TIER_COLOR: Record<McpRequiredTier, string> = {
  free: "default",
  pro: "blue",
  enterprise: "gold",
};

export interface CatalogBrowserProps {
  entries: TenantCatalogEntry[];
  loading: boolean;
  error: string | null;
  /** Toggle the tenant's opt-in for a platform server (A and B alike). */
  onToggleEnable: (entry: TenantCatalogEntry, next: boolean) => Promise<void>;
  /** Per-user authorize for an enabled ``oauth2`` connector. */
  onAuthorize: (entry: TenantCatalogEntry) => void;
}

export function CatalogBrowser({
  entries,
  loading,
  error,
  onToggleEnable,
  onAuthorize,
}: CatalogBrowserProps) {
  const { t } = useTranslation();
  // Cross-tenant W3 — 切入态只读:目录启停是写操作,置灰。
  const isTenantSwitched = useIsTenantSwitched();
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());

  const handleToggle = useCallback(
    async (entry: TenantCatalogEntry, next: boolean) => {
      setBusy((prev) => new Set(prev).add(entry.name));
      try {
        await onToggleEnable(entry, next);
      } finally {
        setBusy((prev) => {
          const updated = new Set(prev);
          updated.delete(entry.name);
          return updated;
        });
      }
    },
    [onToggleEnable],
  );

  if (loading) {
    return (
      <div
        style={{ textAlign: "center", padding: "32px 0" }}
        data-testid="cb-loading"
      >
        <Spin />
      </div>
    );
  }

  if (error !== null) {
    return (
      <Alert
        type="error"
        showIcon
        message={t("mcp_catalog.browser_failed")}
        description={error}
        data-testid="cb-error"
      />
    );
  }

  if (entries.length === 0) {
    return (
      <Empty
        description={t("mcp_catalog.browser_empty")}
        data-testid="cb-empty"
      />
    );
  }

  return (
    <div
      data-testid="cb-root"
      style={{ display: "flex", flexDirection: "column", gap: 12 }}
    >
      {entries.map((entry) => {
        const locked = !entry.entitled;
        const isOauth = entry.auth_type === "oauth2";
        const isShared = entry.auth_type === "bearer";
        const card = (
          <Card
            key={entry.id}
            size="small"
            data-testid={`cb-card-${entry.name}`}
            style={locked ? { opacity: 0.6 } : undefined}
            styles={{ body: { padding: 14 } }}
          >
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                gap: 12,
              }}
            >
              <div style={{ minWidth: 0 }}>
                <Text strong>{entry.display_name}</Text>
                {entry.category && (
                  <Tag style={{ marginLeft: 8 }}>{entry.category}</Tag>
                )}
                <Tag
                  color={TIER_COLOR[entry.required_tier]}
                  style={{ marginLeft: 4 }}
                >
                  {t(`mcp_catalog.tier_${entry.required_tier}`)}
                </Tag>
                {isOauth && (
                  <Tag
                    color="geekblue"
                    style={{ marginLeft: 4 }}
                    data-testid={`cb-oauth-${entry.name}`}
                  >
                    {t("mcp_catalog.oauth_badge")}
                  </Tag>
                )}
                {entry.description && (
                  <Paragraph
                    type="secondary"
                    style={{ marginTop: 6, marginBottom: 0, fontSize: 12 }}
                    ellipsis={{ rows: 2 }}
                  >
                    {entry.description}
                  </Paragraph>
                )}
                {isShared && (
                  <Text
                    type="warning"
                    style={{ display: "block", marginTop: 4, fontSize: 11 }}
                  >
                    {t("mcp_catalog.shared_hint")}
                  </Text>
                )}
              </div>
              <div style={{ flexShrink: 0, alignSelf: "center" }}>
                {locked ? (
                  <Button
                    size="small"
                    disabled
                    icon={<Lock size={13} strokeWidth={1.5} />}
                    data-testid={`cb-locked-${entry.name}`}
                  >
                    {t("mcp_catalog.requires_tier", {
                      tier: t(`mcp_catalog.tier_${entry.required_tier}`),
                    })}
                  </Button>
                ) : (
                  <Space size={8}>
                    <ReadonlyTooltip on={isTenantSwitched}>
                      <Switch
                        size="small"
                        checked={entry.tenant_enabled}
                        disabled={isTenantSwitched}
                        loading={busy.has(entry.name)}
                        onChange={(next) => void handleToggle(entry, next)}
                        aria-label={t("mcp_catalog.enable_aria", {
                          name: entry.display_name,
                        })}
                        data-testid={`cb-toggle-${entry.name}`}
                      />
                    </ReadonlyTooltip>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {entry.tenant_enabled
                        ? t("mcp_catalog.enabled")
                        : t("mcp_catalog.enable")}
                    </Text>
                    {isOauth && entry.tenant_enabled && (
                      <Button
                        type="primary"
                        size="small"
                        data-testid={`cb-authorize-${entry.name}`}
                        onClick={() => onAuthorize(entry)}
                      >
                        {t("mcp_catalog.authorize")}
                      </Button>
                    )}
                  </Space>
                )}
              </div>
            </div>
          </Card>
        );
        return locked ? (
          <Badge.Ribbon
            key={entry.id}
            text={t("mcp_catalog.locked_ribbon")}
            color="gray"
          >
            {card}
          </Badge.Ribbon>
        ) : (
          card
        );
      })}
    </div>
  );
}
