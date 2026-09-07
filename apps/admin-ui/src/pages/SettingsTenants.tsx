/**
 * Settings — Tenants page (Stream U, PR D).
 *
 * Lists every tenant on the platform (``GET /v1/tenants``). Platform-level
 * read — only system_admins see the table (mirrors the backend gate). Each
 * row's "Manage" action switches the current tenant scope into that tenant
 * (persisted by :func:`TenantScopeProvider`) and jumps to its per-tenant
 * config page, where config / quotas / credentials are edited.
 */
import { startTransition, useCallback, useEffect, useState } from "react";
import { Alert, App, Button, Modal, Popconfirm, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Building } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";

import {
  activateTenant,
  deactivateTenant,
  listTenants,
  resendFirstAdmin,
  type TenantSummary,
} from "../api/tenants";
import { useAuth } from "../auth/AuthContext";
import { useTenantScope } from "../tenant/TenantScopeContext";
import { CreateTenantDrawer } from "../components/CreateTenantDrawer";
import { OneTimeCredentialPanel } from "../components/OneTimeCredentialPanel";
import { PageHeader } from "../components/PageHeader";

export function SettingsTenants() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const auth = useAuth();
  const isSystemAdmin = auth.identity?.isSystemAdmin ?? false;
  const { setScope } = useTenantScope();
  const navigate = useNavigate();

  const [rows, setRows] = useState<TenantSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [createOpen, setCreateOpen] = useState(false);
  /** One-time credential from "Resend first-admin credentials" (password mode). */
  const [credential, setCredential] = useState<{ account: string; password: string } | null>(
    null,
  );

  const reload = useCallback(() => {
    setLoading(true);
    listTenants().then(
      (data) => {
        // Hide the synthetic platform tenant — it's not a customer tenant; its
        // shared resources are managed under the ``*`` (platform) scope, and
        // "Manage"/"Deactivate" on it would be meaningless or dangerous.
        setRows(data.filter((r) => !r.is_platform));
        setLoading(false);
      },
      (err: unknown) => {
        setError(err instanceof Error ? err.message : "unknown error");
        setLoading(false);
      },
    );
  }, []);

  useEffect(() => {
    if (!isSystemAdmin) {
      setLoading(false);
      return;
    }
    reload();
  }, [isSystemAdmin, reload]);

  const changeStatus = useCallback(
    async (id: string, kind: "deactivate" | "activate") => {
      try {
        if (kind === "deactivate") {
          await deactivateTenant(id);
        } else {
          await activateTenant(id);
        }
        message.success(t("settings_tenants.status_changed"));
        reload();
      } catch {
        message.error(t("settings_tenants.status_change_failed"));
      }
    },
    [message, t, reload],
  );

  // Platform-scope compensation for a tenant whose first admin never got a
  // usable credential (Keycloak reset failed at create time, or the one-time
  // panel was dismissed). The per-tenant "Resend" needs that very admin's
  // session, so it is unreachable for them — this is the way back in.
  const resendCredentials = useCallback(
    async (id: string) => {
      try {
        const result = await resendFirstAdmin(id);
        if (result.initial_password) {
          setCredential({ account: result.email, password: result.initial_password });
        } else {
          message.success(t("settings_tenants.resend_first_admin_sent"));
        }
      } catch {
        message.error(t("settings_tenants.resend_first_admin_failed"));
      }
    },
    [message, t],
  );

  // The scope change and the navigation must land in ONE commit. Shell's
  // ``useScopeRedirect`` fires on the scope edge and tests the *current*
  // ``location.pathname`` against the new scope; if the scope commits while
  // the path is still ``/settings/tenants`` (a platform-level page, not part
  // of a single tenant's nav) it bounces to that scope's default page and
  // clobbers the navigation below. react-router v7 wraps ``navigate`` in
  // ``startTransition`` by default (v6's ``v7_startTransition`` future flag),
  // so the urgent ``setScope`` would otherwise commit first, on its own.
  // Putting both in the same transition makes the effect see the consistent
  // pair. Same shape in ``TenantSwitcher.handleChange``.
  const manage = useCallback(
    (id: string) => {
      startTransition(() => {
        setScope(id);
        navigate("/settings/tenant-config");
      });
    },
    [setScope, navigate],
  );

  const columns: ColumnsType<TenantSummary> = [
    { title: t("settings_tenants.col_display_name"), dataIndex: "display_name", key: "display_name" },
    { title: t("settings_tenants.col_plan"), dataIndex: "plan", key: "plan" },
    {
      title: t("settings_tenants.col_tenant_id"),
      dataIndex: "tenant_id",
      key: "tenant_id",
      render: (id: string) => (
        <Typography.Text code copyable>
          {id}
        </Typography.Text>
      ),
    },
    {
      title: t("settings_tenants.col_created"),
      dataIndex: "created_at",
      key: "created_at",
      render: (v: string) => new Date(v).toLocaleString(),
    },
    {
      title: t("settings_tenants.col_status"),
      key: "status",
      render: (_: unknown, r: TenantSummary) => (
        <Tag
          color={r.status === "suspended" ? "red" : "green"}
          data-testid={`st-status-${r.tenant_id}`}
        >
          {r.status === "suspended"
            ? t("settings_tenants.st_suspended")
            : t("settings_tenants.st_active")}
        </Tag>
      ),
    },
    {
      title: t("settings_tenants.col_actions"),
      key: "actions",
      render: (_: unknown, r: TenantSummary) => (
        <span style={{ display: "inline-flex", gap: 8 }}>
          <Button
            size="small"
            data-testid={`st-manage-${r.tenant_id}`}
            onClick={() => manage(r.tenant_id)}
          >
            {t("settings_tenants.manage")}
          </Button>
          <Button
            size="small"
            data-testid={`st-resend-first-admin-${r.tenant_id}`}
            onClick={() => resendCredentials(r.tenant_id)}
          >
            {t("settings_tenants.resend_first_admin")}
          </Button>
          {r.status === "active" ? (
            <Popconfirm
              title={t("settings_tenants.deactivate_confirm")}
              onConfirm={() => changeStatus(r.tenant_id, "deactivate")}
            >
              <Button size="small" danger data-testid={`st-deactivate-${r.tenant_id}`}>
                {t("settings_tenants.deactivate")}
              </Button>
            </Popconfirm>
          ) : (
            <Button
              size="small"
              data-testid={`st-activate-${r.tenant_id}`}
              onClick={() => changeStatus(r.tenant_id, "activate")}
            >
              {t("settings_tenants.activate")}
            </Button>
          )}
        </span>
      ),
    },
  ];

  return (
    <div data-testid="st-root">
      <Modal
        open={credential !== null}
        onCancel={() => setCredential(null)}
        title={t("credential_panel.title")}
        destroyOnHidden
        footer={
          <Button type="primary" onClick={() => setCredential(null)} data-testid="st-credential-close">
            {t("settings_create_tenant.credentials_close")}
          </Button>
        }
      >
        {credential !== null && (
          <OneTimeCredentialPanel
            account={credential.account}
            password={credential.password}
            loginUrl={window.location.origin}
          />
        )}
      </Modal>
      <PageHeader
        icon={<Building size={18} strokeWidth={1.5} />}
        title={t("settings_tenants.page_title")}
        subtitle={t("settings_tenants.subtitle")}
        actions={
          isSystemAdmin && (
            <Button
              type="primary"
              data-testid="tenants-create"
              onClick={() => setCreateOpen(true)}
            >
              {t("settings_tenants.create")}
            </Button>
          )
        }
      />

      {!isSystemAdmin ? (
        <Alert
          type="warning"
          showIcon
          message={t("settings_tenants.not_admin_title")}
          description={t("settings_tenants.not_admin_body")}
          data-testid="st-not-admin"
        />
      ) : (
        <>
          {error !== null && (
            <Alert
              type="error"
              showIcon
              data-testid="st-error"
              message={t("settings_tenants.failed_to_load")}
              description={error}
              style={{ marginBottom: 16 }}
            />
          )}
          <Table<TenantSummary>
            data-testid="st-table"
            rowKey="tenant_id"
            loading={loading}
            dataSource={rows}
            pagination={false}
            locale={{ emptyText: t("settings_tenants.empty") }}
            columns={columns}
          />
          <CreateTenantDrawer
            open={createOpen}
            onClose={() => setCreateOpen(false)}
            onCreated={() => {
              setCreateOpen(false);
              reload();
            }}
          />
        </>
      )}
    </div>
  );
}
