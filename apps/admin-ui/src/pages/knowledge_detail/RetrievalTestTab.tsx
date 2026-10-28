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
 *
 * B-175 P2-PR4 (B-206 §6): each result shows its document and the best hit
 * chunk's four scores; the tab says how long the search took, whether it was
 * saved to the retrieval records, and how it was degraded; results can be
 * narrowed to one document; a rerank floor can be tried for this search
 * (calibration); and the parameters can be saved as the base's defaults.
 */
import { useCallback, useMemo, useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  Empty,
  Input,
  InputNumber,
  Segmented,
  Select,
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
import { documentLabel, HitDocument } from "./HitDocument";
import { SaveDefaultsButton } from "./SaveDefaultsButton";
import { ScoreBreakdown } from "./ScoreBreakdown";
import { SearchNotices } from "./SearchNotices";
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

/** Above this many results the recall pool grows past an agent's default
 *  (``max(50, min(2 × limit, 100))`` candidates), so rerank scores — relative
 *  within one request — stop being comparable: calibrate at or below it. */
const CALIBRATION_MAX_TOP_K = 25;

/** The filter's key for a result whose document is unknown (deleted, or an
 *  older backend). */
const NO_DOCUMENT = "";

function documentKey(result: RetrievalTestResult): string {
  return result.document?.id ?? NO_DOCUMENT;
}

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
   *  KnowledgeDetail 统一下传。检索本身是只读探针;唯一的写控件「存为库默认参数」
   *  按 ``readonly`` 置灰。 */
  readScope: string | undefined;
  /** B-175 P2-PR4 — switched-in or foreign deep-link read: 「存为库默认参数」
   *  would write the same-named base of the caller's own tenant, so it is off. */
  readonly?: boolean;
  /** The base as 「存为库默认参数」 saved it (the page shows it without a reload). */
  onBaseUpdated?: (next: KnowledgeBase) => void;
  /** Open the retrieval records tab (the link renders only when given). */
  onOpenLogs?: () => void;
}

export function RetrievalTestTab({
  base,
  readScope,
  readonly = false,
  onBaseUpdated,
  onOpenLogs,
}: RetrievalTestTabProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();

  const config = base.retrieval_config;
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState<number>(config?.top_k ?? 5);
  const [method, setMethod] = useState<RetrievalMethod>(config?.method ?? "hybrid");
  const [threshold, setThreshold] = useState<number | null>(config?.score_threshold ?? null);
  const [rerank, setRerank] = useState<boolean>(config?.rerank_enabled ?? true);
  // B-175 P2-PR4 — a rerank floor for this search only; null = the base's own applies.
  const [rerankThreshold, setRerankThreshold] = useState<number | null>(null);
  const [lastResponse, setLastResponse] = useState<RetrievalTestResponse | null>(null);
  const [docFilter, setDocFilter] = useState<string | null>(null);

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
          ...(rerankThreshold !== null ? { rerank_score_threshold: rerankThreshold } : {}),
        },
        readScope,
      );
      setResults(response.results);
      setRerankFallback(rerankFallbackOf(response.rerank_outcome));
      setRerankFloor(response);
      setLastResponse(response);
      setDocFilter(null);
    } catch (err) {
      const detail =
        err instanceof ApiError && err.status === 503
          ? t("knowledge_page.embedder_missing")
          : errMessage(err);
      message.error(detail);
    } finally {
      setRunning(false);
    }
  }, [base.name, query, topK, method, threshold, rerank, rerankThreshold, t, message, readScope]);

  // The documents among the results, for the per-document filter (B-206 §6).
  const documents = useMemo(() => {
    const seen = new Map<string, string>();
    for (const result of results ?? []) {
      const key = documentKey(result);
      if (!seen.has(key)) {
        seen.set(
          key,
          result.document
            ? documentLabel(result.document)
            : result.document === null
              ? t("knowledge_retrieval.doc_deleted")
              : result.filename,
        );
      }
    }
    return [...seen.entries()].map(([value, label]) => ({ value, label }));
  }, [results, t]);

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
        <Space size={6}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t("knowledge_page.field_rerank_threshold")}
          </Text>
          <InputNumber
            size="small"
            min={0}
            max={1}
            step={0.05}
            value={rerankThreshold}
            onChange={(v) => setRerankThreshold(v)}
            placeholder={t("knowledge_retrieval.rerank_threshold_placeholder", {
              value:
                config?.rerank_score_threshold ?? t("knowledge_retrieval.rerank_threshold_none"),
            })}
            style={{ width: 170 }}
            aria-label={t("knowledge_page.field_rerank_threshold")}
            data-testid="kb-test-rerank-threshold"
          />
        </Space>
        <SaveDefaultsButton
          base={base}
          params={{ topK, method, threshold, rerank, rerankThreshold }}
          readonly={readonly}
          onSaved={onBaseUpdated}
        />
      </Space>

      {rerankThreshold !== null && topK > CALIBRATION_MAX_TOP_K && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={t("knowledge_retrieval.calibration_warning")}
          data-testid="kb-test-calibration-warning"
        />
      )}
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
      {results !== null && lastResponse !== null && (
        <>
          <SearchNotices
            prefix="kb-test"
            flags={{
              embedding_degraded: lastResponse.embedding_degraded,
              keyword_degraded: lastResponse.keyword_degraded,
              duplicates_dropped: lastResponse.duplicates_dropped,
              rerank_dropped: lastResponse.rerank_dropped,
            }}
          />
          <SearchMeta response={lastResponse} onOpenLogs={onOpenLogs} />
        </>
      )}
      {documents.length > 1 && (
        <div style={{ marginBottom: 12 }}>
          <Space size={6} data-testid="kb-test-doc-filter">
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t("knowledge_retrieval.doc_filter")}
            </Text>
            <Select<string>
              size="small"
              value={docFilter ?? undefined}
              onChange={(v) => setDocFilter(v ?? null)}
              allowClear
              placeholder={t("knowledge_retrieval.doc_filter_all")}
              options={documents}
              style={{ minWidth: 220 }}
              aria-label={t("knowledge_retrieval.doc_filter")}
            />
          </Space>
        </div>
      )}
      {results === null ? (
        <Empty description={t("knowledge_page.test_empty_hint")} />
      ) : results.length === 0 ? (
        lastResponse?.status === "below_threshold" ? (
          <Empty
            description={
              <span data-testid="kb-test-below-threshold">
                {t("knowledge_retrieval.below_threshold", {
                  candidates: lastResponse.candidates ?? 0,
                })}
              </span>
            }
          />
        ) : (
          <Empty description={t("knowledge_page.test_no_results")} />
        )
      ) : (
        <div data-testid="kb-test-results">
          {results.map((result, idx) =>
            docFilter !== null && documentKey(result) !== docFilter ? null : (
              <Card key={`${result.source}-${idx}`} size="small" style={{ marginBottom: 12 }}>
                <Space size={8} wrap style={{ marginBottom: 4 }}>
                  <Text code style={{ fontSize: 12 }}>
                    {result.source}
                  </Text>
                  {result.recall_source && (
                    <Tag color={RECALL_COLOR[result.recall_source] ?? "default"} bordered={false}>
                      {t(`knowledge_page.recall_${result.recall_source}`)}
                    </Tag>
                  )}
                  <HitDocument document={result.document} testId={`kb-test-doc-${idx}`} />
                </Space>
                <div style={{ marginBottom: 8 }}>
                  <ScoreBreakdown
                    testId={`kb-test-scores-${idx}`}
                    scores={{
                      // An older backend sends only ``score`` (the vector one).
                      vector_score: result.vector_score ?? result.score,
                      keyword_score: result.keyword_score,
                      fused_score: result.fused_score,
                      rerank_score: result.rerank_score,
                    }}
                  />
                </div>
                <ResultBody result={result} idx={idx} />
              </Card>
            ),
          )}
        </div>
      )}
    </div>
  );
}

/** 「用时 N ms · 已记入检索记录」 — B-175 P2-PR4. */
function SearchMeta({
  response,
  onOpenLogs,
}: {
  response: RetrievalTestResponse;
  onOpenLogs?: () => void;
}) {
  const { t } = useTranslation();
  if (response.elapsed_ms === undefined && response.log_id === undefined) return null;
  return (
    <Space size={8} wrap style={{ display: "flex", marginBottom: 12 }}>
      {response.elapsed_ms !== undefined && (
        <Text type="secondary" style={{ fontSize: 12 }} data-testid="kb-test-elapsed">
          {t("knowledge_retrieval.elapsed", { ms: response.elapsed_ms })}
        </Text>
      )}
      {response.log_id ? (
        <Text type="secondary" style={{ fontSize: 12 }} data-testid="kb-test-logged">
          {t("knowledge_retrieval.logged")}
        </Text>
      ) : (
        response.log_id === null && (
          <Text type="warning" style={{ fontSize: 12 }} data-testid="kb-test-not-logged">
            {t("knowledge_retrieval.not_logged")}
          </Text>
        )
      )}
      {response.log_id && onOpenLogs && (
        <Button
          type="link"
          size="small"
          style={{ padding: 0, fontSize: 12 }}
          onClick={onOpenLogs}
          data-testid="kb-test-open-logs"
        >
          {t("knowledge_retrieval.open_logs")}
        </Button>
      )}
    </Space>
  );
}
