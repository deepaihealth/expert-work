/**
 * Users — the top-level user-dimension observability + management view
 * (conversation-centric IA, M2 tenant-wide roster).
 *
 * A "user" is a ``tenant_user`` (subject_type="user"): the ``user_id`` an
 * external app passes when calling an agent, or a logged-in employee. The
 * human-recognizable identifier is ``subject_id`` (the passed-in id / OIDC
 * ``sub``) — NOT the internal surrogate UUID. Admin-only: the page
 * self-guards on the tenant ``admin`` role (or system_admin), and the
 * backend 403s independently (defense in depth).
 *
 * A row drills into the agent-agnostic user profile (``/users/:userId``).
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Input,
  Segmented,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
} from "antd";
import type { TableColumnsType } from "antd";
import { Users as UsersIcon } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { listUsers, type TenantUserRoster, type TenantUserRosterItem } from "../api/users";
import { useAuth } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { concreteTenantScope, useTenantScope } from "../tenant/TenantScopeContext";

const { Text } = Typography;

type TypeFilter = "all" | "external" | "member";

const NUM_STYLE = { fontVariantNumeric: "tabular-nums" as const };

export function Users() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { identity } = useAuth();
  const { apiTenantScope } = useTenantScope();

  const isAdmin =
    (identity?.isSystemAdmin ?? false) || (identity?.roles.includes("admin") ?? false);
  // Only deny once the server-truth identity has resolved — an API-key admin
  // reads ``roles: []`` optimistically until ``/v1/me`` returns.
  const denied = (identity?.serverResolved ?? false) && !isAdmin;

  const [data, setData] = useState<TenantUserRoster | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState<TypeFilter>("all");

  // Scope-aware roster: a UUID-scope-switched system_admin sees the selected
  // tenant's roster instead of silently reading their home tenant (跨租户钻取
  // 2026-07-31). The "*" aggregate maps to undefined — the backend types
  // tenant_id as ``UUID | None`` and 422s a literal "*" — so the aggregate
  // view falls back to the caller's home tenant. Caveat: under a UUID switch
  // the UserProfile/UserDetail sub-panels (Conversations/Memory/Usage/
  // Workspace panes) are not scope-wired yet and still read the home
  // tenant — W3 计划内.
  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await listUsers({ tenantScope: concreteTenantScope(apiTenantScope) }));
    } catch (err) {
      setError(err instanceof ApiError ? `${err.code}: ${err.message}` : String(err));
    } finally {
      setLoading(false);
    }
  }, [apiTenantScope]);

  useEffect(() => {
    if (denied) {
      setLoading(false);
      return;
    }
    void refresh();
  }, [denied, refresh]);

  const items = data?.items ?? [];

  const stats = useMemo(() => {
    let conversations = 0;
    let errors = 0;
    for (const u of items) {
      conversations += u.conversation_count;
      errors += u.error_count;
    }
    return { conversations, errors };
  }, [items]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return items.filter((u) => {
      if (typeFilter === "external" && u.is_member) return false;
      if (typeFilter === "member" && !u.is_member) return false;
      if (q === "") return true;
      return (
        u.subject_id.toLowerCase().includes(q) ||
        (u.display_name ?? "").toLowerCase().includes(q) ||
        // An employee is most likely searched for by the email the roster
        // shows them under — their subject_id is an opaque OIDC sub.
        (u.member_email ?? "").toLowerCase().includes(q)
      );
    });
  }, [items, query, typeFilter]);

  const truncated = (data?.total ?? 0) > items.length;

  const columns: TableColumnsType<TenantUserRosterItem> = useMemo(
    () => [
      {
        title: t("users_page.col_user"),
        key: "user",
        render: (_: unknown, record) => {
          // An employee's subject_id is an OIDC sub UUID and display_name is
          // usually unset — lead with whatever names them (matching the detail
          // page's title chain) and keep subject_id below as the id operators
          // copy. An external caller has neither, so their subject_id — the id
          // their app passed — leads on its own.
          const name = record.display_name ?? record.member_email;
          return (
            <Space direction="vertical" size={0}>
              {name && <Text style={{ fontSize: 13 }}>{name}</Text>}
              <Text code style={{ fontSize: 12 }} type={name ? "secondary" : undefined}>
                {record.subject_id}
              </Text>
            </Space>
          );
        },
      },
      {
        title: t("users_page.col_type"),
        key: "type",
        width: 140,
        render: (_: unknown, record) =>
          record.is_member ? (
            <Tag color="blue" data-testid={`user-type-${record.user_id}`}>
              {record.member_role
                ? `${t("users_page.tag_member")} · ${record.member_role}`
                : t("users_page.tag_member")}
            </Tag>
          ) : (
            <Tag color="orange" data-testid={`user-type-${record.user_id}`}>
              {t("users_page.tag_external")}
            </Tag>
          ),
      },
      {
        title: t("users_page.col_conversations"),
        dataIndex: "conversation_count",
        key: "conversations",
        width: 100,
        render: (v: number) => <span style={NUM_STYLE}>{v}</span>,
      },
      {
        title: t("users_page.col_runs"),
        dataIndex: "run_count",
        key: "runs",
        width: 90,
        render: (v: number) => <span style={NUM_STYLE}>{v}</span>,
      },
      {
        title: t("users_page.col_errors"),
        dataIndex: "error_count",
        key: "errors",
        width: 90,
        render: (v: number) => (
          <span
            style={{
              ...NUM_STYLE,
              color: v > 0 ? "var(--ew-status-error, #f5222d)" : undefined,
            }}
            data-testid={`user-errors-${v > 0 ? "hot" : "zero"}`}
          >
            {v}
          </span>
        ),
      },
      {
        title: t("users_page.col_last_active"),
        // last_run_at 而非 last_active_at:后者是「任何触达」——员工 admin
        // 登录控制台看页面每个请求都 bump 它,列表显示 8/26 而详情页对话
        // 的末次活跃(跑 run)是 8/24,两页对不上(2026-08-26 用户反馈)。
        // 本页口号是「观察交互」,统一用真正跑 run 的时间,与详情页同源。
        dataIndex: "last_run_at",
        key: "last_run_at",
        width: 190,
        render: (iso: string | null) =>
          iso ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {new Date(iso).toLocaleString()}
            </Text>
          ) : (
            <Text type="secondary">—</Text>
          ),
      },
    ],
    [t],
  );

  return (
    <div data-testid="users-root">
      <PageHeader
        icon={<UsersIcon size={18} strokeWidth={1.5} />}
        title={t("users_page.page_title")}
        subtitle={t("users_page.subtitle")}
      />

      {denied ? (
        <Alert
          type="warning"
          showIcon
          message={t("users_page.not_admin_title")}
          description={t("users_page.not_admin_body")}
          data-testid="users-not-admin"
        />
      ) : (
        <>
          {error !== null && (
            <Alert
              type="error"
              showIcon
              message={t("users_page.failed_to_load")}
              description={error}
              style={{ marginBottom: 16 }}
              data-testid="users-error"
            />
          )}

          <div
            style={{
              display: "flex",
              gap: 24,
              flexWrap: "wrap",
              padding: 16,
              marginBottom: 16,
              background: "var(--ew-surface-raised)",
              border: "1px solid var(--ew-border-subtle)",
              borderRadius: 6,
            }}
            data-testid="users-stats"
          >
            <Statistic title={t("users_page.stat_total")} value={data?.total ?? 0} />
            <Statistic title={t("users_page.stat_conversations")} value={stats.conversations} />
            <Statistic
              title={t("users_page.stat_errors")}
              value={stats.errors}
              valueStyle={
                stats.errors > 0 ? { color: "var(--ew-status-error, #f5222d)" } : undefined
              }
            />
          </div>

          <div
            style={{
              display: "flex",
              gap: 8,
              alignItems: "center",
              flexWrap: "wrap",
              marginBottom: 12,
            }}
          >
            <Input
              allowClear
              placeholder={t("users_page.search_placeholder")}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              style={{ maxWidth: 280 }}
              data-testid="users-search"
            />
            <Segmented<TypeFilter>
              value={typeFilter}
              onChange={(value) => setTypeFilter(value)}
              options={[
                { value: "all", label: t("users_page.type_all") },
                { value: "external", label: t("users_page.type_external") },
                { value: "member", label: t("users_page.type_member") },
              ]}
              data-testid="users-type-filter"
            />
            {truncated && (
              <Tag color="warning" data-testid="users-truncated">
                {t("users_page.truncated")}
              </Tag>
            )}
          </div>

          <Table<TenantUserRosterItem>
            size="small"
            columns={columns}
            dataSource={filtered}
            rowKey={(r) => r.user_id}
            loading={loading}
            pagination={{ showSizeChanger: false, pageSize: 50, hideOnSinglePage: true }}
            onRow={(record) => ({
              onClick: () =>
                navigate(`/users/${encodeURIComponent(record.user_id)}`, {
                  state: {
                    subjectId: record.subject_id,
                    displayName: record.display_name ?? undefined,
                    isMember: record.is_member,
                    memberRole: record.member_role ?? undefined,
                  },
                }),
              style: { cursor: "pointer" },
            })}
            locale={{ emptyText: t("users_page.empty") }}
            data-testid="users-table"
          />
        </>
      )}
    </div>
  );
}
