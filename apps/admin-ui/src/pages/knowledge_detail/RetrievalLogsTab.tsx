/**
 * A base's retrieval records — B-175 P2-PR4 (master plan step 5; B-175 v2 §7
 * 「查看线上检索记录」).
 *
 * Every search of the base — an agent's ``knowledge_search`` or a console
 * retrieval test — leaves a record: the question, the parameters, how it
 * ended, each hit's ids and scores (never document text). One server page at
 * a time, newest first, filtered by source / outcome / time; a row opens the
 * record (``RetrievalLogDrawer``). Readable by anyone who can read
 * conversation records (``session:read``, viewers too). Records are read for
 * one concrete tenant: the page's read scope (``?tenant_id=`` or the switched
 * tenant; the "*" view reads the home tenant) — never "*", which the API
 * refuses.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, DatePicker, Empty, Select, Skeleton, Space, Table, Tooltip, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { Dayjs } from "dayjs";
import { useTranslation } from "react-i18next";

import { ApiError } from "../../api/client";
import {
  listRetrievalLogs,
  RETRIEVAL_LOG_RETENTION_DAYS,
  type RetrievalLogFilters,
  type RetrievalLogSource,
  type RetrievalLogsPage,
  type RetrievalLogSummary,
} from "../../api/knowledge_logs";
import { TABLE_PAGINATION, TABLE_PAGE_SIZE } from "../../utils/pagination";
import { LogSourceTag, LogStatusTag } from "./logTags";
import { RetrievalLogDrawer } from "./RetrievalLogDrawer";

const { Text } = Typography;

/** The outcome filter: zero hits is its own query flag, the rest a status. */
type Outcome = "all" | "zero_hit" | "ok" | "below_threshold" | "error";
type TimeRange = [Dayjs | null, Dayjs | null] | null;

function outcomeFilter(outcome: Outcome): RetrievalLogFilters {
  if (outcome === "all") return {};
  if (outcome === "zero_hit") return { zeroHit: true };
  return { status: outcome };
}

/** A picked range as the API's bounds: ``since`` inclusive, ``until`` exclusive. */
export function timeRangeFilter(range: TimeRange): Pick<RetrievalLogFilters, "since" | "until"> {
  const bounds: Pick<RetrievalLogFilters, "since" | "until"> = {};
  if (range?.[0]) bounds.since = range[0].toISOString();
  if (range?.[1]) bounds.until = range[1].toISOString();
  return bounds;
}

function errMessage(err: unknown): string {
  return err instanceof ApiError
    ? `${err.code}: ${err.message}`
    : err instanceof Error
      ? err.message
      : "unknown error";
}

interface RetrievalLogsTabProps {
  baseName: string;
  /** The page's read scope (a concrete tenant, or none for home). */
  readScope: string | undefined;
}

export function RetrievalLogsTab({ baseName, readScope }: RetrievalLogsTabProps) {
  const { t } = useTranslation();
  const [source, setSource] = useState<RetrievalLogSource | undefined>(undefined);
  const [outcome, setOutcome] = useState<Outcome>("all");
  const [range, setRange] = useState<TimeRange>(null);
  const [page, setPage] = useState(1);
  const [reload, setReload] = useState(0);
  const [data, setData] = useState<RetrievalLogsPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [selected, setSelected] = useState<string | null>(null);

  const filtered = source !== undefined || outcome !== "all" || range !== null;
  const filters: RetrievalLogFilters = {
    ...(source ? { source } : {}),
    ...outcomeFilter(outcome),
    ...timeRangeFilter(range),
  };
  const filtersKey = JSON.stringify(filters);

  // Only the latest read may land: a slow reply for an older filter or page
  // must not replace a newer one.
  const readSeq = useRef(0);
  useEffect(() => {
    const seq = ++readSeq.current;
    setLoading(true);
    setError(null);
    listRetrievalLogs(
      baseName,
      JSON.parse(filtersKey) as RetrievalLogFilters,
      { limit: TABLE_PAGE_SIZE, offset: (page - 1) * TABLE_PAGE_SIZE },
      readScope,
    ).then(
      (next) => {
        if (seq !== readSeq.current) return;
        setData(next);
        setLoading(false);
      },
      (err: unknown) => {
        if (seq !== readSeq.current) return;
        setError(err);
        setLoading(false);
      },
    );
  }, [baseName, readScope, filtersKey, page, reload]);

  /** A filter change starts over at page 1. */
  const refilter = useCallback(<T,>(set: (value: T) => void) => {
    return (value: T) => {
      set(value);
      setPage(1);
    };
  }, []);

  const clearFilters = () => {
    setSource(undefined);
    setOutcome("all");
    setRange(null);
    setPage(1);
  };

  const columns: ColumnsType<RetrievalLogSummary> = [
    {
      title: t("knowledge_retrieval.col_time"),
      dataIndex: "created_at",
      width: 180,
      render: (iso: string) => new Date(iso).toLocaleString(),
    },
    {
      title: t("knowledge_retrieval.col_source"),
      dataIndex: "source",
      width: 110,
      render: (value: string) => <LogSourceTag source={value} />,
    },
    {
      title: t("knowledge_retrieval.col_query"),
      dataIndex: "query",
      ellipsis: { showTitle: false },
      render: (query: string) => (
        <Tooltip title={query} placement="topLeft">
          <span>{query}</span>
        </Tooltip>
      ),
    },
    {
      title: t("knowledge_retrieval.col_status"),
      dataIndex: "status",
      width: 130,
      render: (value: string) => <LogStatusTag status={value} />,
    },
    {
      title: t("knowledge_retrieval.col_results"),
      dataIndex: "result_count",
      width: 80,
      align: "right",
    },
    {
      title: t("knowledge_retrieval.col_latency"),
      dataIndex: "latency_ms",
      width: 90,
      align: "right",
      render: (ms: number) => t("knowledge_retrieval.latency_ms", { ms }),
    },
    {
      title: t("knowledge_retrieval.col_actions"),
      key: "actions",
      width: 80,
      render: (_: unknown, record) => (
        <Button
          type="link"
          size="small"
          style={{ padding: 0 }}
          onClick={(event) => {
            event.stopPropagation();
            setSelected(record.id);
          }}
          data-testid={`kb-logs-view-${record.id}`}
        >
          {t("knowledge_retrieval.view")}
        </Button>
      ),
    },
  ];

  return (
    <div data-testid="knowledge-logs-tab">
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message={t("knowledge_retrieval.retention_notice", { days: RETRIEVAL_LOG_RETENTION_DAYS })}
        data-testid="kb-logs-retention"
      />

      <Space size={12} wrap style={{ marginBottom: 12 }}>
        <span data-testid="kb-logs-filter-source">
          <Select<RetrievalLogSource | "all">
            value={source ?? "all"}
            onChange={refilter((v: RetrievalLogSource | "all") =>
              setSource(v === "all" ? undefined : v),
            )}
            style={{ width: 150 }}
            aria-label={t("knowledge_retrieval.filter_source")}
            options={[
              { value: "all", label: t("knowledge_retrieval.source_all") },
              { value: "agent", label: t("knowledge_retrieval.source_agent") },
              { value: "console_test", label: t("knowledge_retrieval.source_console_test") },
            ]}
          />
        </span>
        <span data-testid="kb-logs-filter-outcome">
          <Select<Outcome>
            value={outcome}
            onChange={refilter(setOutcome)}
            style={{ width: 190 }}
            aria-label={t("knowledge_retrieval.filter_outcome")}
            options={[
              { value: "all", label: t("knowledge_retrieval.outcome_all") },
              { value: "zero_hit", label: t("knowledge_retrieval.outcome_zero_hit") },
              { value: "ok", label: t("knowledge_retrieval.outcome_ok") },
              { value: "below_threshold", label: t("knowledge_retrieval.outcome_below_threshold") },
              { value: "error", label: t("knowledge_retrieval.outcome_error") },
            ]}
          />
        </span>
        <DatePicker.RangePicker
          showTime
          value={range}
          onChange={refilter((v: TimeRange) => setRange(v))}
          aria-label={t("knowledge_retrieval.filter_time")}
          data-testid="kb-logs-filter-time"
        />
        {filtered && (
          <Button size="small" onClick={clearFilters} data-testid="kb-logs-clear-filters">
            {t("knowledge_retrieval.clear_filters")}
          </Button>
        )}
      </Space>

      {error !== null ? (
        <Alert
          type="error"
          showIcon
          message={t("knowledge_retrieval.load_failed")}
          description={
            error instanceof ApiError && error.status === 403
              ? t("knowledge_retrieval.no_permission")
              : errMessage(error)
          }
          action={
            <Button size="small" onClick={() => setReload((n) => n + 1)} data-testid="kb-logs-retry">
              {t("knowledge_retrieval.retry")}
            </Button>
          }
          data-testid="kb-logs-error"
        />
      ) : data === null ? (
        <Skeleton active paragraph={{ rows: 6 }} />
      ) : (
        <div data-testid="kb-logs-table">
          <Table<RetrievalLogSummary>
            rowKey="id"
            size="small"
            columns={columns}
            dataSource={data.logs}
            loading={loading}
            onRow={(record) => ({
              onClick: () => setSelected(record.id),
              style: { cursor: "pointer" },
            })}
            pagination={{
              ...TABLE_PAGINATION,
              current: page,
              total: data.total,
              onChange: setPage,
              showTotal: (total) => t("knowledge_retrieval.pager_total", { total }),
            }}
            locale={{
              emptyText: (
                <Empty
                  description={
                    <Text type="secondary" data-testid="kb-logs-empty">
                      {filtered
                        ? t("knowledge_retrieval.empty_filtered")
                        : t("knowledge_retrieval.empty", { days: RETRIEVAL_LOG_RETENTION_DAYS })}
                    </Text>
                  }
                />
              ),
            }}
          />
        </div>
      )}

      <RetrievalLogDrawer
        baseName={baseName}
        logId={selected}
        readScope={readScope}
        onClose={() => setSelected(null)}
      />
    </div>
  );
}
