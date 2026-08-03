/**
 * Knowledge list page — Stream H.7 + KB commercial uplift.
 *
 * The bases list over ``/v1/knowledge``; selecting a base navigates to its
 * detail page (``/knowledge/:name``) where documents, retrieval testing and
 * settings live. (Earlier this was a single master-detail screen; the detail
 * surface outgrew it — see ``KnowledgeDetail``.)
 *
 * Tenant semantics: the list follows the global TenantScope switch (W3) and
 * aggregates every tenant under ``"*"`` (W4). Writes still bind by name to the
 * caller's own tenant, so they are greyed out in both the switched-in and the
 * aggregate view — see ``writeDisabled`` below.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { App, Alert, Button, Empty, Popconfirm, Space, Table, Tag, Tooltip, Typography } from "antd";
import type { TableColumnsType } from "antd";
import { BookOpen, Plus, RefreshCcw, Trash2 } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { deleteBase, listBases, type KnowledgeBase } from "../api/knowledge";
import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { useTenantScope } from "../tenant/TenantScopeContext";
import { useIsTenantSwitched } from "../tenant/useIsTenantSwitched";
import { CreateBaseModal } from "../components/CreateBaseModal";
import { PageHeader } from "../components/PageHeader";
import { ReadonlyTooltip } from "../components/ReadonlyTooltip";
import { tenantRowKey } from "../utils/tenantRowKey";

const { Text } = Typography;

function errMessage(err: unknown): string {
  return err instanceof ApiError
    ? `${err.code}: ${err.message}`
    : err instanceof Error
      ? err.message
      : "unknown error";
}

export function KnowledgeAdmin() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  // Cross-tenant W3 — the list rides the ambient scope (the old H-19
  // "does not follow the tenant switch" note is gone with the backend gap).
  const { apiTenantScope } = useTenantScope();
  // Cross-tenant W3 — 切入态只读:删库/新建是写操作,置灰。
  const isTenantSwitched = useIsTenantSwitched();
  // Cross-tenant W4 — "*" aggregate: tenant column + row-jump carries the
  // owning tenant (home rows keep the plain URL, see onRow below).
  const isAggregate = apiTenantScope === "*";
  // Cross-tenant W4 (review C-1) — the aggregate is read-only too: writes
  // (delete/create) bind by name to the caller's HOME tenant, so acting on a
  // foreign row would hit the home tenant's same-named base.
  const writeDisabled = isAggregate || isTenantSwitched;
  const { identity } = useAuth();
  const navigate = useNavigate();

  const [bases, setBases] = useState<KnowledgeBase[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setBases(await listBases(apiTenantScope));
    } catch (err) {
      setError(errMessage(err));
    } finally {
      setLoading(false);
    }
  }, [apiTenantScope]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleDelete = useCallback(
    async (name: string) => {
      try {
        await deleteBase(name);
        await refresh();
      } catch (err) {
        message.error(errMessage(err));
      }
    },
    [message, refresh],
  );

  const columns: TableColumnsType<KnowledgeBase> = useMemo(
    () => [
      {
        title: t("knowledge_page.col_base_name"),
        dataIndex: "name",
        key: "name",
        render: (name: string, record) => (
          <Space size={8}>
            <Text strong>{name}</Text>
            {record.needs_reindex && (
              <Tag color="warning" bordered={false}>
                {t("knowledge_page.needs_reindex_tag")}
              </Tag>
            )}
            {record.reindexing && (
              <Tag color="processing" bordered={false}>
                {t("knowledge_page.reindexing_tag")}
              </Tag>
            )}
          </Space>
        ),
      },
      {
        title: t("knowledge_page.col_description"),
        dataIndex: "description",
        key: "description",
        ellipsis: true,
        render: (description: string | null) =>
          description ? (
            <Text type="secondary" style={{ fontSize: 13 }}>
              {description}
            </Text>
          ) : (
            <Text type="secondary">—</Text>
          ),
      },
      {
        title: t("knowledge_page.col_documents"),
        key: "documents",
        width: 90,
        render: (_: unknown, record) => (
          <Text className="mono">{record.stats?.document_count ?? 0}</Text>
        ),
      },
      {
        title: t("knowledge_page.col_chunks_total"),
        key: "chunks",
        width: 90,
        render: (_: unknown, record) => (
          <Text className="mono">{record.stats?.chunk_count ?? 0}</Text>
        ),
      },
      // Cross-tenant W4 — raw tenant UUID is noise inside a single tenant;
      // only the "*" aggregate needs it to tell rows apart.
      ...(isAggregate
        ? [
            {
              title: t("knowledge_page.col_tenant"),
              dataIndex: "tenant_id" as const,
              key: "tenant_id",
              width: 160,
              render: (tenantId: string | null | undefined) =>
                tenantId ? (
                  <Tooltip title={tenantId}>
                    <Text code style={{ fontSize: 12 }}>
                      {tenantId.slice(0, 8)}…
                    </Text>
                  </Tooltip>
                ) : (
                  <Text type="secondary">—</Text>
                ),
            },
          ]
        : []),
      {
        title: "",
        key: "actions",
        width: 60,
        render: (_: unknown, record) => (
          <ReadonlyTooltip
            on={writeDisabled}
            title={isAggregate ? t("common.cross_tenant_readonly") : undefined}
          >
            <Popconfirm
              title={t("knowledge_page.delete_base_confirm_title", { name: record.name })}
              description={t("knowledge_page.delete_base_confirm_body")}
              onConfirm={() => void handleDelete(record.name)}
              okText={t("knowledge_page.delete")}
              okButtonProps={{ danger: true }}
              disabled={writeDisabled}
            >
              <Button
                size="small"
                danger
                type="text"
                disabled={writeDisabled}
                icon={<Trash2 size={13} strokeWidth={1.5} />}
                onClick={(e) => e.stopPropagation()}
                aria-label={t("knowledge_page.delete")}
                data-testid={`kb-delete-${record.name}`}
              />
            </Popconfirm>
          </ReadonlyTooltip>
        ),
      },
    ],
    [t, handleDelete, writeDisabled, isAggregate],
  );

  return (
    <div data-testid="knowledge-root">
      <PageHeader
        icon={<BookOpen size={18} strokeWidth={1.5} />}
        title={t("knowledge_page.page_title")}
        subtitle={
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t("knowledge_page.subtitle")}
          </Text>
        }
        actions={
          <Space>
            <Button
              icon={<RefreshCcw size={14} strokeWidth={1.5} />}
              onClick={() => void refresh()}
              loading={loading}
              aria-label={t("common.refresh")}
              data-testid="kb-refresh"
            >
              {t("common.refresh")}
            </Button>
            <ReadonlyTooltip
              on={writeDisabled}
              title={isAggregate ? t("common.cross_tenant_readonly") : undefined}
            >
              <Button
                type="primary"
                icon={<Plus size={14} strokeWidth={1.5} />}
                onClick={() => setCreateOpen(true)}
                disabled={writeDisabled}
                data-testid="kb-create-open"
              >
                {t("knowledge_page.create_base")}
              </Button>
            </ReadonlyTooltip>
          </Space>
        }
      />

      {error !== null && (
        <Alert
          type="error"
          showIcon
          message={t("knowledge_page.failed_to_load")}
          description={error}
          style={{ marginBottom: 16 }}
          data-testid="knowledge-error"
        />
      )}

      <Table<KnowledgeBase>
        columns={columns}
        // Two tenants may own a same-named base in the "*" aggregate — key
        // the row by (tenant, name) so they never collide.
        rowKey={(r) => tenantRowKey(r.tenant_id, r.name)}
        dataSource={bases}
        loading={loading}
        pagination={false}
        onRow={(record) => ({
          onClick: () => {
            // Cross-tenant W4 — in the "*" aggregate a foreign-tenant row
            // would 404 (getBase falls back to the home tenant), so carry
            // the row's owning tenant into the detail URL. Absent = pre-W4
            // backend — keeps the old behavior.
            const query =
              record.tenant_id != null && record.tenant_id !== identity?.homeTenantId
                ? `?tenant_id=${encodeURIComponent(record.tenant_id)}`
                : "";
            navigate(`/knowledge/${encodeURIComponent(record.name)}${query}`);
          },
          style: { cursor: "pointer" },
        })}
        locale={{
          emptyText: (
            <Empty
              description={
                isAggregate ? t("knowledge_page.empty_cross") : t("knowledge_page.bases_empty")
              }
            />
          ),
        }}
        data-testid="kb-table"
      />

      <CreateBaseModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(created) => {
          setCreateOpen(false);
          navigate(`/knowledge/${encodeURIComponent(created.name)}`);
        }}
      />
    </div>
  );
}
