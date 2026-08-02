/**
 * Knowledge-base detail page — KB commercial uplift.
 *
 * ``/knowledge/:name/:tab`` — mirrors the platform's list→detail-with-tabs
 * convention (AgentsList→AgentDetail). Stats header + a re-index banner when
 * the base's embedding model is stale; tabs for Documents / Retrieval test /
 * Settings.
 */
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Alert, App, Button, Empty, Skeleton, Space, Tabs, Tag, Tooltip, Typography } from "antd";
import { BookOpen, RefreshCcw } from "lucide-react";
import { useTranslation } from "react-i18next";

import { getBase, reindexBase, type KnowledgeBase } from "../api/knowledge";
import { ApiError } from "../api/client";
import { concreteTenantScope, useTenantScope } from "../tenant/TenantScopeContext";
import { useIsTenantSwitched } from "../tenant/useIsTenantSwitched";
import { PageHeader } from "../components/PageHeader";
import { DocumentsTab } from "./knowledge_detail/DocumentsTab";
import { RetrievalTestTab } from "./knowledge_detail/RetrievalTestTab";
import { SettingsTab } from "./knowledge_detail/SettingsTab";

const { Text } = Typography;

function errMessage(err: unknown): string {
  return err instanceof ApiError
    ? `${err.code}: ${err.message}`
    : err instanceof Error
      ? err.message
      : "unknown error";
}

export function KnowledgeDetail() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const { name, tab } = useParams<{ name: string; tab?: string }>();
  const navigate = useNavigate();
  // Cross-tenant W3 — detail reads take a concrete UUID only ("*" 422s).
  const { apiTenantScope } = useTenantScope();
  // Cross-tenant W3 — 切入态只读:重建索引是写操作,置灰。
  const isTenantSwitched = useIsTenantSwitched();

  const [base, setBase] = useState<KnowledgeBase | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reindexing, setReindexing] = useState(false);

  const refresh = useCallback(async () => {
    if (!name) return;
    setLoading(true);
    setError(null);
    try {
      setBase(await getBase(name, concreteTenantScope(apiTenantScope)));
    } catch (err) {
      setError(errMessage(err));
    } finally {
      setLoading(false);
    }
  }, [name, apiTenantScope]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleReindex = useCallback(async () => {
    if (!name) return;
    setReindexing(true);
    try {
      await reindexBase(name);
      message.success(t("knowledge_page.reindex_started"));
      await refresh();
    } catch (err) {
      message.error(errMessage(err));
    } finally {
      setReindexing(false);
    }
  }, [name, t, message, refresh]);

  const activeTab = tab ?? "documents";

  if (!name) {
    return <Empty description="Missing :name in URL" style={{ marginTop: 80 }} />;
  }

  if (loading) {
    return (
      <div>
        <Skeleton.Input active size="large" style={{ marginBottom: 16 }} />
        <Skeleton active paragraph={{ rows: 6 }} />
      </div>
    );
  }

  if (error !== null || base === null) {
    return (
      <Alert
        type="error"
        showIcon
        message={t("knowledge_page.failed_to_load_base")}
        description={error ?? "not found"}
        data-testid="knowledge-detail-error"
        style={{ marginTop: 16 }}
      />
    );
  }

  const embedding = base.embedding_model ?? t("knowledge_page.embedding_unset");

  return (
    <div data-testid="knowledge-detail-root">
      <PageHeader
        title={base.name}
        icon={<BookOpen size={20} strokeWidth={1.5} />}
        backTo={{ label: t("nav.knowledge"), to: "/knowledge" }}
        subtitle={
          <Space size={12} align="center" wrap>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t("knowledge_page.stat_documents")}: {base.stats?.document_count ?? 0}
            </Text>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t("knowledge_page.stat_chunks")}: {base.stats?.chunk_count ?? 0}
            </Text>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t("knowledge_page.stat_embedding")}: <Text code>{embedding}</Text>
            </Text>
            {base.reindexing && (
              <Tag color="processing" bordered={false}>
                {t("knowledge_page.reindexing_tag")}
              </Tag>
            )}
          </Space>
        }
        actions={
          <Button
            icon={<RefreshCcw size={14} strokeWidth={1.5} />}
            onClick={() => void refresh()}
            aria-label={t("common.refresh")}
            data-testid="knowledge-detail-refresh"
          >
            {t("common.refresh")}
          </Button>
        }
      />

      {base.needs_reindex && !base.reindexing && (
        <Alert
          type="warning"
          showIcon
          message={t("knowledge_page.needs_reindex_banner")}
          style={{ marginBottom: 16 }}
          data-testid="knowledge-needs-reindex"
          action={
            <Tooltip title={isTenantSwitched ? t("common.tenant_switched_readonly") : undefined}>
              <Button
                size="small"
                type="primary"
                loading={reindexing}
                disabled={isTenantSwitched}
                onClick={() => void handleReindex()}
                data-testid="knowledge-reindex-btn"
              >
                {t("knowledge_page.reindex_button")}
              </Button>
            </Tooltip>
          }
        />
      )}

      <Tabs
        activeKey={activeTab}
        onChange={(k) => navigate(`/knowledge/${encodeURIComponent(name)}/${k}`)}
        items={[
          { key: "documents", label: t("knowledge_page.tab_documents") },
          { key: "test", label: t("knowledge_page.tab_test") },
          { key: "settings", label: t("knowledge_page.tab_settings") },
        ]}
      />

      {activeTab === "documents" && <DocumentsTab baseName={name} />}
      {activeTab === "test" && <RetrievalTestTab base={base} />}
      {activeTab === "settings" && <SettingsTab base={base} onSaved={refresh} />}
    </div>
  );
}
