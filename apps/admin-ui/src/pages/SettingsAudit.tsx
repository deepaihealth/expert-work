/**
 * Audit query page — Stream H.4 PR 4.
 *
 * Reviewer-facing audit-log surface. Filter chips on the right
 * (Actor / Action / Resource type / Result / time range), a vertical
 * timeline of entries on the left, and a Drawer that shows the full
 * ``AuditEntry.details`` payload (already redactor-cleaned at write).
 *
 * Cursor pagination is opaque — we pass ``next_cursor`` through
 * verbatim and stop when the backend says ``has_more=false``.
 *
 * Cross-tenant view requires system_admin (the SDK + backend gate
 * this; the UI just plumbs the current ``TenantScope``).
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  App,
  Button,
  DatePicker,
  Drawer,
  Empty,
  Input,
  Select,
  Skeleton,
  Space,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { Dayjs } from "dayjs";
import {
  AlertTriangle,
  CheckCircle2,
  Globe2,
  Network,
  ShieldAlert,
  ShieldOff,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { PageHeader } from "../components/PageHeader";
import {
  listAudit,
  type AuditEntry,
  type AuditResult,
  type ListAuditParams,
} from "../api/audit";
import { memberNameWithId } from "../components/MemberName";
import { useMemberNames } from "../hooks/useMemberNames";
import { useTenantScope } from "../tenant/TenantScopeContext";

const { Text } = Typography;

const RESULT_OPTIONS: AuditResult[] = ["success", "denied", "error"];

/** Above this response size an MCP call is highlighted as a possible
 *  data-volume / exfil anomaly (Stream 14.4). */
const MCP_LARGE_RESPONSE_CHARS = 100_000;

interface McpTraffic {
  server: string;
  responseChars: number | null;
  isError: boolean;
}

/** Stream 14.4 — surface MCP traffic dimensions the orchestrator wrote into
 *  ``details`` (``mcp_server`` / ``response_chars`` / ``mcp_is_error``) so an
 *  operator scans MCP volume in the timeline instead of opening every drawer.
 *  Returns ``null`` for non-MCP rows. */
function mcpTraffic(entry: AuditEntry): McpTraffic | null {
  // MCP tool names are namespaced ``mcp__<server>__<tool>`` (wire-safe form).
  if (entry.resource_id === null || !entry.resource_id.startsWith("mcp__")) return null;
  const d = entry.details ?? {};
  const server =
    typeof d.mcp_server === "string" ? d.mcp_server : entry.resource_id.slice("mcp__".length).split("__")[0];
  const responseChars = typeof d.response_chars === "number" ? d.response_chars : null;
  return { server, responseChars, isError: d.mcp_is_error === true };
}

/** Compact char count, e.g. ``1.2k`` / ``3.4M``. */
function formatChars(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function ResultTag({ result }: { result: AuditResult }) {
  if (result === "success") {
    return (
      <Tag color="success" icon={<CheckCircle2 size={11} strokeWidth={1.75} />}>
        success
      </Tag>
    );
  }
  if (result === "denied") {
    return (
      <Tag color="warning" icon={<ShieldOff size={11} strokeWidth={1.75} />}>
        denied
      </Tag>
    );
  }
  return (
    <Tag color="error" icon={<AlertTriangle size={11} strokeWidth={1.75} />}>
      error
    </Tag>
  );
}

export function SettingsAudit() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const { apiTenantScope } = useTenantScope();
  // B-316 — actor ids shown as member names; the actor filter still takes ids.
  const memberNameOf = useMemberNames(apiTenantScope);
  const withMemberName = (id: string): string => memberNameWithId(t, memberNameOf, id);

  // Active filters — drive the query. ``cursor`` resets whenever a
  // filter changes; the user clicks "Load more" to extend.
  const [actorFilter, setActorFilter] = useState("");
  const [actionFilter, setActionFilter] = useState("");
  const [resourceTypeFilter, setResourceTypeFilter] = useState("");
  const [resultFilter, setResultFilter] = useState<AuditResult | undefined>(undefined);
  const [timeRange, setTimeRange] = useState<[Dayjs | null, Dayjs | null] | null>(null);

  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [appliedScope, setAppliedScope] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [selected, setSelected] = useState<AuditEntry | null>(null);

  const buildParams = useCallback(
    (next: { cursor?: string | null } = {}): ListAuditParams => {
      const fromTs = timeRange?.[0]?.toISOString();
      const toTs = timeRange?.[1]?.toISOString();
      return {
        tenantScope: apiTenantScope,
        actorId: actorFilter.trim().length > 0 ? actorFilter.trim() : undefined,
        action: actionFilter.trim().length > 0 ? actionFilter.trim() : undefined,
        resourceType:
          resourceTypeFilter.trim().length > 0 ? resourceTypeFilter.trim() : undefined,
        result: resultFilter,
        fromTs,
        toTs,
        cursor: next.cursor,
      };
    },
    [
      apiTenantScope,
      actorFilter,
      actionFilter,
      resourceTypeFilter,
      resultFilter,
      timeRange,
    ],
  );

  const fetchFirstPage = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listAudit(buildParams());
      setEntries(result.items);
      setCursor(result.next_cursor);
      setHasMore(result.has_more);
      setAppliedScope(result.applied_scope);
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
  }, [buildParams]);

  const loadMore = useCallback(async () => {
    if (cursor === null) return;
    setLoadingMore(true);
    try {
      const result = await listAudit(buildParams({ cursor }));
      setEntries((prev) => [...prev, ...result.items]);
      setCursor(result.next_cursor);
      setHasMore(result.has_more);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "unknown error";
      message.error(msg);
    } finally {
      setLoadingMore(false);
    }
  }, [buildParams, cursor, message]);

  // Re-fetch from page 1 whenever any filter changes.
  useEffect(() => {
    fetchFirstPage();
  }, [fetchFirstPage]);

  const isCrossTenant = appliedScope === "cross_tenant";

  const detailsJson = useMemo(
    () => (selected !== null ? JSON.stringify(selected.details, null, 2) : ""),
    [selected],
  );

  return (
    <div data-testid="audit-root">
      <PageHeader
        icon={<ShieldAlert size={18} strokeWidth={1.5} />}
        title={t("audit.page_title")}
        subtitle={t("audit.subtitle")}
        actions={
          isCrossTenant && (
            <Tag icon={<Globe2 size={12} strokeWidth={1.5} />} color="purple" data-testid="audit-cross-banner">
              {t("audit.cross_tenant_banner")}
            </Tag>
          )
        }
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
        data-testid="audit-filters"
      >
        <Input
          placeholder={t("audit.filter_actor")}
          value={actorFilter}
          onChange={(e) => setActorFilter(e.target.value)}
          style={{ width: 200 }}
          allowClear
          data-testid="audit-actor-filter"
        />
        <Input
          placeholder={t("audit.filter_action")}
          value={actionFilter}
          onChange={(e) => setActionFilter(e.target.value)}
          style={{ width: 200 }}
          allowClear
          data-testid="audit-action-filter"
        />
        <Input
          placeholder={t("audit.filter_resource_type")}
          value={resourceTypeFilter}
          onChange={(e) => setResourceTypeFilter(e.target.value)}
          style={{ width: 180 }}
          allowClear
          data-testid="audit-resource-filter"
        />
        <Select<AuditResult | "all">
          value={resultFilter ?? "all"}
          onChange={(v) => setResultFilter(v === "all" ? undefined : (v as AuditResult))}
          style={{ width: 140 }}
          aria-label={t("audit.filter_result")}
          data-testid="audit-result-filter"
          options={[
            { value: "all", label: t("audit.filter_result_all") },
            ...RESULT_OPTIONS.map((r) => ({ value: r, label: r })),
          ]}
        />
        <DatePicker.RangePicker
          showTime
          value={timeRange}
          onChange={(range) => setTimeRange(range as [Dayjs | null, Dayjs | null] | null)}
          data-testid="audit-time-range"
        />
      </div>

      {error !== null && (
        <Alert
          type="error"
          showIcon
          message={t("audit.failed_to_load")}
          description={error}
          style={{ marginBottom: 12 }}
          data-testid="audit-error"
        />
      )}

      {loading && entries.length === 0 ? (
        <Skeleton active paragraph={{ rows: 5 }} />
      ) : entries.length === 0 ? (
        <Empty description={t("audit.empty")} />
      ) : (
        <div className="ew-audit-timeline" data-testid="audit-timeline">
          {entries.map((entry, idx) => (
            <div
              key={`${entry.id ?? "no-id"}-${idx}`}
              data-testid={`audit-row-${entry.id ?? idx}`}
              onClick={() => setSelected(entry)}
              style={{
                display: "grid",
                gridTemplateColumns: "180px 200px 1fr 100px",
                gap: 12,
                alignItems: "center",
                padding: "8px 12px",
                borderBottom: "1px solid var(--ew-border-subtle)",
                cursor: "pointer",
                fontSize: 13,
              }}
            >
              <Text type="secondary" style={{ fontSize: 12 }}>
                {entry.occurred_at !== null ? new Date(entry.occurred_at).toLocaleString() : "—"}
              </Text>
              <Tooltip title={entry.actor_id}>
                {memberNameOf(entry.actor_id) !== entry.actor_id ? (
                  <Text style={{ fontSize: 12 }} data-testid={`audit-actor-${entry.id ?? idx}`}>
                    {memberNameOf(entry.actor_id)}
                  </Text>
                ) : (
                  <Text code style={{ fontSize: 11 }} data-testid={`audit-actor-${entry.id ?? idx}`}>
                    {entry.actor_id.length > 24 ? `${entry.actor_id.slice(0, 24)}…` : entry.actor_id}
                  </Text>
                )}
              </Tooltip>
              <Space size={6}>
                <Text code style={{ fontSize: 12 }}>{entry.action}</Text>
                <Text type="secondary" style={{ fontSize: 12 }}>·</Text>
                <Text type="secondary" style={{ fontSize: 12 }}>{entry.resource_type}</Text>
                {entry.resource_id !== null && (
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    ({entry.resource_id.length > 18 ? `${entry.resource_id.slice(0, 18)}…` : entry.resource_id})
                  </Text>
                )}
                {(() => {
                  const mcp = mcpTraffic(entry);
                  if (mcp === null) return null;
                  const large =
                    mcp.responseChars !== null && mcp.responseChars >= MCP_LARGE_RESPONSE_CHARS;
                  return (
                    <Tag
                      color={large ? "warning" : "cyan"}
                      icon={<Network size={10} strokeWidth={1.75} />}
                      data-testid={`audit-mcp-${entry.id ?? "x"}`}
                      title={t("audit.mcp_traffic_hint", { server: mcp.server })}
                    >
                      {mcp.server}
                      {mcp.responseChars !== null && ` · ${formatChars(mcp.responseChars)}`}
                    </Tag>
                  );
                })()}
              </Space>
              <ResultTag result={entry.result} />
            </div>
          ))}
        </div>
      )}

      {hasMore && (
        <div style={{ display: "flex", justifyContent: "center", marginTop: 16 }}>
          <Button onClick={loadMore} loading={loadingMore} data-testid="audit-load-more">
            {t("audit.load_more")}
          </Button>
        </div>
      )}

      <Drawer
        title={selected !== null ? t("audit.detail_title") : ""}
        open={selected !== null}
        onClose={() => setSelected(null)}
        width={680}
        data-testid="audit-detail-drawer"
      >
        {selected !== null && (
          <Space direction="vertical" style={{ width: "100%" }} size={12}>
            <DetailRow label={t("audit.detail_id")} value={String(selected.id ?? "—")} mono />
            <DetailRow
              label={t("audit.detail_occurred_at")}
              value={selected.occurred_at ?? "—"}
            />
            <DetailRow label={t("audit.detail_actor")} value={`${selected.actor_type} / ${withMemberName(selected.actor_id)}`} mono />
            {selected.on_behalf_of !== null && (
              <DetailRow
                label={t("audit.detail_on_behalf_of")}
                value={withMemberName(selected.on_behalf_of)}
                mono
              />
            )}
            <DetailRow label={t("audit.detail_action")} value={selected.action} mono />
            <DetailRow
              label={t("audit.detail_resource")}
              value={`${selected.resource_type}${selected.resource_id !== null ? ` / ${selected.resource_id}` : ""}`}
              mono
            />
            <div>
              <Text type="secondary" style={{ fontSize: 12 }}>{t("audit.detail_result")}</Text>
              <div style={{ marginTop: 4 }}><ResultTag result={selected.result} /></div>
            </div>
            {selected.reason !== null && (
              <DetailRow label={t("audit.detail_reason")} value={selected.reason} />
            )}
            {selected.trace_id !== null && (
              <DetailRow label={t("audit.detail_trace_id")} value={selected.trace_id} mono />
            )}
            {selected.ip !== null && (
              <DetailRow label={t("audit.detail_ip")} value={selected.ip} mono />
            )}
            <div>
              <Text type="secondary" style={{ fontSize: 12 }}>{t("audit.detail_payload")}</Text>
              <pre
                data-testid="audit-detail-payload"
                style={{
                  background: "var(--ew-surface-raised)",
                  padding: 12,
                  borderRadius: 4,
                  fontSize: 11,
                  margin: "4px 0 0",
                  maxHeight: 320,
                  overflow: "auto",
                }}
              >
                {detailsJson}
              </pre>
              <Text type="secondary" style={{ fontSize: 11 }}>
                {t("audit.detail_payload_hint")}
              </Text>
            </div>
          </Space>
        )}
      </Drawer>
    </div>
  );
}

interface DetailRowProps {
  label: string;
  value: string;
  mono?: boolean;
}

function DetailRow({ label, value, mono = false }: DetailRowProps) {
  return (
    <div>
      <Text type="secondary" style={{ fontSize: 12 }}>{label}</Text>
      <div style={{ fontFamily: mono ? "var(--ew-font-mono)" : undefined, fontSize: 12, marginTop: 2 }}>
        {value}
      </div>
    </div>
  );
}
