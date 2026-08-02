/**
 * Add MCP server drawer — Stream W (tenant admin).
 *
 * The primary "Add MCP server" affordance. Opens the catalog browser — the
 * tenant-facing MCP marketplace — where each platform server carries an
 * enable/disable toggle (opt-in selection, P4). ``oauth2`` connectors add a
 * per-user "Authorize" step (``OAuthConnectForm``). A secondary "Advanced — add
 * a custom server" link demotes the legacy custom-URL registration (the
 * existing ``CreateMcpServerDrawer`` in create mode).
 *
 * Mirrors ``CreateMcpServerDrawer`` chrome (Drawer 560 px, footer-less body
 * with per-step controls, reset-on-close).
 */
import { useCallback, useEffect, useState } from "react";
import { App, Button, Divider, Drawer, Tooltip, Typography } from "antd";
import { useTranslation } from "react-i18next";

import {
  disablePlatformServer,
  enablePlatformServer,
  listTenantCatalog,
  type TenantCatalogEntry,
} from "../../api/mcp-catalog";
import { ApiError } from "../../api/client";
import { useIsTenantSwitched } from "../../tenant/useIsTenantSwitched";
import { CatalogBrowser } from "./CatalogBrowser";
import { OAuthConnectForm } from "./OAuthConnectForm";
import { CreateMcpServerDrawer } from "../CreateMcpServerDrawer";

export interface AddMcpServerDrawerProps {
  open: boolean;
  onClose: () => void;
  /** Fires after a successful custom-server create so the parent can refresh +
   *  close. (Enable/disable toggles persist in place and keep the drawer open.) */
  onSaved: () => void;
  /** Fires after a successful platform enable/disable toggle so the parent can
   *  refresh its unified server list (the toggle keeps the drawer open). */
  onEnabledChange?: () => void;
}

type Step =
  | { kind: "browse" }
  | { kind: "authorize"; entry: TenantCatalogEntry };

export function AddMcpServerDrawer({
  open,
  onClose,
  onSaved,
  onEnabledChange,
}: AddMcpServerDrawerProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  // Cross-tenant W3 — 切入态只读:自建 server 入口是写操作,置灰。
  const isTenantSwitched = useIsTenantSwitched();

  const [step, setStep] = useState<Step>({ kind: "browse" });
  const [entries, setEntries] = useState<TenantCatalogEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [customOpen, setCustomOpen] = useState(false);

  const reset = useCallback(() => {
    setStep({ kind: "browse" });
    setError(null);
    setCustomOpen(false);
  }, []);

  useEffect(() => {
    if (!open) {
      reset();
      return;
    }
    setLoading(true);
    setError(null);
    listTenantCatalog().then(
      (data) => {
        setEntries(data);
        setLoading(false);
      },
      (err: unknown) => {
        setError(err instanceof Error ? err.message : "unknown error");
        setLoading(false);
      },
    );
  }, [open, reset]);

  const handleClose = useCallback(() => {
    reset();
    onClose();
  }, [reset, onClose]);

  const handleToggleEnable = useCallback(
    async (entry: TenantCatalogEntry, next: boolean) => {
      try {
        if (next) {
          await enablePlatformServer(entry.id);
        } else {
          await disablePlatformServer(entry.id);
        }
        setEntries((prev) =>
          prev.map((e) =>
            e.id === entry.id ? { ...e, tenant_enabled: next } : e,
          ),
        );
        onEnabledChange?.();
      } catch (err) {
        const msg =
          err instanceof ApiError
            ? `${err.code}: ${err.message}`
            : err instanceof Error
              ? err.message
              : "unknown error";
        message.error(msg);
        // Re-throw so the toggle reverts to the persisted state.
        throw err instanceof Error ? err : new Error(msg);
      }
    },
    [message, onEnabledChange],
  );

  return (
    <>
      <Drawer
        open={open}
        onClose={handleClose}
        title={
          step.kind === "browse"
            ? t("mcp_catalog.browser_title")
            : t("mcp_oauth.connect_title", { name: step.entry.display_name })
        }
        width={560}
        destroyOnHidden
        data-testid="amsd-drawer"
      >
        {step.kind === "browse" ? (
          <>
            <CatalogBrowser
              entries={entries}
              loading={loading}
              error={error}
              onToggleEnable={handleToggleEnable}
              onAuthorize={(entry) => setStep({ kind: "authorize", entry })}
            />
            <Divider />
            <div data-testid="amsd-advanced">
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {t("mcp_catalog.advanced_hint")}
              </Typography.Text>
              <div style={{ marginTop: 8 }}>
                <Tooltip
                  title={isTenantSwitched ? t("common.tenant_switched_readonly") : undefined}
                >
                  <Button
                    data-testid="amsd-custom"
                    disabled={isTenantSwitched}
                    onClick={() => setCustomOpen(true)}
                  >
                    {t("mcp_catalog.advanced_custom")}
                  </Button>
                </Tooltip>
              </div>
            </div>
          </>
        ) : (
          <OAuthConnectForm
            entry={step.entry}
            onBack={() => setStep({ kind: "browse" })}
          />
        )}
      </Drawer>

      <CreateMcpServerDrawer
        open={customOpen}
        onClose={() => setCustomOpen(false)}
        onSaved={() => {
          setCustomOpen(false);
          onSaved();
          handleClose();
        }}
        editing={null}
      />
    </>
  );
}
