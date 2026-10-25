/**
 * Retrieval test ("hit testing") tab — KB commercial uplift.
 *
 * Runs a query through the live retrieval pipeline and shows the ranked chunks
 * with their vector similarity score + which recall path surfaced each. The
 * controls (top-k / method / threshold / rerank) seed from the base's defaults
 * and let an operator probe how tuning changes recall.
 *
 * B-206 K-1: a result can be a parent row (「整块」) of its hit chunks (「小段」):
 * the whole parent is shown, its best hit chunk highlighted where its text
 * appears verbatim, or shown on its own above the parent where it does not.
 */
import { useCallback, useState } from "react";
import {
  Alert,
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
  type RetrievalTestResponse,
  type RetrievalTestResult,
} from "../../api/knowledge";
import { ApiError } from "../../api/client";
import {
  rerankFallbackOf,
  rerankNoticeTone,
  type RerankFallback,
} from "../../api/rerank_outcome";
import { HeadingPath } from "./HeadingPath";
import { thresholdPausedNotice } from "./thresholdPausedNotice";

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

const BODY_STYLE = { whiteSpace: "pre-wrap", marginBottom: 0, fontSize: 13 } as const;

/** A result's text. Without a parent row it is the hit chunk, shown as before;
 *  with one it is the whole parent, under a line naming the parent and its hit
 *  chunks. */
function ResultBody({ result, idx }: { result: RetrievalTestResult; idx: number }) {
  const { t } = useTranslation();
  const parentIndex = result.parent_index ?? null;
  if (parentIndex === null) {
    return (
      <>
        <HeadingPath path={result.heading_path} testId={`kb-test-heading-${idx}`} />
        <Paragraph style={BODY_STYLE} ellipsis={{ rows: 4, expandable: true }}>
          {result.content}
        </Paragraph>
      </>
    );
  }
  const hits = result.hit_chunk_indexes ?? [result.chunk_index];
  const hit = result.hit_content ?? null;
  const at = hit === null ? -1 : result.content.indexOf(hit);
  return (
    <>
      <Text
        type="secondary"
        style={{ display: "block", fontSize: 12, marginBottom: 4 }}
        data-testid={`kb-test-parent-${idx}`}
      >
        {t("knowledge_page.test_parent_hits", {
          count: hits.length,
          parent: parentIndex,
          hits: hits.map((index) => `#${index}`).join(t("knowledge_page.test_hit_separator")),
        })}
      </Text>
      <HeadingPath path={result.heading_path} testId={`kb-test-heading-${idx}`} />
      {hit !== null && at < 0 && (
        <div
          style={{
            border: "1px solid var(--ew-border-subtle)",
            borderRadius: 6,
            padding: "6px 10px",
            marginBottom: 8,
          }}
          data-testid={`kb-test-hit-${idx}`}
        >
          <Text type="secondary" style={{ display: "block", fontSize: 12 }}>
            {t("knowledge_page.test_hit_chunk")}
          </Text>
          <Paragraph style={BODY_STYLE}>{hit}</Paragraph>
        </div>
      )}
      <Paragraph style={BODY_STYLE}>
        {hit !== null && at >= 0 ? (
          <>
            {result.content.slice(0, at)}
            <mark
              style={{ background: "var(--ew-status-warning-bg)", color: "inherit", padding: 0 }}
              data-testid={`kb-test-hit-mark-${idx}`}
            >
              {hit}
            </mark>
            {result.content.slice(at + hit.length)}
          </>
        ) : (
          result.content
        )}
      </Paragraph>
    </>
  );
}

interface RetrievalTestTabProps {
  base: KnowledgeBase;
  /** Cross-tenant W4(review C-2)— 权威读口径:URL ``?tenant_id=`` 原样透
   *  传优先;无 URL 参数时取 ambient scope("*" 折叠成 undefined),由
   *  KnowledgeDetail 统一下传。检索测试是只读探针,无写控件。 */
  readScope: string | undefined;
}

export function RetrievalTestTab({ base, readScope }: RetrievalTestTabProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();

  const config = base.retrieval_config;
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState<number>(config?.top_k ?? 5);
  const [method, setMethod] = useState<RetrievalMethod>(config?.method ?? "hybrid");
  const [threshold, setThreshold] = useState<number | null>(config?.score_threshold ?? null);
  const [rerank, setRerank] = useState<boolean>(config?.rerank_enabled ?? true);

  const [results, setResults] = useState<RetrievalTestResult[] | null>(null);
  // B-206 K-7 —— 重排没定顺序(退回召回顺序)时的原因;重排正常 / 没开 → null。
  const [rerankFallback, setRerankFallback] = useState<RerankFallback | null>(null);
  // B-206 K-7b —— 这次检索没用库的重排分数下限:在别的重排模型下标定的(提示里写这次检索
  // 自己的两个模型名,不用页面早先读到的)。
  const [rerankFloor, setRerankFloor] = useState<RetrievalTestResponse | null>(null);
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
        readScope,
      );
      setResults(response.results);
      setRerankFallback(rerankFallbackOf(response.rerank_outcome));
      setRerankFloor(response);
    } catch (err) {
      const detail =
        err instanceof ApiError && err.status === 503
          ? t("knowledge_page.embedder_missing")
          : errMessage(err);
      message.error(detail);
    } finally {
      setRunning(false);
    }
  }, [base.name, query, topK, method, threshold, rerank, t, message, readScope]);

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

      {results !== null && rerankFallback !== null && (
        <Alert
          type={rerankNoticeTone(rerankFallback)}
          showIcon
          style={{ marginBottom: 12 }}
          message={t(`knowledge_page.rerank_outcome_${rerankFallback}`)}
          data-testid="kb-test-rerank-outcome"
        />
      )}
      {results !== null && rerankFloor?.rerank_threshold_paused === true && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={thresholdPausedNotice(
            t,
            rerankFloor.rerank_threshold_model,
            rerankFloor.rerank_model,
          )}
          data-testid="kb-test-rerank-threshold-paused"
        />
      )}
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
              <ResultBody result={result} idx={idx} />
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
