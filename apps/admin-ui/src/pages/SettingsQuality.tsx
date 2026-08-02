/**
 * Quality dashboard — Stream RT-5 (RT-ADR-26), tenant-scoped.
 *
 * Reads the production-quality series + drift alerts the RT-5 sampler /
 * drift worker fill (``/v1/quality``). Three sections:
 *
 *   - per-agent trend: a sparkline + latest mean over the selected window;
 *   - low-score drill: the worst-scoring sampled runs, each linking to
 *     ``run_detail`` (the run / conversation) so an operator can research it;
 *   - drift alerts: raised ``quality.drift`` events (recent mean < baseline).
 *
 * Honest boundary (RT-ADR-23): ``overall`` is a subjective LLM-judge rubric
 * score, not ground truth — surfaced in the header so the dashboard is not
 * read as a correctness oracle.
 *
 * Mirrors the tenant-page pattern (``SettingsAudit`` / ``SettingsUsage``):
 * PageHeader + filter bar + antd Table + ``ApiError`` → ``${code}: ${message}``.
 */
import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  Alert,
  Empty,
  Input,
  Segmented,
  Skeleton,
  Table,
  Tag,
  Typography,
} from "antd";
import type { TableColumnsType } from "antd";
import { Activity } from "lucide-react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { useTenantScope } from "../tenant/TenantScopeContext";
import { PageHeader } from "../components/PageHeader";
import {
  listQualityDriftAlerts,
  listQualityScores,
  type QualityDriftAlert,
  type QualityScore,
} from "../api/quality";

const { Text } = Typography;

/** Window selector → lookback hours. */
const WINDOWS: Record<string, number> = { "24h": 24, "7d": 168, "30d": 720 };

/** 1-5 rubric → semantic color (worst red, mid gold, best green). */
function scoreColor(overall: number): string {
  if (overall <= 2) return "error";
  if (overall === 3) return "warning";
  return "success";
}

interface AgentTrend {
  agent: string;
  mean: number;
  count: number;
  /** Oldest → newest ``overall`` values for the sparkline. */
  series: number[];
}

/** Group the flat (newest-first) score list into a per-agent trend. */
function toTrends(scores: QualityScore[]): AgentTrend[] {
  const byAgent = new Map<string, QualityScore[]>();
  for (const s of scores) {
    const bucket = byAgent.get(s.agent_name);
    if (bucket) bucket.push(s);
    else byAgent.set(s.agent_name, [s]);
  }
  const trends: AgentTrend[] = [];
  for (const [agent, rows] of byAgent) {
    // rows are newest-first; the sparkline reads oldest → newest.
    const chrono = [...rows].reverse();
    const series = chrono.map((r) => r.overall);
    const mean = series.reduce((a, b) => a + b, 0) / series.length;
    trends.push({ agent, mean, count: series.length, series });
  }
  return trends.sort((a, b) => a.mean - b.mean); // worst mean first
}

/** Tiny dependency-free sparkline for a 1-5 score series. */
function Sparkline({ series }: { series: number[] }) {
  const w = 120;
  const h = 28;
  const pad = 2;
  if (series.length === 0)
    return <span style={{ color: "var(--ew-text-subtle)" }}>—</span>;
  // Map a clamped 1..5 score to bottom..top (clamp guards the viewport if the
  // rubric range ever changes; ``overall`` is 1-5 today).
  const yFor = (v: number) => {
    const cv = Math.max(1, Math.min(5, v));
    return h - pad - ((cv - 1) / 4) * (h - 2 * pad);
  };
  const points = series
    .map((v, i) => {
      const x =
        series.length === 1
          ? w / 2
          : pad + (i * (w - 2 * pad)) / (series.length - 1);
      return `${x.toFixed(1)},${yFor(v).toFixed(1)}`;
    })
    .join(" ");
  const last = series[series.length - 1];
  return (
    <svg
      width={w}
      height={h}
      role="img"
      aria-label={`trend ${series.join(",")}`}
    >
      <polyline
        points={points}
        fill="none"
        stroke="var(--ew-accent-cyan, #22d3ee)"
        strokeWidth={1.5}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      {series.length === 1 && (
        <circle
          cx={w / 2}
          cy={yFor(last)}
          r={2}
          fill="var(--ew-accent-cyan, #22d3ee)"
        />
      )}
    </svg>
  );
}

export function SettingsQuality() {
  const { t } = useTranslation();
  // Cross-tenant W3 — the lists are scope-aware ("*" aggregates every tenant).
  const { apiTenantScope } = useTenantScope();

  const [agentFilter, setAgentFilter] = useState("");
  // Named ``timeWindow`` (not ``window``) so it never shadows the DOM global.
  const [timeWindow, setTimeWindow] = useState<string>("7d");

  const [scores, setScores] = useState<QualityScore[]>([]);
  const [alerts, setAlerts] = useState<QualityDriftAlert[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    const agentName =
      agentFilter.trim().length > 0 ? agentFilter.trim() : undefined;
    try {
      const [scoreList, alertList] = await Promise.all([
        listQualityScores({ agentName, windowH: WINDOWS[timeWindow], tenantScope: apiTenantScope }),
        listQualityDriftAlerts({ agentName, windowH: WINDOWS[timeWindow], tenantScope: apiTenantScope }),
      ]);
      setScores(scoreList.items);
      setAlerts(alertList.items);
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
  }, [agentFilter, timeWindow, apiTenantScope]);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  const trends = useMemo(() => toTrends(scores), [scores]);
  const lowScores = useMemo(
    () => [...scores].sort((a, b) => a.overall - b.overall),
    [scores],
  );

  const trendColumns: TableColumnsType<AgentTrend> = [
    { title: t("quality_page.col_agent"), dataIndex: "agent", key: "agent" },
    {
      title: t("quality_page.trend_mean"),
      key: "mean",
      render: (_, r) => (
        <Tag color={scoreColor(Math.round(r.mean))}>{r.mean.toFixed(2)}</Tag>
      ),
    },
    {
      title: t("quality_page.trend_samples"),
      dataIndex: "count",
      key: "count",
      render: (n: number) => <Text type="secondary">{n}</Text>,
    },
    {
      title: t("quality_page.section_trend"),
      key: "spark",
      render: (_, r) => <Sparkline series={r.series} />,
    },
  ];

  const lowColumns: TableColumnsType<QualityScore> = [
    {
      title: t("quality_page.col_overall"),
      dataIndex: "overall",
      key: "overall",
      width: 80,
      render: (v: number) => <Tag color={scoreColor(v)}>{v}</Tag>,
    },
    {
      title: t("quality_page.col_agent"),
      dataIndex: "agent_name",
      key: "agent_name",
    },
    {
      title: t("quality_page.col_dimensions"),
      key: "dimensions",
      render: (_, r) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {Object.entries(r.dimensions)
            .map(([k, v]) => `${k}:${v}`)
            .join("  ")}
        </Text>
      ),
    },
    {
      title: t("quality_page.col_rationale"),
      dataIndex: "rationale",
      key: "rationale",
      ellipsis: true,
    },
    {
      title: t("quality_page.col_observed_at"),
      dataIndex: "observed_at",
      key: "observed_at",
      width: 180,
      render: (v: string | null) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {v !== null ? new Date(v).toLocaleString() : "—"}
        </Text>
      ),
    },
    {
      title: t("quality_page.col_run"),
      key: "run",
      width: 90,
      render: (_, r) => (
        <Link
          to={`/runs/${encodeURIComponent(r.thread_id)}/${encodeURIComponent(r.run_id)}`}
        >
          {t("quality_page.open_run")}
        </Link>
      ),
    },
  ];

  const driftColumns: TableColumnsType<QualityDriftAlert> = [
    {
      title: t("quality_page.col_agent"),
      dataIndex: "agent_name",
      key: "agent_name",
    },
    {
      title: t("quality_page.drift_col_recent"),
      dataIndex: "recent_mean",
      key: "recent_mean",
      render: (v: number) => (
        <Tag color={scoreColor(Math.round(v))}>{v.toFixed(2)}</Tag>
      ),
    },
    {
      title: t("quality_page.drift_col_baseline"),
      dataIndex: "baseline_mean",
      key: "baseline_mean",
      render: (v: number) => v.toFixed(2),
    },
    {
      title: t("quality_page.drift_col_drift_pct"),
      dataIndex: "drift_pct",
      key: "drift_pct",
      render: (v: number) => <Tag color="error">-{(v * 100).toFixed(1)}%</Tag>,
    },
    {
      title: t("quality_page.drift_col_samples"),
      key: "samples",
      render: (_, r) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {r.recent_count} / {r.baseline_count}
        </Text>
      ),
    },
    {
      title: t("quality_page.drift_col_detected_at"),
      dataIndex: "detected_at",
      key: "detected_at",
      width: 180,
      render: (v: string | null) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {v !== null ? new Date(v).toLocaleString() : "—"}
        </Text>
      ),
    },
  ];

  return (
    <div data-testid="quality-root">
      <PageHeader
        icon={<Activity size={18} strokeWidth={1.5} />}
        title={t("quality_page.page_title")}
        subtitle={t("quality_page.subtitle")}
      />

      <Alert
        type="info"
        showIcon
        message={t("quality_page.honest_note")}
        style={{ marginBottom: 16 }}
        data-testid="quality-honest-note"
      />

      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: 8,
          marginBottom: 16,
          padding: 12,
          background: "var(--ew-surface-raised)",
          borderRadius: 6,
          border: "1px solid var(--ew-border-subtle)",
        }}
        data-testid="quality-filters"
      >
        <Input
          placeholder={t("quality_page.filter_agent")}
          value={agentFilter}
          onChange={(e) => setAgentFilter(e.target.value)}
          onPressEnter={fetchAll}
          style={{ width: 220 }}
          allowClear
          aria-label={t("quality_page.filter_agent")}
          data-testid="quality-agent-filter"
        />
        <Segmented<string>
          value={timeWindow}
          onChange={(v) => setTimeWindow(v)}
          options={[
            { value: "24h", label: t("quality_page.window_24h") },
            { value: "7d", label: t("quality_page.window_7d") },
            { value: "30d", label: t("quality_page.window_30d") },
          ]}
          aria-label={t("quality_page.filter_window")}
          data-testid="quality-window"
        />
      </div>

      {error !== null && (
        <Alert
          type="error"
          showIcon
          message={t("quality_page.failed_to_load")}
          description={error}
          style={{ marginBottom: 12 }}
          data-testid="quality-error"
        />
      )}

      {loading && scores.length === 0 && alerts.length === 0 ? (
        <Skeleton active paragraph={{ rows: 6 }} />
      ) : (
        <>
          <SectionTitle>{t("quality_page.section_drift")}</SectionTitle>
          {alerts.length === 0 ? (
            <Empty
              description={t("quality_page.drift_empty")}
              data-testid="quality-drift-empty"
            />
          ) : (
            <Table<QualityDriftAlert>
              rowKey="id"
              columns={driftColumns}
              dataSource={alerts}
              pagination={false}
              size="small"
              style={{ marginBottom: 24 }}
              data-testid="quality-drift-table"
            />
          )}

          <SectionTitle>{t("quality_page.section_trend")}</SectionTitle>
          {trends.length === 0 ? (
            <Empty
              description={t("quality_page.trend_empty")}
              data-testid="quality-trend-empty"
            />
          ) : (
            <Table<AgentTrend>
              rowKey="agent"
              columns={trendColumns}
              dataSource={trends}
              pagination={false}
              size="small"
              style={{ marginBottom: 24 }}
              data-testid="quality-trend-table"
            />
          )}

          <SectionTitle>{t("quality_page.section_low_scores")}</SectionTitle>
          {lowScores.length === 0 ? (
            <Empty
              description={t("quality_page.low_empty")}
              data-testid="quality-low-empty"
            />
          ) : (
            <Table<QualityScore>
              rowKey="id"
              columns={lowColumns}
              dataSource={lowScores}
              pagination={{ pageSize: 20, hideOnSinglePage: true }}
              size="small"
              data-testid="quality-low-table"
            />
          )}
        </>
      )}
    </div>
  );
}

function SectionTitle({ children }: { children: ReactNode }) {
  return (
    <Text
      strong
      style={{
        display: "block",
        fontSize: 13,
        margin: "8px 0",
        color: "var(--ew-text-strong)",
      }}
    >
      {children}
    </Text>
  );
}
