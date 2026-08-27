/**
 * Settings — Members page (Stream R W2).
 *
 * Tenant members and their invitation lifecycle. An admin invites users
 * by email (batch, max 50), then resends invites or removes members.
 * ``DELETE`` is lifecycle-aware: an ``invited`` row is revoked, an
 * ``active`` row is suspended. Invitation is a batch call that returns
 * per-item results, so a partial failure (Keycloak conflict / down)
 * surfaces the failing emails without aborting the whole batch.
 *
 * Deletion-hygiene PR5 adds the one-shot "deactivate & purge" action:
 * type-to-confirm (the member's email) then ``POST /{id}:purge`` —
 * lifecycle + role bindings + Keycloak account DELETE + data cascade.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  App,
  Button,
  Drawer,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { TableColumnsType } from "antd";
import { KeyRound, RefreshCw, Send, Trash2, UserPlus, Users, UserX } from "lucide-react";
import { useTranslation } from "react-i18next";

import { PageHeader } from "../components/PageHeader";
import { OneTimeCredentialPanel } from "../components/OneTimeCredentialPanel";
import {
  inviteMembers,
  listMembers,
  purgeMember,
  resendMember,
  resetMemberPassword,
  revokeMember,
  type InvitationItem,
  type InviteResultItem,
  type MemberList,
  type MemberRole,
  type MemberStatus,
  type TenantMember,
} from "../api/members";
import { ApiError } from "../api/client";
import { PurgeUserModal } from "./user_profile/PurgeUserModal";
import { SCOPE_ALL, useTenantScope } from "../tenant/TenantScopeContext";
import { useIsTenantSwitched } from "../tenant/useIsTenantSwitched";
import { ReadonlyTooltip } from "../components/ReadonlyTooltip";

const { Paragraph, Text } = Typography;

const ROLE_OPTIONS: MemberRole[] = ["admin", "operator", "viewer"];

type StatusFilter = "all" | MemberStatus;

const STATUS_FILTERS: StatusFilter[] = [
  "all",
  "invited",
  "active",
  "suspended",
  "revoked",
];

const STATUS_TAG_COLOR: Record<MemberStatus, string> = {
  invited: "purple",
  active: "cyan",
  suspended: "gold",
  revoked: "default",
};

interface InviteForm {
  email: string;
  role: MemberRole;
  display_name?: string;
}

/** Generated-password-mode success item — narrows away the ``string | null
 *  | undefined`` union so callers get a plain ``string`` password. Email
 *  mode (or a failed item) never satisfies this. */
function hasInitialPassword(
  item: InviteResultItem,
): item is InviteResultItem & { initial_password: string } {
  return item.error_code === null && Boolean(item.initial_password);
}

export function SettingsMembers() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const { scope, apiTenantScope } = useTenantScope();
  // Cross-tenant aggregate ("All tenants") is a read-only view: write
  // actions (invite / resend / reset-password / remove) stay on the
  // single-tenant context where the tenant_id is unambiguous.
  const crossTenant = scope === SCOPE_ALL;
  // Cross-tenant W3 — 切入态只读:邀请/重发/吊销/重置密码/清除是写操作,置灰。
  const isTenantSwitched = useIsTenantSwitched();

  const [data, setData] = useState<MemberList | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");

  const [inviteOpen, setInviteOpen] = useState(false);
  const [inviteSubmitting, setInviteSubmitting] = useState(false);
  const [inviteForm] = Form.useForm<InviteForm>();
  // Generated-password mode: successful invite items that carry a one-time
  // password. Non-null switches the drawer body/footer to the result view
  // instead of closing — the password must stay visible until the admin
  // explicitly closes. Reset whenever the drawer closes (any path), so a
  // stale password never lingers past its one render.
  const [inviteCredentials, setInviteCredentials] = useState<
    Array<InviteResultItem & { initial_password: string }> | null
  >(null);
  // Same one-shot semantics for the resend action — a table-row action has
  // no drawer of its own, so a dedicated modal carries the result instead.
  const [resendCredential, setResendCredential] = useState<{
    email: string;
    password: string;
  } | null>(null);

  useEffect(() => {
    if (!inviteOpen) {
      setInviteCredentials(null);
    }
  }, [inviteOpen]);

  const [pwTarget, setPwTarget] = useState<TenantMember | null>(null);
  const [pw, setPw] = useState("");
  const [pwErr, setPwErr] = useState<string | null>(null);

  // One-shot deactivate & purge (deletion-hygiene PR5) — type-to-confirm.
  const [purgeTarget, setPurgeTarget] = useState<TenantMember | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listMembers({
        status: statusFilter === "all" ? undefined : statusFilter,
        // Cross-tenant W3 — bare ambient scope: "*" aggregate or a concrete
        // switched-in tenant UUID (replaces the legacy crossTenant flag).
        tenantScope: apiTenantScope,
      });
      setData(result);
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `${err.code}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "unknown error";
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [statusFilter, apiTenantScope]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const onInvite = useCallback(async () => {
    const values = await inviteForm.validateFields();
    const invitation: InvitationItem = {
      email: values.email.trim(),
      role: values.role,
      ...(values.display_name?.trim()
        ? { display_name: values.display_name.trim() }
        : {}),
    };
    setInviteSubmitting(true);
    try {
      const result = await inviteMembers([invitation]);
      const failed = result.results.filter((r) => r.error_code !== null);
      const withPassword = result.results.filter(hasInitialPassword);
      if (failed.length > 0) {
        const detail = failed
          .map((r) => `${r.email} (${r.error_code})`)
          .join(", ");
        message.warning(t("settings_members.invite_partial_fail", { detail }));
      }
      if (withPassword.length > 0) {
        // Response-driven: a generated password switches the drawer to the
        // result view. It stays open until the admin explicitly closes it —
        // unlike the plain-success path below, which auto-closes.
        setInviteCredentials(withPassword);
      } else {
        if (failed.length === 0) {
          message.success(t("settings_members.invite_success"));
        }
        setInviteOpen(false);
        inviteForm.resetFields();
      }
      refresh();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "failed");
    } finally {
      setInviteSubmitting(false);
    }
  }, [inviteForm, message, refresh, t]);

  const onResend = useCallback(
    async (id: string, email: string) => {
      try {
        const result = await resendMember(id);
        if (result.initial_password) {
          // Response-driven, same as invite: a generated password opens the
          // one-time credential modal instead of the plain "resent" toast.
          setResendCredential({ email, password: result.initial_password });
        } else {
          message.success(t("settings_members.resent"));
        }
        refresh();
      } catch (err) {
        message.error(err instanceof Error ? err.message : "failed");
      }
    },
    [message, refresh, t],
  );

  const onRemove = useCallback(
    async (id: string) => {
      try {
        await revokeMember(id);
        message.success(t("settings_members.removed"));
        refresh();
      } catch (err) {
        message.error(err instanceof Error ? err.message : "failed");
      }
    },
    [message, refresh, t],
  );

  const closePw = useCallback(() => {
    setPwTarget(null);
    setPw("");
    setPwErr(null);
  }, []);

  const submitPw = useCallback(async () => {
    if (pwTarget === null) {
      return;
    }
    if (pw.length < 8) {
      setPwErr(t("settings_members.set_password_too_short"));
      return;
    }
    try {
      await resetMemberPassword(pwTarget.id, pw);
      message.success(t("settings_members.set_password_ok"));
      closePw();
    } catch {
      message.error(t("settings_members.set_password_failed"));
    }
  }, [pwTarget, pw, message, t, closePw]);

  const columns: TableColumnsType<TenantMember> = useMemo(
    () => [
      {
        title: t("settings_members.col_email"),
        dataIndex: "email",
        key: "email",
        render: (email: string) => (
          <Tooltip title={email}>
            <Text code style={{ fontSize: 11 }}>
              {email}
            </Text>
          </Tooltip>
        ),
      },
      ...(crossTenant
        ? [
            {
              title: t("settings_members.col_tenant"),
              dataIndex: "tenant_id",
              key: "tenant_id",
              width: 180,
              render: (tid: string) => (
                <Tooltip title={tid}>
                  <Text code style={{ fontSize: 11 }}>
                    {tid.slice(0, 8)}…
                  </Text>
                </Tooltip>
              ),
            } satisfies TableColumnsType<TenantMember>[number],
          ]
        : []),
      {
        title: t("settings_members.col_name"),
        dataIndex: "display_name",
        key: "display_name",
        width: 180,
        render: (name: string | null) =>
          name === null || name === "" ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              —
            </Text>
          ) : (
            <Text style={{ fontSize: 13 }}>{name}</Text>
          ),
      },
      {
        title: t("settings_members.col_role"),
        dataIndex: "role",
        key: "role",
        width: 120,
        render: (r: MemberRole) => (
          <Tag color={r === "admin" ? "blue" : "default"}>{r}</Tag>
        ),
      },
      {
        title: t("settings_members.col_status"),
        dataIndex: "status",
        key: "status",
        width: 130,
        render: (s: MemberStatus) => (
          <Tag color={STATUS_TAG_COLOR[s]}>{t(`settings_members.status_${s}`)}</Tag>
        ),
      },
      {
        title: t("settings_members.col_invited_at"),
        dataIndex: "invited_at",
        key: "invited_at",
        width: 180,
        render: (iso: string | null) =>
          iso === null ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              —
            </Text>
          ) : (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {new Date(iso).toLocaleString()}
            </Text>
          ),
      },
      {
        title: t("settings_members.col_last_active_at"),
        dataIndex: "last_active_at",
        key: "last_active_at",
        width: 180,
        render: (iso: string | null | undefined) =>
          iso == null ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              —
            </Text>
          ) : (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {new Date(iso).toLocaleString()}
            </Text>
          ),
      },
      ...(crossTenant
        ? []
        : ([
      {
        title: t("settings_members.col_actions"),
        key: "actions",
        width: 300,
        render: (_: unknown, record: TenantMember) => {
          const removable =
            record.status === "invited" || record.status === "active";
          const settable =
            record.keycloak_user_id !== null &&
            (record.status === "active" || record.status === "invited");
          return (
            <Space size={6}>
              {record.status === "invited" && (
                <ReadonlyTooltip on={isTenantSwitched}>
                  <Button
                    size="small"
                    disabled={isTenantSwitched}
                    icon={<Send size={12} strokeWidth={1.75} />}
                    onClick={() => onResend(record.id, record.email)}
                    data-testid={`members-resend-${record.id}`}
                  >
                    {t("settings_members.resend")}
                  </Button>
                </ReadonlyTooltip>
              )}
              {settable && (
                <ReadonlyTooltip on={isTenantSwitched}>
                  <Button
                    size="small"
                    disabled={isTenantSwitched}
                    icon={<KeyRound size={12} strokeWidth={1.75} />}
                    onClick={() => {
                      setPwTarget(record);
                      setPw("");
                      setPwErr(null);
                    }}
                    data-testid={`members-set-password-${record.id}`}
                  >
                    {t("settings_members.set_password")}
                  </Button>
                </ReadonlyTooltip>
              )}
              {removable && (
                <ReadonlyTooltip on={isTenantSwitched}>
                  <Popconfirm
                    title={
                      record.status === "invited"
                        ? t("settings_members.revoke_confirm_title")
                        : t("settings_members.suspend_confirm_title")
                    }
                    description={
                      record.status === "invited"
                        ? t("settings_members.revoke_confirm_body")
                        : t("settings_members.suspend_confirm_body")
                    }
                    okType="danger"
                    okText={t("common.delete")}
                    cancelText={t("common.cancel")}
                    onConfirm={() => onRemove(record.id)}
                    disabled={isTenantSwitched}
                  >
                    <Button
                      size="small"
                      danger
                      disabled={isTenantSwitched}
                      icon={<Trash2 size={12} strokeWidth={1.75} />}
                      data-testid={`members-remove-${record.id}`}
                    >
                      {t("settings_members.remove")}
                    </Button>
                  </Popconfirm>
                </ReadonlyTooltip>
              )}
              {/* One-shot deactivate & purge — every status is purgeable
                  (suspended / revoked rows re-enter as backfill cleanup). */}
              <ReadonlyTooltip on={isTenantSwitched}>
                <Button
                  size="small"
                  danger
                  disabled={isTenantSwitched}
                  icon={<UserX size={12} strokeWidth={1.75} />}
                  onClick={() => setPurgeTarget(record)}
                  data-testid={`members-purge-${record.id}`}
                >
                  {t("settings_members.purge_action")}
                </Button>
              </ReadonlyTooltip>
            </Space>
          );
        },
      },
          ] satisfies TableColumnsType<TenantMember>)),
    ],
    [t, onResend, onRemove, crossTenant, isTenantSwitched],
  );

  return (
    <div data-testid="members-root">
      <PageHeader
        icon={<Users size={18} strokeWidth={1.5} />}
        title={t("settings_members.page_title")}
        subtitle={t("settings_members.subtitle")}
        actions={
          <>
            <Segmented<StatusFilter>
              value={statusFilter}
              onChange={(value) => setStatusFilter(value)}
              options={STATUS_FILTERS.map((s) => ({
                value: s,
                label:
                  s === "all"
                    ? t("settings_members.filter_all")
                    : t(`settings_members.status_${s}`),
              }))}
              data-testid="members-status-filter"
            />
            <Button
              onClick={refresh}
              loading={loading}
              icon={<RefreshCw size={14} strokeWidth={1.5} />}
            >
              {t("common.refresh")}
            </Button>
            {!crossTenant && (
              <ReadonlyTooltip on={isTenantSwitched}>
                <Button
                  type="primary"
                  icon={<UserPlus size={14} strokeWidth={1.5} />}
                  onClick={() => setInviteOpen(true)}
                  disabled={isTenantSwitched}
                  data-testid="members-invite-btn"
                >
                  {t("settings_members.invite")}
                </Button>
              </ReadonlyTooltip>
            )}
          </>
        }
      />

      {crossTenant && (
        <Alert
          type="info"
          showIcon
          message={t("settings_members.cross_tenant_banner")}
          style={{ marginBottom: 12 }}
          data-testid="members-cross-banner"
        />
      )}

      {error !== null && (
        <Alert
          type="error"
          showIcon
          message={t("settings_members.failed_to_load")}
          description={error}
          style={{ marginBottom: 12 }}
          data-testid="members-error"
        />
      )}

      <Table<TenantMember>
        columns={columns}
        dataSource={data?.items ?? []}
        rowKey={(r) => r.id}
        loading={loading}
        pagination={{ pageSize: 50, showSizeChanger: false, total: data?.total ?? 0 }}
        locale={{
          emptyText: (
            <Empty
              description={
                scope === "*"
                  ? t("settings_members.empty_cross")
                  : t("settings_members.empty")
              }
            />
          ),
        }}
        data-testid="members-table"
      />

      <Drawer
        title={t("settings_members.invite_drawer_title")}
        open={inviteOpen}
        onClose={() => setInviteOpen(false)}
        width={520}
        data-testid="members-invite-drawer"
        extra={
          inviteCredentials !== null ? (
            <Button
              type="primary"
              onClick={() => {
                setInviteOpen(false);
                inviteForm.resetFields();
              }}
              data-testid="members-invite-credentials-close"
            >
              {t("settings_members.credentials_close")}
            </Button>
          ) : (
            <Space>
              <Button onClick={() => setInviteOpen(false)}>
                {t("common.cancel")}
              </Button>
              <Button
                type="primary"
                loading={inviteSubmitting}
                onClick={onInvite}
                data-testid="members-invite-submit"
              >
                {t("settings_members.invite_submit")}
              </Button>
            </Space>
          )
        }
      >
        {inviteCredentials !== null ? (
          <Space
            direction="vertical"
            size="large"
            style={{ width: "100%" }}
            data-testid="members-invite-credentials"
          >
            {inviteCredentials.map((item) => (
              <div
                key={item.email}
                data-testid={`members-invite-credential-${item.email}`}
              >
                <Text strong style={{ display: "block", marginBottom: 8 }}>
                  {item.email}
                </Text>
                <OneTimeCredentialPanel
                  account={item.email}
                  password={item.initial_password}
                  loginUrl={window.location.origin}
                />
              </div>
            ))}
          </Space>
        ) : (
          <Form
            form={inviteForm}
            layout="vertical"
            initialValues={{ role: "viewer" }}
          >
            <Form.Item
              name="email"
              label={t("settings_members.field_email")}
              rules={[
                {
                  required: true,
                  message: t("settings_members.email_required"),
                },
                { type: "email", message: t("settings_members.email_invalid") },
              ]}
            >
              <Input
                data-testid="members-invite-email"
                placeholder="user@example.com"
                autoComplete="off"
              />
            </Form.Item>
            <Form.Item
              name="role"
              label={t("settings_members.field_role")}
              rules={[{ required: true, message: t("settings_members.role_required") }]}
            >
              <Select<MemberRole>
                data-testid="members-invite-role"
                options={ROLE_OPTIONS.map((r) => ({ value: r, label: r }))}
              />
            </Form.Item>
            <Form.Item
              name="display_name"
              label={t("settings_members.field_display_name")}
            >
              <Input
                data-testid="members-invite-display-name"
                placeholder={t("settings_members.field_display_name_placeholder")}
                autoComplete="off"
              />
            </Form.Item>
          </Form>
        )}
      </Drawer>

      <Modal
        open={resendCredential !== null}
        title={t("settings_members.resend_credentials_title")}
        onCancel={() => setResendCredential(null)}
        data-testid="members-resend-credentials-modal"
        footer={
          <Button
            type="primary"
            onClick={() => setResendCredential(null)}
            data-testid="members-resend-credentials-close"
          >
            {t("settings_members.credentials_close")}
          </Button>
        }
      >
        {resendCredential !== null && (
          <OneTimeCredentialPanel
            account={resendCredential.email}
            password={resendCredential.password}
            loginUrl={window.location.origin}
          />
        )}
      </Modal>

      <Modal
        open={pwTarget !== null}
        title={t("settings_members.set_password_title")}
        onCancel={closePw}
        data-testid="members-set-password-modal"
        footer={
          <Space>
            <Button onClick={closePw}>{t("common.cancel")}</Button>
            <Button
              type="primary"
              onClick={submitPw}
              data-testid="members-set-password-submit"
            >
              {t("settings_members.set_password_submit")}
            </Button>
          </Space>
        }
      >
        <p
          style={{
            color: "var(--ew-text-secondary)",
            fontSize: 13,
            margin: "0 0 12px",
          }}
        >
          {t("settings_members.set_password_hint")}
        </p>
        <Text style={{ fontSize: 13 }}>
          {t("settings_members.set_password_label")}
        </Text>
        <Input.Password
          data-testid="members-set-password-input"
          value={pw}
          onChange={(e) => {
            setPw(e.target.value);
            setPwErr(null);
          }}
          placeholder={t("settings_members.set_password_placeholder")}
          autoComplete="new-password"
          style={{ marginTop: 6 }}
        />
        {pwErr !== null && (
          <Text
            type="danger"
            style={{ display: "block", marginTop: 8, fontSize: 12 }}
            data-testid="members-set-password-error"
          >
            {pwErr}
          </Text>
        )}
      </Modal>

      {purgeTarget !== null && (
        <PurgeUserModal
          open
          onClose={() => setPurgeTarget(null)}
          userId={purgeTarget.id}
          subjectId={purgeTarget.email}
          confirmTarget={purgeTarget.email}
          onSubmit={() => purgeMember(purgeTarget.id)}
          onPurged={() => {
            setPurgeTarget(null);
            refresh();
          }}
          copy={{
            title: t("settings_members.purge_confirm_title"),
            okText: t("settings_members.purge_action"),
            body: (
              <>
                <Alert
                  type="error"
                  showIcon
                  message={t("settings_members.purge_confirm_body", {
                    email: purgeTarget.email,
                  })}
                  style={{ marginBottom: 16 }}
                />
                {purgeTarget.subject_id === null && (
                  <Paragraph
                    type="secondary"
                    style={{ marginBottom: 16 }}
                    data-testid="members-purge-no-data-note"
                  >
                    {t("settings_members.purge_no_data_note")}
                  </Paragraph>
                )}
              </>
            ),
            typeToConfirm: t("settings_members.purge_type_to_confirm"),
            done: t("settings_members.purge_done"),
            partial: t("settings_members.purge_partial"),
          }}
        />
      )}
    </div>
  );
}
