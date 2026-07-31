/**
 * Retrieval test ("hit testing") tab — KB commercial uplift.
 *
 * Runs a query through the live retrieval pipeline and shows the ranked chunks
 * with their vector similarity score + which recall path surfaced each. The
 * controls (top-k / method / threshold / rerank) seed from the base's defaults
 * and let an operator probe how tuning changes recall.
 */
import { useCallback, useState } from "react";
import {
  App,
  Button,
  Card,
  Empty,
  Input,
  InputNumber,
  Progress,
  Segmented,
  Space,
  Switch,
  Tag,
  Typography,
} from "antd";
import { Search } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  testRetrieval,
  type KnowledgeBase,
  type RetrievalMethod,
  type RetrievalTestResult,
} from "../../api/knowledge";
import { ApiError } from "../../api/client";
import { concreteTenantScope, useTenantScope } from "../../tenant/TenantScopeContext";

const { Text, Paragraph } = Typography;

function errMessage(err: unknown): string {
  return err instanceof ApiError
    ? `${err.code}: ${err.message}`
    : err instanceof Error
      ? err.message
      : "unknown error";
}

const RECALL_COLOR: Record<string, string> = {
  vector: "cyan",
  keyword: "gold",
  both: "green",
};

export function RetrievalTestTab({ base }: { base: KnowledgeBase }) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  // Cross-tenant W3 — read-only probe on the switched-in tenant's base.
  const { apiTenantScope } = useTenantScope();

  const config = base.retrieval_config;
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState<number>(config?.top_k ?? 5);
  const [method, setMethod] = useState<RetrievalMethod>(config?.method ?? "hybrid");
  const [threshold, setThreshold] = useState<number | null>(config?.score_threshold ?? null);
  const [rerank, setRerank] = useState<boolean>(config?.rerank_enabled ?? true);

  const [results, setResults] = useState<RetrievalTestResult[] | null>(null);
  const [running, setRunning] = useState(false);

  const run = useCallback(async () => {
    if (!query.trim()) return;
    setRunning(true);
    try {
      const response = await testRetrieval(
        base.name,
        {
          query: query.trim(),
          top_k: topK,
          method,
          score_threshold: threshold,
          rerank,
        },
        concreteTenantScope(apiTenantScope),
      );
      setResults(response.results);
    } catch (err) {
      const detail =
        err instanceof ApiError && err.status === 503
          ? t("knowledge_page.embedder_missing")
          : errMessage(err);
      message.error(detail);
    } finally {
      setRunning(false);
    }
  }, [base.name, query, topK, method, threshold, rerank, t, message, apiTenantScope]);

  return (
    <div data-testid="knowledge-test-tab">
      <Space.Compact style={{ width: "100%", marginBottom: 12 }}>
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onPressEnter={() => void run()}
          placeholder={t("knowledge_page.test_query_placeholder")}
          aria-label={t("knowledge_page.test_title")}
          data-testid="kb-test-query"
        />
        <Button
          type="primary"
          icon={<Search size={14} strokeWidth={1.5} />}
          loading={running}
          onClick={() => void run()}
          data-testid="kb-test-run"
        >
          {t("knowledge_page.test_run")}
        </Button>
      </Space.Compact>

      <Space size={16} wrap style={{ marginBottom: 16 }}>
        <Space size={6}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t("knowledge_page.test_control_method")}
          </Text>
          <Segmented<RetrievalMethod>
            size="small"
            value={method}
            onChange={(v) => setMethod(v)}
            options={[
              { value: "hybrid", label: t("knowledge_page.method_hybrid") },
              { value: "vector", label: t("knowledge_page.method_vector") },
              { value: "keyword", label: t("knowledge_page.method_keyword") },
            ]}
          />
        </Space>
        <Space size={6}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t("knowledge_page.test_control_top_k")}
          </Text>
          <InputNumber
            size="small"
            min={1}
            max={50}
            value={topK}
            onChange={(v) => setTopK(v ?? 5)}
            aria-label={t("knowledge_page.test_control_top_k")}
          />
        </Space>
        <Space size={6}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t("knowledge_page.test_control_threshold")}
          </Text>
          <InputNumber
            size="small"
            min={0}
            max={1}
            step={0.05}
            value={threshold}
            onChange={(v) => setThreshold(v)}
            aria-label={t("knowledge_page.test_control_threshold")}
          />
        </Space>
        <Space size={6}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t("knowledge_page.test_control_rerank")}
          </Text>
          <Switch
            size="small"
            checked={rerank}
            onChange={setRerank}
            aria-label={t("knowledge_page.test_control_rerank")}
          />
        </Space>
      </Space>

      {results === null ? (
        <Empty description={t("knowledge_page.test_empty_hint")} />
      ) : results.length === 0 ? (
        <Empty description={t("knowledge_page.test_no_results")} />
      ) : (
        <div data-testid="kb-test-results">
          {results.map((result, idx) => (
            <Card key={`${result.source}-${idx}`} size="small" style={{ marginBottom: 12 }}>
              <Space
                align="center"
                style={{ justifyContent: "space-between", width: "100%", marginBottom: 8 }}
              >
                <Space size={8}>
                  <Text code style={{ fontSize: 12 }}>
                    {result.source}
                  </Text>
                  {result.recall_source && (
                    <Tag color={RECALL_COLOR[result.recall_source] ?? "default"} bordered={false}>
                      {t(`knowledge_page.recall_${result.recall_source}`)}
                    </Tag>
                  )}
                </Space>
                {result.score !== null && (
                  <Progress
                    type="line"
                    percent={Math.round(result.score * 100)}
                    size="small"
                    style={{ width: 120 }}
                  />
                )}
              </Space>
              <Paragraph
                style={{ whiteSpace: "pre-wrap", marginBottom: 0, fontSize: 13 }}
                ellipsis={{ rows: 4, expandable: true }}
              >
                {result.content}
              </Paragraph>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
