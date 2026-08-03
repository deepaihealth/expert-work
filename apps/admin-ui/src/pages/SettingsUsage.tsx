/**
 * Settings — Usage page (Stream Z3, tenant-scoped).
 *
 * Any authenticated user with ``billing:read`` (all tenant roles) — NOT
 * system_admin gated; tenant scoping is automatic server-side. Shows the
 * current/selected month's **billed** cost (only) plus realtime token
 * counters.
 *
 * MONETIZATION NO-LEAK RULE: this page renders billed cost + tokens ONLY.
 * It never displays (and the SDK never carries) base_cost / markup / margin —
 * those live exclusively on the system_admin chargeback page.
 *
 * Mirrors the tenant-page pattern (``SettingsAudit``): PageHeader + filter
 * bar + antd Table + ``ApiError`` → ``${code}: ${message}`` errors, with
 * CSS-var surface treatment for the locked dark-first design baseline.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  DatePicker,
  Segmented,
  Skeleton,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { TableColumnsType } from "antd";
import dayjs, { type Dayjs } from "dayjs";
import { Gauge } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { useTenantScope } from "../tenant/TenantScopeContext";
import { PageHeader } from "../components/PageHeader";
import {
  getUsageCost,
  getUsageTokens,
  type TokenCounts,
  type TokenGroup,
  type UsageCost,
  type UsageCostGroup,
  type UsageGroupBy,
  type UsageTokens,
} from "../api/usage";
import { formatMicros } from "../utils/money";
import { cacheHitRate, formatHitRate } from "../utils/cache";
import { tenantRowKey } from "../utils/tenantRowKey";

const { Text } = Typography;

const MONTH_FMT = "YYYY-MM";

/** Stream RT-3 (RT-ADR-14) — the cache columns shared by the cost / token /
 *  kind tables. Every row extends ``TokenCounts``, so one factory types for all
 *  three. Hit rate is derived client-side (no backend metric). */
function cacheColumns<T extends TokenCounts>(
  t: (key: string) => string,
): TableColumnsType<T> {
  return [
    {
      title: t("usage.col_cache_read_tokens"),
      dataIndex: "cache_read_tokens",
      key: "cache_read_tokens",
      width: 150,
      align: "right",
      // ``?? 0`` — cache columns (migration 0036) may be absent on older
      // pre-metering rollup rows; never blank the row on a missing count.
      render: (v: number) => (v ?? 0).toLocaleString(),
    },
    {
      title: t("usage.col_cache_creation_tokens"),
      dataIndex: "cache_creation_tokens",
      key: "cache_creation_tokens",
      width: 160,
      align: "right",
      render: (v: number) => (v ?? 0).toLocaleString(),
    },
    {
      title: t("usage.col_hit_rate"),
      key: "hit_rate",
      width: 110,
      align: "right",
      render: (_: unknown, row: T) => formatHitRate(cacheHitRate(row)),
    },
  ];
}

/** Cross-tenant W4 — the owning-tenant column shared by the cost / token /
 *  kind tables; callers append it only in the "*" aggregate (the backend
 *  buckets per tenant there and each row carries its ``tenant_id``). */
function tenantColumn<T extends { tenant_id?: string | null }>(
  t: (key: string) => string,
): TableColumnsType<T> {
  return [
    {
      title: t("usage.col_tenant"),
      dataIndex: "tenant_id",
      key: "tenant_id",
      width: 120,
      render: (v: string | null | undefined) =>
        v ? (
          <Tooltip title={v}>
            <Text code style={{ fontSize: 12 }}>
              {v.slice(0, 8)}…
            </Text>
          </Tooltip>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
  ];
}

type CostGroupBy = Extract<UsageGroupBy, "agent" | "model">;

function errText(err: unknown): string {
  return err instanceof ApiError
    ? `${err.code}: ${err.message}`
    : err instanceof Error
      ? err.message
      : "unknown error";
}

export function SettingsUsage() {
  const { t } = useTranslation();
  // Cross-tenant W3 — the rollups are scope-aware ("*" aggregates every tenant).
  const { apiTenantScope } = useTenantScope();
  // Cross-tenant W4 — "*" aggregate: the backend buckets per tenant and each
  // row carries its owning tenant; surface it as a column.
  const isAggregate = apiTenantScope === "*";

  const [month, setMonth] = useState<Dayjs>(() => dayjs());
  const [groupBy, setGroupBy] = useState<CostGroupBy>("agent");

  const [cost, setCost] = useState<UsageCost | null>(null);
  const [tokens, setTokens] = useState<UsageTokens | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    const monthStr = month.format(MONTH_FMT);
    try {
      const [c, tok] = await Promise.all([
        getUsageCost({ month: monthStr, groupBy, tenantScope: apiTenantScope }),
        getUsageTokens({ month: monthStr, tenantScope: apiTenantScope }),
      ]);
      setCost(c);
      setTokens(tok);
    } catch (err) {
      setError(errText(err));
    } finally {
      setLoading(false);
    }
  }, [month, groupBy, apiTenantScope]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const costColumns: TableColumnsType<UsageCostGroup> = useMemo(
    () => [
      {
        title: t("usage.col_key"),
        dataIndex: "key",
        key: "key",
        render: (key: string, row) => (
          <span>
            <Text strong>{key}</Text>
            {row.unpriced && (
              <Tag color="warning" style={{ marginLeft: 8 }} data-testid={`usage-unpriced-${key}`}>
                {t("usage.unpriced")}
              </Tag>
            )}
          </span>
        ),
      },
      ...(isAggregate ? tenantColumn<UsageCostGroup>(t) : []),
      {
        title: t("usage.col_input_tokens"),
        dataIndex: "input_tokens",
        key: "input_tokens",
        width: 140,
        align: "right",
        render: (v: number) => v.toLocaleString(),
      },
      {
        title: t("usage.col_output_tokens"),
        dataIndex: "output_tokens",
        key: "output_tokens",
        width: 140,
        align: "right",
        render: (v: number) => v.toLocaleString(),
      },
      ...cacheColumns<UsageCostGroup>(t),
      {
        title: t("usage.col_billed"),
        dataIndex: "billed_cost_micros",
        key: "billed_cost_micros",
        width: 140,
        align: "right",
        render: (v: number) => (
          <Text style={{ fontFamily: "var(--ew-font-mono)" }}>{formatMicros(v)}</Text>
        ),
      },
    ],
    [t, isAggregate],
  );

  const tokenColumns: TableColumnsType<TokenGroup> = useMemo(
    () => [
      { title: t("usage.col_key"), dataIndex: "key", key: "key" },
      ...(isAggregate ? tenantColumn<TokenGroup>(t) : []),
      {
        title: t("usage.col_input_tokens"),
        dataIndex: "input_tokens",
        key: "input_tokens",
        width: 140,
        align: "right",
        render: (v: number) => v.toLocaleString(),
      },
      {
        title: t("usage.col_output_tokens"),
        dataIndex: "output_tokens",
        key: "output_tokens",
        width: 140,
        align: "right",
        render: (v: number) => v.toLocaleString(),
      },
      ...cacheColumns<TokenGroup>(t),
    ],
    [t, isAggregate],
  );

  const kindColumns: TableColumnsType<TokenGroup> = useMemo(
    () => [
      {
        title: t("usage.col_kind"),
        dataIndex: "key",
        key: "key",
        render: (key: string) => (
          <Tag
            color={
              key === "skill_evolution" ? "purple" : key === "memory_consolidation" ? "cyan" : "default"
            }
          >
            {key === "skill_evolution"
              ? t("usage.kind_skill_evolution")
              : key === "memory_consolidation"
                ? t("usage.kind_memory_consolidation")
                : key === "conversation"
                  ? t("usage.kind_conversation")
                  : key}
          </Tag>
        ),
      },
      ...(isAggregate ? tenantColumn<TokenGroup>(t) : []),
      {
        title: t("usage.col_input_tokens"),
        dataIndex: "input_tokens",
        key: "input_tokens",
        width: 140,
        align: "right",
        render: (v: number) => v.toLocaleString(),
      },
      {
        title: t("usage.col_output_tokens"),
        dataIndex: "output_tokens",
        key: "output_tokens",
        width: 140,
        align: "right",
        render: (v: number) => v.toLocaleString(),
      },
      ...cacheColumns<TokenGroup>(t),
    ],
    [t, isAggregate],
  );

  return (
    <div data-testid="usage-root">
      <PageHeader
        icon={<Gauge size={18} strokeWidth={1.5} />}
        title={t("usage.page_title")}
        // W4(review M4)— 聚合态口径换成平台全租户(照 empty_cross 模式)。
        subtitle={t(isAggregate ? "usage.subtitle_cross" : "usage.subtitle")}
        actions={
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <DatePicker
              picker="month"
              value={month}
              allowClear={false}
              onChange={(value) => value && setMonth(value)}
              data-testid="usage-month"
            />
            <Segmented<CostGroupBy>
              value={groupBy}
              onChange={(value) => setGroupBy(value)}
              options={[
                { value: "agent", label: t("usage.group_by_agent") },
                { value: "model", label: t("usage.group_by_model") },
              ]}
              data-testid="usage-group-by"
            />
          </div>
        }
      />

      {error !== null && (
        <Alert
          type="error"
          showIcon
          message={t("usage.failed_to_load")}
          description={error}
          style={{ marginBottom: 12 }}
          data-testid="usage-error"
        />
      )}

      {loading && cost === null ? (
        <Skeleton active paragraph={{ rows: 6 }} />
      ) : (
        <>
          <div
            style={{
              padding: 16,
              marginBottom: 16,
              background: "var(--ew-surface-raised)",
              border: "1px solid var(--ew-border-subtle)",
              borderRadius: 6,
            }}
            data-testid="usage-summary"
          >
            <Statistic
              title={t("usage.total_billed")}
              value={formatMicros(cost?.total_billed_cost_micros ?? 0)}
              valueStyle={{ fontFamily: "var(--ew-font-mono)" }}
            />
            {cost?.as_of != null && (
              <Text type="secondary" style={{ fontSize: 12 }} data-testid="usage-as-of">
                {t("usage.as_of_note", { time: new Date(cost.as_of).toLocaleString() })}
              </Text>
            )}
          </div>

          <Table<UsageCostGroup>
            columns={costColumns}
            dataSource={cost?.groups ?? []}
            // W4 — per-tenant buckets repeat ``key`` in the "*" aggregate.
            rowKey={(r) => tenantRowKey(r.tenant_id, r.key)}
            loading={loading}
            pagination={false}
            locale={{ emptyText: t("usage.empty") }}
            style={{ marginBottom: 24 }}
            data-testid="usage-cost-table"
          />

          <div style={{ marginBottom: 8 }}>
            <Text strong style={{ fontSize: 14 }}>
              {t("usage.tokens_heading")}
            </Text>
            <Tag color="processing" style={{ marginLeft: 8 }}>
              {t("usage.realtime")}
            </Tag>
            <div>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {t("usage.realtime_note")}
              </Text>
            </div>
          </div>

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
            data-testid="usage-token-totals"
          >
            <Statistic
              title={t("usage.col_input_tokens")}
              value={(tokens?.total.input_tokens ?? 0).toLocaleString()}
            />
            <Statistic
              title={t("usage.col_output_tokens")}
              value={(tokens?.total.output_tokens ?? 0).toLocaleString()}
            />
            <Statistic
              title={t("usage.col_cache_creation_tokens")}
              value={(tokens?.total.cache_creation_tokens ?? 0).toLocaleString()}
            />
            <Statistic
              title={t("usage.col_cache_read_tokens")}
              value={(tokens?.total.cache_read_tokens ?? 0).toLocaleString()}
            />
            <Statistic
              title={t("usage.col_hit_rate")}
              value={formatHitRate(tokens ? cacheHitRate(tokens.total) : null)}
              valueStyle={{ color: "var(--ew-accent-cyan, #22d3ee)" }}
            />
          </div>

          <Table<TokenGroup>
            columns={tokenColumns}
            dataSource={
              groupBy === "agent" ? (tokens?.by_agent ?? []) : (tokens?.by_model ?? [])
            }
            rowKey={(r) => tenantRowKey(r.tenant_id, r.key)}
            pagination={false}
            locale={{ emptyText: t("usage.empty") }}
            style={{ marginBottom: 24 }}
            data-testid="usage-token-table"
          />

          {/* SE-16 (SE-A43) — evolution spend separable from conversation. */}
          <div style={{ marginBottom: 8 }}>
            <Text strong style={{ fontSize: 14 }}>
              {t("usage.kind_heading")}
            </Text>
          </div>
          <Table<TokenGroup>
            columns={kindColumns}
            dataSource={tokens?.by_kind ?? []}
            rowKey={(r) => tenantRowKey(r.tenant_id, r.key)}
            pagination={false}
            locale={{ emptyText: t("usage.empty") }}
            data-testid="usage-token-kind-table"
          />
        </>
      )}
    </div>
  );
}
