/**
 * Create Tenant drawer — Stream U PR G.
 *
 * Folds the standalone Create-Tenant page into a drawer opened from the
 * ``/settings/tenants`` list page. Provisions a new tenant via
 * ``POST /v1/tenants``. The ``tenant_id`` UUID client-side validation
 * (PR #370) is preserved verbatim: blank → omitted, valid UUID → forwarded,
 * a human-typed slug → blocked before the POST.
 *
 * The drawer is only opened from the system-admin-gated tenants page, so the
 * page's ``not-admin`` gate, breadcrumb, and page header are dropped here. On
 * success ``onCreated`` fires so the parent can refresh the list.
 */
import { useCallback, useEffect, useState } from "react";
import { Alert, App, Button, Drawer, Form, Input, Select, Typography } from "antd";
import { useTranslation } from "react-i18next";

import {
  createTenant,
  type CreatedTenant,
  type CreateTenantBody,
  type FirstAdminSummary,
} from "../api/tenants";
import type { TenantPlan } from "../api/tenant_config";
import { ApiError } from "../api/client";
import { OneTimeCredentialPanel } from "./OneTimeCredentialPanel";

const { Text } = Typography;

const PLAN_OPTIONS: TenantPlan[] = ["free", "pro", "enterprise"];

/** Canonical UUID form (any version) — the backend's ``tenant_id`` is a UUID. */
const UUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

interface CreateTenantForm {
  display_name: string;
  plan: TenantPlan;
  tenant_id?: string;
  first_admin_email?: string;
  first_admin_display_name?: string;
}

interface CreateTenantDrawerProps {
  open: boolean;
  onClose: () => void;
  /** Fires after a successful POST so the parent can refresh the list.
   *  Receives the created tenant's record. */
  onCreated: (record: CreatedTenant) => void;
}

export function CreateTenantDrawer({ open, onClose, onCreated }: CreateTenantDrawerProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();

  const [form] = Form.useForm<CreateTenantForm>();
  const [submitting, setSubmitting] = useState(false);
  const [createdId, setCreatedId] = useState<string | null>(null);
  const [firstAdmin, setFirstAdmin] = useState<FirstAdminSummary | null>(null);
  /** Set only when the first admin got a generated password — switches the
   *  drawer to the one-time credential result view instead of closing. */
  const [credentialResult, setCredentialResult] = useState<{
    account: string;
    password: string;
  } | null>(null);

  const reset = useCallback(() => {
    setCreatedId(null);
    setFirstAdmin(null);
    setCredentialResult(null);
    form.resetFields();
  }, [form]);

  useEffect(() => {
    if (!open) reset();
  }, [open, reset]);

  const handleCancel = useCallback(() => {
    reset();
    onClose();
  }, [reset, onClose]);

  const onCreate = useCallback(async () => {
    let values: CreateTenantForm;
    try {
      values = await form.validateFields();
    } catch {
      // Field-level errors are already surfaced by the form; nothing to submit.
      return;
    }
    const body: CreateTenantBody = {
      display_name: values.display_name,
      plan: values.plan,
    };
    const tid = values.tenant_id?.trim();
    if (tid) {
      body.tenant_id = tid;
    }
    const adminEmail = values.first_admin_email?.trim();
    if (adminEmail) {
      body.first_admin_email = adminEmail;
      const adminName = values.first_admin_display_name?.trim();
      if (adminName) {
        body.first_admin_display_name = adminName;
      }
    }
    setSubmitting(true);
    try {
      const record = await createTenant(body);
      setCreatedId(record.tenant_id);
      setFirstAdmin(record.first_admin ?? null);
      onCreated(record);
      const admin = record.first_admin;
      const initialPassword = admin?.initial_password;
      if (admin && initialPassword) {
        // Generated-password path: switch to the one-time credential result
        // view instead of the usual toast + form reset — the password must
        // stay visible (and copyable) until the admin explicitly closes.
        setCredentialResult({ account: admin.email, password: initialPassword });
      } else {
        message.success(t("settings_create_tenant.created"));
        form.resetFields();
      }
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `${err.code}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "unknown error";
      message.error(msg);
    } finally {
      setSubmitting(false);
    }
  }, [form, message, onCreated, t]);

  return (
    <Drawer
      open={open}
      onClose={handleCancel}
      title={t("settings_create_tenant.page_title")}
      width={520}
      destroyOnHidden
      footer={
        credentialResult !== null ? (
          <div style={{ display: "flex", justifyContent: "flex-end" }}>
            <Button type="primary" onClick={handleCancel} data-testid="ct-close-saved">
              {t("settings_create_tenant.credentials_close")}
            </Button>
          </div>
        ) : (
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <Button onClick={handleCancel} disabled={submitting} data-testid="ct-cancel">
              {t("common.cancel")}
            </Button>
            <Button
              type="primary"
              loading={submitting}
              onClick={onCreate}
              data-testid="ct-submit"
            >
              {t("settings_create_tenant.create_btn")}
            </Button>
          </div>
        )
      }
      data-testid="create-tenant-drawer"
    >
      {credentialResult !== null ? (
        <div data-testid="ct-credential-result">
          {createdId !== null && (
            <Alert
              type="success"
              showIcon
              style={{ marginBottom: 16 }}
              message={t("settings_create_tenant.created")}
              description={
                <span>
                  {t("settings_create_tenant.created_detail")}{" "}
                  <Text code copyable data-testid="ct-created-id">
                    {createdId}
                  </Text>
                </span>
              }
              data-testid="ct-created"
            />
          )}
          <OneTimeCredentialPanel
            account={credentialResult.account}
            password={credentialResult.password}
            loginUrl={window.location.origin}
          />
        </div>
      ) : (
        <>
          {createdId !== null && (
            <Alert
              type="success"
              showIcon
              style={{ marginBottom: 16 }}
              message={t("settings_create_tenant.created")}
              description={
                <span>
                  {t("settings_create_tenant.created_detail")}{" "}
                  <Text code copyable data-testid="ct-created-id">
                    {createdId}
                  </Text>
                  {firstAdmin !== null && (
                    <div style={{ marginTop: 8 }} data-testid="ct-first-admin">
                      {t("settings_create_tenant.first_admin_provisioned")}{" "}
                      <Text code>{firstAdmin.email}</Text> ({firstAdmin.status})
                    </div>
                  )}
                </span>
              }
              data-testid="ct-created"
            />
          )}
          <Form form={form} layout="vertical" initialValues={{ plan: "free" }} data-testid="ct-form">
            <Form.Item
              name="display_name"
              label={t("settings_create_tenant.field_display_name")}
              rules={[
                { required: true, message: t("settings_create_tenant.display_name_required") },
              ]}
            >
              <Input data-testid="ct-display-name" maxLength={128} />
            </Form.Item>
            <Form.Item name="plan" label={t("settings_create_tenant.field_plan")}>
              <Select<TenantPlan>
                data-testid="ct-plan"
                options={PLAN_OPTIONS.map((p) => ({ value: p, label: p }))}
              />
            </Form.Item>
            <Form.Item
              name="tenant_id"
              label={t("settings_create_tenant.field_tenant_id")}
              extra={t("settings_create_tenant.tenant_id_hint")}
              rules={[
                {
                  validator: (_rule, value) => {
                    const v = (value ?? "").trim();
                    if (v === "" || UUID_RE.test(v)) return Promise.resolve();
                    return Promise.reject(
                      new Error(t("settings_create_tenant.tenant_id_invalid")),
                    );
                  },
                },
              ]}
            >
              <Input
                data-testid="ct-tenant-id"
                placeholder={t("settings_create_tenant.tenant_id_placeholder")}
              />
            </Form.Item>
            <Form.Item
              name="first_admin_email"
              label={t("settings_create_tenant.field_first_admin_email")}
              extra={t("settings_create_tenant.first_admin_hint")}
              rules={[
                {
                  type: "email",
                  message: t("settings_create_tenant.first_admin_email_invalid"),
                },
              ]}
            >
              <Input data-testid="ct-first-admin-email" maxLength={320} />
            </Form.Item>
            <Form.Item
              name="first_admin_display_name"
              label={t("settings_create_tenant.field_first_admin_display_name")}
            >
              <Input data-testid="ct-first-admin-name" maxLength={128} />
            </Form.Item>
          </Form>
        </>
      )}
    </Drawer>
  );
}
