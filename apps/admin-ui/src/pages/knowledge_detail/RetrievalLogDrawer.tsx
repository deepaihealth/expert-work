/**
 * One retrieval record — B-175 P2-PR4.
 *
 * The record keeps ids and scores, never text: the drawer shows each result's
 * document as it is now (or that it was deleted), its passage and four
 * scores, and the passage text read live (``…/{id}/passages``) with the best
 * hit chunk marked — or that the passage is gone.
 *
 * An agent's record links to its conversation (``session:read``, the records'
 * own gate) and, for admins only, its run (run detail is owner-or-admin).
 * Both pages read the ambient tenant scope, so the links are withheld when
 * this base is read under another tenant's scope (the "*" view's row jump).
 */
import { useEffect, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import {
  Alert,
  Card,
  Descriptions,
  Drawer,
  Empty,
  Skeleton,
  Space,
  Tag,
  Typography,
} from "antd";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";

import { ApiError } from "../../api/client";
import {
  getRetrievalLog,
  getRetrievalLogPassages,
  type LivePassage,
  type RetrievalLogDetail,
  type RetrievalLogResult,
} from "../../api/knowledge_logs";
import { useAuth } from "../../auth/AuthContext";
import { concreteTenantScope, useTenantScope } from "../../tenant/TenantScopeContext";
import { HeadingPath } from "./HeadingPath";
import { HitDocument } from "./HitDocument";
import { LogSourceTag, LogStatusTag } from "./logTags";
import { ScoreBreakdown } from "./ScoreBreakdown";
import { SearchNotices } from "./SearchNotices";

const { Text, Paragraph } = Typography;

const TEXT_STYLE = { whiteSpace: "pre-wrap", marginBottom: 0, fontSize: 13 } as const;

function errMessage(err: unknown): string {
  return err instanceof ApiError
    ? `${err.code}: ${err.message}`
    : err instanceof Error
      ? err.message
      : "unknown error";
}

/** The live text is read once per record; ``undefined`` while reading. */
type LiveText = { passages: LivePassage[] } | { error: string } | undefined;

const PARAMS = [
  ["limit", "knowledge_page.field_top_k"],
  ["method", "knowledge_page.field_method"],
  ["score_threshold", "knowledge_page.field_threshold"],
  ["rerank", "knowledge_page.field_rerank"],
  ["rerank_score_threshold", "knowledge_page.field_rerank_threshold"],
] as const;

function paramText(t: TFunction, key: string, value: unknown): string {
  if (value === null || value === undefined) return t("knowledge_retrieval.param_default");
  if (typeof value === "boolean") {
    return t(value ? "knowledge_retrieval.value_on" : "knowledge_retrieval.value_off");
  }
  if (key === "method" && typeof value === "string") return t(`knowledge_page.method_${value}`);
  return String(value);
}

interface RetrievalLogDrawerProps {
  baseName: string;
  /** The record to show; ``null`` closes the drawer. */
  logId: string | null;
  /** The page's read scope (a concrete tenant, or none for home). */
  readScope: string | undefined;
  onClose: () => void;
}

export function RetrievalLogDrawer({ baseName, logId, readScope, onClose }: RetrievalLogDrawerProps) {
  const { t } = useTranslation();
  const [detail, setDetail] = useState<RetrievalLogDetail | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [live, setLive] = useState<LiveText>(undefined);
  // Only the latest record's reads may land (a quick switch between rows).
  const seq = useRef(0);

  useEffect(() => {
    const mine = ++seq.current;
    setDetail(null);
    setError(null);
    setLive(undefined);
    if (logId === null) return;
    getRetrievalLog(baseName, logId, readScope).then(
      (next) => mine === seq.current && setDetail(next),
      (err: unknown) => mine === seq.current && setError(err),
    );
    getRetrievalLogPassages(baseName, logId, readScope).then(
      (passages) => mine === seq.current && setLive({ passages }),
      (err: unknown) => mine === seq.current && setLive({ error: errMessage(err) }),
    );
  }, [baseName, logId, readScope]);

  return (
    <Drawer
      open={logId !== null}
      onClose={onClose}
      width={720}
      title={t("knowledge_retrieval.detail_title")}
    >
      <div data-testid="kb-log-drawer">
        {error !== null ? (
          error instanceof ApiError && error.status === 404 ? (
            <Alert
              type="info"
              showIcon
              message={t("knowledge_retrieval.detail_missing")}
              data-testid="kb-log-missing"
            />
          ) : (
            <Alert
              type="error"
              showIcon
              message={t("knowledge_retrieval.detail_failed")}
              description={errMessage(error)}
              data-testid="kb-log-failed"
            />
          )
        ) : detail === null ? (
          <Skeleton active paragraph={{ rows: 8 }} />
        ) : (
          <RecordBody baseName={baseName} detail={detail} live={live} readScope={readScope} />
        )}
      </div>
    </Drawer>
  );
}

function RecordBody({
  baseName,
  detail,
  live,
  readScope,
}: {
  baseName: string;
  detail: RetrievalLogDetail;
  live: LiveText;
  readScope: string | undefined;
}) {
  const { t } = useTranslation();
  const candidates = typeof detail.flags.candidates === "number" ? detail.flags.candidates : null;
  const rerankModel =
    typeof detail.flags.rerank_model === "string" ? detail.flags.rerank_model : null;
  return (
    <>
      <Descriptions column={1} size="small" style={{ marginBottom: 16 }}>
        <Descriptions.Item label={t("knowledge_retrieval.field_question")}>
          <Paragraph style={TEXT_STYLE} data-testid="kb-log-query">
            {detail.query}
          </Paragraph>
        </Descriptions.Item>
        <Descriptions.Item label={t("knowledge_retrieval.col_source")}>
          <LogSourceTag source={detail.source} />
        </Descriptions.Item>
        <Descriptions.Item label={t("knowledge_retrieval.col_status")}>
          <Space size={8}>
            <LogStatusTag status={detail.status} />
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t("knowledge_retrieval.col_results")}: {detail.result_count}
            </Text>
          </Space>
        </Descriptions.Item>
        <Descriptions.Item label={t("knowledge_retrieval.col_time")}>
          {new Date(detail.created_at).toLocaleString()} ·{" "}
          {t("knowledge_retrieval.latency_ms", { ms: detail.latency_ms })}
        </Descriptions.Item>
        {detail.actor_id && (
          <Descriptions.Item label={t("knowledge_retrieval.field_actor")}>
            <Text code data-testid="kb-log-actor">
              {detail.actor_id}
            </Text>
          </Descriptions.Item>
        )}
        {detail.thread_id && (
          <Descriptions.Item label={t("knowledge_retrieval.field_links")}>
            <RecordLinks
              baseName={baseName}
              threadId={detail.thread_id}
              runId={detail.run_id}
              readScope={readScope}
            />
          </Descriptions.Item>
        )}
      </Descriptions>

      <div data-testid="kb-log-params" style={{ marginBottom: 16 }}>
        <Descriptions title={t("knowledge_retrieval.field_params")} column={2} size="small">
          {PARAMS.map(([key, label]) => (
            <Descriptions.Item key={key} label={t(label)}>
              {paramText(t, key, detail.params[key])}
            </Descriptions.Item>
          ))}
        </Descriptions>
      </div>

      <div data-testid="kb-log-flags" style={{ marginBottom: 16 }}>
        <Text strong style={{ display: "block", marginBottom: 8 }}>
          {t("knowledge_retrieval.field_flags")}
        </Text>
        <Space size={12} wrap style={{ marginBottom: 8 }}>
          {candidates !== null && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t("knowledge_retrieval.flag_candidates", { count: candidates })}
            </Text>
          )}
          {rerankModel !== null && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t("knowledge_retrieval.flag_rerank_model", { model: rerankModel })}
            </Text>
          )}
        </Space>
        <SearchNotices prefix="kb-log" flags={detail.flags} />
        {detail.kb_ids.length > 1 && (
          <Alert
            type="info"
            showIcon
            message={t("knowledge_retrieval.multibase", { count: detail.kb_ids.length })}
            data-testid="kb-log-multibase"
          />
        )}
      </div>

      <Text strong style={{ display: "block", marginBottom: 8 }}>
        {t("knowledge_retrieval.results_title")}
      </Text>
      {detail.results.length === 0 ? (
        <Empty description={t("knowledge_retrieval.results_empty")} />
      ) : (
        <>
          <LiveTextStatus live={live} />
          {detail.results.map((result, idx) => (
            <ResultCard
              key={`${result.chunk_id ?? "none"}-${idx}`}
              result={result}
              idx={idx}
              live={live !== undefined && "passages" in live ? live.passages[idx] : undefined}
            />
          ))}
        </>
      )}
    </>
  );
}

function RecordLinks({
  baseName,
  threadId,
  runId,
  readScope,
}: {
  baseName: string;
  threadId: string;
  runId: string | null;
  readScope: string | undefined;
}) {
  const { t } = useTranslation();
  const location = useLocation();
  const { apiTenantScope } = useTenantScope();
  const { identity } = useAuth();
  const home = identity?.homeTenantId;
  // Conversation and run detail read the ambient scope: link only when that
  // is the tenant this base is read under.
  const reachable = (readScope ?? home) === (concreteTenantScope(apiTenantScope) ?? home);
  const isAdmin = (identity?.isSystemAdmin ?? false) || (identity?.roles.includes("admin") ?? false);
  if (!reachable) {
    return (
      <Text type="secondary" style={{ fontSize: 12 }} data-testid="kb-log-links-other-tenant">
        {t("knowledge_retrieval.links_other_tenant")}
      </Text>
    );
  }
  return (
    <Space size={12}>
      <Link
        to={`/conversations/${encodeURIComponent(threadId)}`}
        state={{ from: `${location.pathname}${location.search}`, fromLabel: baseName }}
        data-testid="kb-log-conversation-link"
      >
        {t("knowledge_retrieval.open_conversation")}
      </Link>
      {isAdmin && runId && (
        <Link
          to={`/runs/${encodeURIComponent(threadId)}/${encodeURIComponent(runId)}`}
          data-testid="kb-log-run-link"
        >
          {t("knowledge_retrieval.open_run")}
        </Link>
      )}
    </Space>
  );
}

function LiveTextStatus({ live }: { live: LiveText }) {
  const { t } = useTranslation();
  if (live === undefined) {
    return (
      <Text type="secondary" style={{ display: "block", fontSize: 12, marginBottom: 8 }}>
        {t("knowledge_retrieval.text_loading")}
      </Text>
    );
  }
  if ("error" in live) {
    return (
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 8 }}
        message={t("knowledge_retrieval.text_failed", { error: live.error })}
        data-testid="kb-log-text-failed"
      />
    );
  }
  return (
    <Text
      type="secondary"
      style={{ display: "block", fontSize: 12, marginBottom: 8 }}
      data-testid="kb-log-text-live-note"
    >
      {t("knowledge_retrieval.text_live_note")}
    </Text>
  );
}

function ResultCard({
  result,
  idx,
  live,
}: {
  result: RetrievalLogResult;
  idx: number;
  /** This result's live text; ``undefined`` while reading or when unreadable. */
  live: LivePassage | undefined;
}) {
  const { t } = useTranslation();
  const label =
    result.parent_index !== null && result.parent_chunk_id !== null
      ? t("knowledge_retrieval.passage_parent", {
          parent: result.parent_index,
          chunk: result.chunk_index ?? "?",
        })
      : t("knowledge_retrieval.passage_chunk", { chunk: result.chunk_index ?? "?" });
  return (
    <Card size="small" style={{ marginBottom: 12 }} data-testid={`kb-log-result-${idx}`}>
      <Space size={8} wrap style={{ marginBottom: 4 }}>
        <Text strong>#{idx + 1}</Text>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {label}
        </Text>
        {result.recall_source && (
          <Tag bordered={false}>{t(`knowledge_page.recall_${result.recall_source}`)}</Tag>
        )}
        <HitDocument document={result.document} testId={`kb-log-result-doc-${idx}`} />
      </Space>
      <div style={{ marginBottom: 8 }}>
        <ScoreBreakdown scores={result} testId={`kb-log-result-scores-${idx}`} />
      </div>
      {/* A deleted document has no text to read. */}
      {result.document !== null && live !== undefined && (
        <PassageText live={live} hasParent={result.parent_chunk_id !== null} idx={idx} />
      )}
    </Card>
  );
}

function PassageText({
  live,
  hasParent,
  idx,
}: {
  live: LivePassage;
  hasParent: boolean;
  idx: number;
}) {
  const { t } = useTranslation();
  const { passage, hit } = live;
  if (passage === null) {
    return (
      <Text type="secondary" style={{ fontSize: 12 }} data-testid={`kb-log-result-text-gone-${idx}`}>
        {t("knowledge_retrieval.text_gone")}
      </Text>
    );
  }
  const at = hit === null ? -1 : passage.content.indexOf(hit.content);
  return (
    <div data-testid={`kb-log-result-text-${idx}`}>
      {!passage.enabled && (
        <Tag color="default" data-testid={`kb-log-result-disabled-${idx}`}>
          {t("knowledge_retrieval.text_disabled")}
        </Tag>
      )}
      {hasParent && hit === null && (
        <Text
          type="secondary"
          style={{ display: "block", fontSize: 12, marginBottom: 4 }}
          data-testid={`kb-log-result-hit-gone-${idx}`}
        >
          {t("knowledge_retrieval.hit_gone")}
        </Text>
      )}
      <HeadingPath path={passage.heading_path} testId={`kb-log-result-heading-${idx}`} />
      <Paragraph style={TEXT_STYLE}>
        {hit !== null && at >= 0 ? (
          <>
            {passage.content.slice(0, at)}
            <mark
              style={{ background: "var(--ew-status-warning-bg)", color: "inherit", padding: 0 }}
              data-testid={`kb-log-result-mark-${idx}`}
            >
              {hit.content}
            </mark>
            {passage.content.slice(at + hit.content.length)}
          </>
        ) : (
          passage.content
        )}
      </Paragraph>
    </div>
  );
}
