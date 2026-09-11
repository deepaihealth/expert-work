/**
 * Memory admin page — Stream H.4 PR 2.
 *
 * Per-user (or cross-tenant for system_admin) memory CRUD. The edit
 * drawer reuses the ApprovalCard Monaco pattern from H.3 PR 5: pristine
 * vs dirty buffer detection, with the Save button labelled
 * differently when the buffer has been touched.
 *
 * Search is intentionally client-side — backend has no full-text index
 * on memory.content (recall ranking is vector-based via the
 * embedder). Reviewers loading <1k memories in a single tenant view
 * filter locally; M1 may add server-side fuzzy search if list size
 * grows.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  App,
  Button,
  Drawer,
  Empty,
  Input,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { TableColumnsType } from "antd";
import Editor from "@monaco-editor/react";
import { Brain, Globe2, RefreshCw, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  correctMemory,
  deleteMemory,
  getConsolidatorHealth,
  listMemories,
  updateMemory,
  type ConsolidatorHealth,
  type MemoryItem,
  type MemoryKind,
  type MemoryList,
} from "../api/memory";
import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { useTenantScope } from "../tenant/TenantScopeContext";
import { PageHeader } from "../components/PageHeader";

const { Text } = Typography;

const KIND_OPTIONS: MemoryKind[] = ["fact", "episodic"];

const KIND_COLOR: Record<MemoryKind, string> = {
  fact: "blue",
  episodic: "purple",
};

export function MemoryAdmin() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const { scope, apiTenantScope } = useTenantScope();
  const isSystemAdmin = useAuth().identity?.isSystemAdmin ?? false;

  const [data, setData] = useState<MemoryList | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [kindFilter, setKindFilter] = useState<MemoryKind | undefined>(undefined);
  const [searchText, setSearchText] = useState("");
  // M3 — tenant-admin governance: narrow to one member's memories
  // (server-side ?user_id=, same gate as the user-detail Memory tab).
  const [userFilter, setUserFilter] = useState("");

  const [editing, setEditing] = useState<MemoryItem | null>(null);
  const [editBuf, setEditBuf] = useState("");
  const [submitting, setSubmitting] = useState(false);
  // Stream Memory-Enhance (M-4) — "correct" routes Save through the user
  // self-correction endpoint (confidence → 1.0); "edit" keeps the admin PATCH.
  const [mode, setMode] = useState<"edit" | "correct">("edit");

  // Only a well-formed UUID reaches the server — partial input while
  // typing must not turn into 422s.
  const userIdParam = useMemo(() => {
    const v = userFilter.trim();
    return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(v)
      ? v
      : undefined;
  }, [userFilter]);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listMemories({
        tenantScope: apiTenantScope,
        kind: kindFilter,
        userId: userIdParam,
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
  }, [apiTenantScope, kindFilter, userIdParam]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // B-51 —— 长期记忆整合的健康度。数据源是每次 sweep 的审计行(consolidator
  // 靠 advisory lock 单飞,进程内存只有一个副本有,审计日志才是跨副本可读的
  // 现成来源)。读它要平台级 scope,所以只有 system_admin 看得到这条;读失败
  // 就当没有(整合健康度不该挡住记忆列表)。
  const [consolidator, setConsolidator] = useState<ConsolidatorHealth | null>(null);
  useEffect(() => {
    if (!isSystemAdmin) {
      setConsolidator(null);
      return;
    }
    let cancelled = false;
    getConsolidatorHealth()
      .then((health) => {
        if (!cancelled) setConsolidator(health);
      })
      .catch(() => {
        if (!cancelled) setConsolidator(null);
      });
    return () => {
      cancelled = true;
    };
  }, [isSystemAdmin]);

  const openEdit = useCallback((item: MemoryItem) => {
    setMode("edit");
    setEditing(item);
    setEditBuf(item.content);
  }, []);

  const openCorrect = useCallback((item: MemoryItem) => {
    setMode("correct");
    setEditing(item);
    setEditBuf(item.content);
  }, []);

  const isDirty = useMemo(
    () => editing !== null && editBuf !== editing.content,
    [editing, editBuf],
  );

  const onSave = useCallback(async () => {
    if (editing === null) return;
    if (editBuf.trim().length === 0) {
      message.error(t("memory.empty_content"));
      return;
    }
    setSubmitting(true);
    try {
      if (mode === "correct") {
        await correctMemory(editing.id, { action: "rewrite", content: editBuf });
        message.success(t("memory.corrected"));
      } else {
        await updateMemory(editing.id, { content: editBuf });
        message.success(t("memory.updated"));
      }
      setEditing(null);
      refresh();
    } catch (err) {
      const msg = err instanceof ApiError && err.code === "EMBEDDER_UNCONFIGURED"
        ? t("memory.embedder_unconfigured")
        : err instanceof Error ? err.message : "failed";
      message.error(msg);
    } finally {
      setSubmitting(false);
    }
  }, [editing, editBuf, mode, message, refresh, t]);

  const onDelete = useCallback(async (id: string) => {
    try {
      await deleteMemory(id);
      message.success(t("memory.deleted"));
      refresh();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "failed");
    }
  }, [message, refresh, t]);

  const filteredItems = useMemo(() => {
    const all = data?.items ?? [];
    if (searchText.trim().length === 0) return all;
    const needle = searchText.toLowerCase();
    return all.filter((m) => m.content.toLowerCase().includes(needle));
  }, [data, searchText]);

  const columns: TableColumnsType<MemoryItem> = useMemo(() => [
    {
      title: t("memory.col_kind"),
      dataIndex: "kind",
      key: "kind",
      width: 110,
      render: (k: MemoryKind) => <Tag color={KIND_COLOR[k]}>{k}</Tag>,
    },
    {
      title: t("memory.col_content"),
      dataIndex: "content",
      key: "content",
      render: (text: string) => (
        <Tooltip title={text} mouseEnterDelay={0.4}>
          <Text style={{ fontSize: 13 }}>
            {text.length > 120 ? `${text.slice(0, 120)}…` : text}
          </Text>
        </Tooltip>
      ),
    },
    {
      title: t("memory.col_score"),
      key: "score",
      width: 150,
      render: (_, record) => (
        <Space size={4}>
          <Tooltip title={t("memory.importance_tip")}>
            <Tag color="geekblue" style={{ fontSize: 11 }}>
              {t("memory.importance_abbr")} {record.importance.toFixed(2)}
            </Tag>
          </Tooltip>
          <Tooltip title={t("memory.confidence_tip")}>
            <Tag color={record.confidence >= 1 ? "green" : "default"} style={{ fontSize: 11 }}>
              {t("memory.confidence_abbr")} {record.confidence.toFixed(2)}
            </Tag>
          </Tooltip>
        </Space>
      ),
    },
    {
      title: t("memory.col_user"),
      dataIndex: "user_id",
      key: "user_id",
      width: 200,
      render: (uid: string) => (
        <Text code style={{ fontSize: 11 }}>{uid.slice(0, 8)}…</Text>
      ),
    },
    {
      title: t("memory.col_created"),
      dataIndex: "created_at",
      key: "created_at",
      width: 200,
      render: (iso: string) => (
        <Text type="secondary" style={{ fontSize: 12 }}>{new Date(iso).toLocaleString()}</Text>
      ),
    },
    {
      title: t("memory.col_actions"),
      key: "actions",
      width: 180,
      render: (_, record) => (
        <Space size={4}>
          <Button size="small" onClick={() => openEdit(record)} data-testid={`memory-edit-${record.id}`}>
            {t("common.edit")}
          </Button>
          <Button
            size="small"
            onClick={() => openCorrect(record)}
            data-testid={`memory-correct-${record.id}`}
          >
            {t("memory.correct")}
          </Button>
          <Popconfirm
            title={t("memory.delete_confirm_title")}
            description={t("memory.delete_confirm_body")}
            okType="danger"
            okText={t("common.delete")}
            cancelText={t("common.cancel")}
            onConfirm={() => onDelete(record.id)}
          >
            <Button size="small" danger icon={<Trash2 size={12} strokeWidth={1.75} />} data-testid={`memory-delete-${record.id}`}>
              {t("common.delete")}
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ], [t, openEdit, openCorrect, onDelete]);

  const isCrossTenant = data?.cross_tenant ?? false;

  return (
    <div data-testid="memory-root">
      <PageHeader
        icon={<Brain size={18} strokeWidth={1.5} />}
        title={t("memory.page_title")}
        subtitle={t("memory.subtitle")}
        actions={
          <>
            {isCrossTenant && (
              <Tag icon={<Globe2 size={12} strokeWidth={1.5} />} color="purple" data-testid="memory-cross-banner">
                {t("memory.cross_tenant_banner")}
              </Tag>
            )}
            <Input.Search
              placeholder={t("memory.search_placeholder")}
              value={searchText}
              onChange={(e) => setSearchText(e.target.value)}
              style={{ width: 240 }}
              data-testid="memory-search"
              allowClear
            />
            <Input
              placeholder={t("memory.filter_user_placeholder")}
              aria-label={t("memory.filter_user_placeholder")}
              value={userFilter}
              onChange={(e) => setUserFilter(e.target.value)}
              style={{ width: 220 }}
              data-testid="memory-user-filter"
              allowClear
            />
            <Select<MemoryKind | "all">
              value={kindFilter ?? "all"}
              onChange={(v) => setKindFilter(v === "all" ? undefined : v as MemoryKind)}
              style={{ width: 140 }}
              aria-label={t("memory.filter_kind")}
              data-testid="memory-kind-filter"
              options={[
                { value: "all", label: t("memory.filter_kind_all") },
                ...KIND_OPTIONS.map((k) => ({ value: k, label: k })),
              ]}
            />
            <Button onClick={refresh} loading={loading} icon={<RefreshCw size={14} strokeWidth={1.5} />}>
              {t("common.refresh")}
            </Button>
          </>
        }
      />

      {error !== null && (
        <Alert type="error" showIcon message={t("memory.failed_to_load")} description={error} style={{ marginBottom: 12 }} data-testid="memory-error" />
      )}

      {consolidator !== null && consolidator.consecutiveCredentialFailures > 0 && (
        <Alert
          type="warning"
          showIcon
          message={t("memory.consolidator_credentials_missing_title", {
            count: consolidator.consecutiveCredentialFailures,
          })}
          description={t("memory.consolidator_credentials_missing_body", {
            providers: consolidator.providers.join(t("memory.provider_list_separator")),
          })}
          style={{ marginBottom: 12 }}
          data-testid="memory-consolidator-alert"
        />
      )}

      <Table<MemoryItem>
        columns={columns}
        dataSource={filteredItems}
        rowKey={(r) => r.id}
        loading={loading}
        pagination={{ pageSize: 50, showSizeChanger: false, total: filteredItems.length }}
        locale={{
          emptyText: (
            <Empty description={scope === "*" ? t("memory.empty_cross") : t("memory.empty_home")} />
          ),
        }}
        data-testid="memory-table"
      />

      <Drawer
        title={
          editing !== null
            ? mode === "correct"
              ? t("memory.correct_title")
              : t("memory.edit_title")
            : ""
        }
        open={editing !== null}
        onClose={() => setEditing(null)}
        width={680}
        data-testid="memory-edit-drawer"
        extra={
          <Space>
            <Button onClick={() => setEditing(null)}>{t("common.cancel")}</Button>
            <Button
              type="primary"
              onClick={onSave}
              loading={submitting}
              disabled={!isDirty}
              data-testid="memory-save-btn"
            >
              {isDirty ? t("memory.save_dirty") : t("common.save")}
            </Button>
          </Space>
        }
      >
        {editing !== null && (
          <Space direction="vertical" style={{ width: "100%" }} size={12}>
            <div>
              <Text type="secondary" style={{ fontSize: 12 }}>{t("memory.edit_meta_user")}</Text>
              <div><Text code style={{ fontSize: 11 }}>{editing.user_id}</Text></div>
            </div>
            <div>
              <Text type="secondary" style={{ fontSize: 12 }}>{t("memory.edit_meta_kind")}</Text>
              <div><Tag color={KIND_COLOR[editing.kind]}>{editing.kind}</Tag></div>
            </div>
            <div>
              <Text type="secondary">{t("memory.edit_content_label")}</Text>
              <div style={{ border: "1px solid var(--ew-border-default)", borderRadius: 4, marginTop: 4 }}>
                <Editor
                  height="320px"
                  defaultLanguage="markdown"
                  value={editBuf}
                  onChange={(v) => setEditBuf(v ?? "")}
                  options={{ minimap: { enabled: false }, fontSize: 13, wordWrap: "on" }}
                  data-testid="memory-content-editor"
                />
              </div>
            </div>
            <Alert
              type="info"
              showIcon
              message={t("memory.embedder_note")}
              style={{ fontSize: 12 }}
            />
          </Space>
        )}
      </Drawer>
    </div>
  );
}
