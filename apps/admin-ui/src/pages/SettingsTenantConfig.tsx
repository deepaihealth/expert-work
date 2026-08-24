/**
 * Settings — Tenant Config page (Stream H.4 PR 8).
 *
 * Per-tenant config knobs (display_name / plan / retention days /
 * allowlists / pii fields). Backend is tenant-scoped only — same
 * effective-tenant resolution as ``SettingsTenantQuotas``.
 *
 * Edit flow:
 *   1. GET returns the current record (or 404 if no config row yet)
 *   2. Switch to "edit" mode → Monaco JSON editor (PATCH body shape)
 *   3. JSON.parse validation → pristine vs dirty buffer detection
 *   4. Save → PUT → soft "Reload to see latest" hint (M0 has no ETag;
 *      M1 adds If-Match → 412 conflict path)
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  Empty,
  Skeleton,
  Space,
  Tag,
  Typography,
} from "antd";
import Editor from "@monaco-editor/react";
import { Edit3, Save, Settings2, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { PageHeader } from "../components/PageHeader";
import {
  getTenantConfig,
  upsertTenantConfig,
  type TenantConfigPatchBody,
  type TenantConfigRecord,
} from "../api/tenant_config";
import { useAuth } from "../auth/AuthContext";
import { useTenantScope } from "../tenant/TenantScopeContext";

const { Text } = Typography;

type ParseResult =
  | { ok: true; value: TenantConfigPatchBody }
  | { ok: false; error: string };

function parsePatch(buffer: string): ParseResult {
  if (buffer.trim().length === 0) {
    return { ok: false, error: "empty" };
  }
  try {
    const parsed: unknown = JSON.parse(buffer);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return { ok: false, error: "top-level must be a JSON object" };
    }
    return { ok: true, value: parsed as TenantConfigPatchBody };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "parse error" };
  }
}

function recordToPatchJson(record: TenantConfigRecord): string {
  const patch: TenantConfigPatchBody = {
    display_name: record.display_name,
    plan: record.plan,
    model_credentials_ref: record.model_credentials_ref,
    // mcp_allowlist 刻意不进种子 patch(BUG-1):它由 MCP 页面的原子
    // enable/disable 独占管理;这里整表回传会把页面加载之后别处的启用/
    // 停用一并回滚(patch 语义 = 缺席即不动,这正是我们要的)。
    rate_limit_override: record.rate_limit_override,
    pii_fields: record.pii_fields,
    http_tool_allowlist: record.http_tool_allowlist,
    http_tool_denylist: record.http_tool_denylist,
    mcp_servers: record.mcp_servers,
    audit_retention_days: record.audit_retention_days,
    event_log_retention_days: record.event_log_retention_days,
    // Sprint #4 (Mini-ADR U-28) — Curator thresholds round-trip
    // through the same JSON-edit surface as the rest of tenant_config.
    skill_stale_days: record.skill_stale_days,
    skill_archive_days: record.skill_archive_days,
    // SE-16 (SE-A41/A45) — skill-evolution rollout gate + implicit judge
    // sample rate, editable through the same JSON surface (PR-4 folds the
    // sample-rate input into the PR-2 rollout pane).
    skill_evolution_enabled: record.skill_evolution_enabled,
    skill_evolution_judge_sample_pct: record.skill_evolution_judge_sample_pct,
  };
  return JSON.stringify(patch, null, 2);
}

/** Stream C.6 — render the per-tenant rate_limit_override for the read view. */
function formatRateLimitOverride(ov: Record<string, unknown>): string {
  const rpm = ov["requests_per_minute"];
  if (typeof rpm !== "number") return "—";
  const burst = ov["burst"];
  return typeof burst === "number" ? `${rpm} req/min (burst ${burst})` : `${rpm} req/min`;
}

export function SettingsTenantConfig() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const { scope, apiTenantScope } = useTenantScope();
  const auth = useAuth();
  const homeTenantId = auth.identity?.homeTenantId ?? null;

  const effectiveTenantId =
    scope === "*"
      ? null
      : typeof apiTenantScope === "string" && apiTenantScope !== "*"
        ? apiTenantScope
        : homeTenantId;

  const [record, setRecord] = useState<TenantConfigRecord | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [editing, setEditing] = useState(false);
  const [buf, setBuf] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const pristineJson = useMemo(
    () => (record === null ? "" : recordToPatchJson(record)),
    [record],
  );

  const refresh = useCallback(async () => {
    if (effectiveTenantId === null) {
      setRecord(null);
      setNotFound(false);
      return;
    }
    setLoading(true);
    setError(null);
    setNotFound(false);
    try {
      const result = await getTenantConfig(effectiveTenantId);
      setRecord(result);
    } catch (err) {
      if (err instanceof ApiError && err.code === "TENANT_CONFIG_NOT_FOUND") {
        setRecord(null);
        setNotFound(true);
      } else {
        const msg =
          err instanceof ApiError
            ? `${err.code}: ${err.message}`
            : err instanceof Error
              ? err.message
              : "unknown error";
        setError(msg);
      }
    } finally {
      setLoading(false);
    }
  }, [effectiveTenantId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const parseResult = useMemo(() => parsePatch(buf), [buf]);
  const isDirty = buf !== pristineJson;

  const onEdit = useCallback(() => {
    setBuf(pristineJson);
    setEditing(true);
  }, [pristineJson]);

  const onCancel = useCallback(() => {
    setBuf(pristineJson);
    setEditing(false);
  }, [pristineJson]);

  const onSave = useCallback(async () => {
    if (effectiveTenantId === null) return;
    if (!parseResult.ok) return;
    setSubmitting(true);
    try {
      const updated = await upsertTenantConfig(effectiveTenantId, parseResult.value);
      setRecord(updated);
      setEditing(false);
      message.success(t("settings_ops.config_saved"));
    } catch (err) {
      message.error(err instanceof Error ? err.message : "failed");
    } finally {
      setSubmitting(false);
    }
  }, [effectiveTenantId, parseResult, message, t]);

  return (
    <div data-testid="config-root">
      <PageHeader
        icon={<Settings2 size={18} strokeWidth={1.5} />}
        title={t("settings_ops.config_page_title")}
        subtitle={t("settings_ops.config_subtitle")}
        actions={
          <>
            {effectiveTenantId !== null && (
              <Tag color="default" data-testid="config-tenant-tag">
                tenant:{" "}
                <Text code style={{ fontSize: 11 }}>{effectiveTenantId.slice(0, 8)}…</Text>
              </Tag>
            )}
            {isDirty && editing && (
              <Tag color="warning" data-testid="config-dirty-tag">{t("settings_ops.dirty")}</Tag>
            )}
            {editing ? (
              <Space>
                <Button onClick={onCancel} icon={<X size={14} strokeWidth={1.75} />}>
                  {t("common.cancel")}
                </Button>
                <Button
                  type="primary"
                  onClick={onSave}
                  loading={submitting}
                  disabled={!parseResult.ok || !isDirty}
                  icon={<Save size={14} strokeWidth={1.75} />}
                  data-testid="config-save-btn"
                >
                  {t("common.save")}
                </Button>
              </Space>
            ) : (
              <Button
                type="primary"
                onClick={onEdit}
                icon={<Edit3 size={14} strokeWidth={1.75} />}
                disabled={effectiveTenantId === null || record === null}
                data-testid="config-edit-btn"
              >
                {t("common.edit")}
              </Button>
            )}
          </>
        }
      />

      {effectiveTenantId === null && (
        <Alert
          type="info"
          showIcon
          message={t("settings_ops.cross_tenant_blocked_title")}
          description={t("settings_ops.cross_tenant_blocked_body")}
          data-testid="config-cross-tenant-block"
        />
      )}

      {error !== null && (
        <Alert
          type="error"
          showIcon
          message={t("settings_ops.failed_to_load")}
          description={error}
          style={{ marginBottom: 12 }}
          data-testid="config-error"
        />
      )}

      {effectiveTenantId !== null && loading && record === null && !notFound && (
        <Skeleton active paragraph={{ rows: 8 }} />
      )}

      {notFound && (
        <Empty description={t("settings_ops.config_not_found")} data-testid="config-not-found" />
      )}

      {record !== null && !editing && (
        <Card title={t("settings_ops.config_record_title")} size="small">
          <dl
            style={{
              display: "grid",
              gridTemplateColumns: "200px 1fr",
              rowGap: 8,
              columnGap: 16,
              margin: 0,
              fontSize: 13,
            }}
          >
            <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("settings_ops.display_name")}</dt>
            <dd style={{ margin: 0 }}>{record.display_name}</dd>
            <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("settings_ops.plan")}</dt>
            <dd style={{ margin: 0 }}><Tag color="blue">{record.plan}</Tag></dd>
            <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("settings_ops.audit_retention_days")}</dt>
            <dd style={{ margin: 0 }}>{record.audit_retention_days}</dd>
            <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("settings_ops.event_log_retention_days")}</dt>
            <dd style={{ margin: 0 }}>{record.event_log_retention_days}</dd>
            {/* Stream C.6 — per-tenant rate limit. Read-only here; edit via the
                JSON editor: "rate_limit_override": {"requests_per_minute": N}. */}
            <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("settings_ops.rate_limit_override")}</dt>
            <dd style={{ margin: 0 }} data-testid="tenant-config-rate-limit-override">
              {record.rate_limit_override && Object.keys(record.rate_limit_override).length > 0
                ? formatRateLimitOverride(record.rate_limit_override)
                : t("settings_ops.rate_limit_default")}
            </dd>
            {/* Sprint #4 (Mini-ADR U-28) — Curator thresholds.
                Displayed read-only; edit goes through the same JSON
                editor as the rest of tenant_config (we deliberately
                don't split out per-field forms — the JSON model keeps
                the surface flat). */}
            <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("settings_ops.skill_stale_days")}</dt>
            <dd style={{ margin: 0 }} data-testid="tenant-config-skill-stale-days">
              {record.skill_stale_days}
            </dd>
            <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("settings_ops.skill_archive_days")}</dt>
            <dd style={{ margin: 0 }} data-testid="tenant-config-skill-archive-days">
              {record.skill_archive_days}
            </dd>
            {/* SE-16 (SE-A41) — skill-evolution rollout gate. Read-only
                display; flip it through the same JSON editor (rollout is
                deliberate, not a casual toggle). */}
            <dt style={{ color: "var(--ew-text-tertiary)" }}>
              {t("settings_ops.skill_evolution_enabled")}
            </dt>
            <dd style={{ margin: 0 }} data-testid="tenant-config-skill-evolution-enabled">
              {record.skill_evolution_enabled
                ? t("settings_ops.skill_evolution_on")
                : t("settings_ops.skill_evolution_off")}
            </dd>
            {/* SE-16 (SE-A45) — implicit-candidate judge sample rate. */}
            <dt style={{ color: "var(--ew-text-tertiary)" }}>
              {t("settings_ops.skill_evolution_judge_sample_pct")}
            </dt>
            <dd style={{ margin: 0 }} data-testid="tenant-config-skill-evolution-judge-sample-pct">
              {record.skill_evolution_judge_sample_pct}%
            </dd>
            <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("settings_ops.mcp_allowlist")}</dt>
            <dd style={{ margin: 0 }}>
              {record.mcp_allowlist.length === 0 ? (
                <Text type="secondary">—</Text>
              ) : (
                <Space size={4} wrap>
                  {record.mcp_allowlist.map((m) => (
                    <Tag key={m} bordered={false}>{m}</Tag>
                  ))}
                </Space>
              )}
            </dd>
            <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("settings_ops.http_allowlist")}</dt>
            <dd style={{ margin: 0 }}>
              {record.http_tool_allowlist.length === 0 ? (
                <Text type="secondary">{t("settings_ops.http_allowlist_empty")}</Text>
              ) : (
                <Space size={4} wrap>
                  {record.http_tool_allowlist.map((url) => (
                    <Tag key={url} bordered={false}>{url}</Tag>
                  ))}
                </Space>
              )}
            </dd>
            <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("settings_ops.http_denylist")}</dt>
            <dd style={{ margin: 0 }} data-testid="tenant-config-http-denylist">
              {record.http_tool_denylist.length === 0 ? (
                <Text type="secondary">{t("settings_ops.http_denylist_empty")}</Text>
              ) : (
                <Space size={4} wrap>
                  {record.http_tool_denylist.map((host) => (
                    <Tag key={host} color="red" bordered={false}>{host}</Tag>
                  ))}
                </Space>
              )}
            </dd>
            <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("settings_ops.updated")}</dt>
            <dd style={{ margin: 0 }}>{new Date(record.updated_at).toLocaleString()} ({record.updated_by})</dd>
          </dl>
        </Card>
      )}

      {editing && (
        <Card
          title={t("settings_ops.config_edit_title")}
          size="small"
          data-testid="config-edit-card"
        >
          <Alert
            type="warning"
            showIcon
            message={t("settings_ops.config_etag_hint_title")}
            description={t("settings_ops.config_etag_hint_body")}
            style={{ marginBottom: 12 }}
          />
          <div style={{ border: "1px solid var(--ew-border-default)", borderRadius: 4 }}>
            <Editor
              height="480px"
              defaultLanguage="json"
              value={buf}
              onChange={(v) => setBuf(v ?? "")}
              options={{ minimap: { enabled: false }, fontSize: 12, wordWrap: "on" }}
              data-testid="config-editor"
            />
          </div>
          {!parseResult.ok && (
            <Alert
              type="error"
              message={t("settings_ops.config_parse_error")}
              description={parseResult.error}
              style={{ marginTop: 8 }}
              data-testid="config-parse-error"
            />
          )}
        </Card>
      )}
    </div>
  );
}
