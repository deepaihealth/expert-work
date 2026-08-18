/**
 * Agent detail page — Stream H.1b PR 3 (scaffold) + H.2 PR 1 (Manifest
 * Monaco editor) + H.2 PR 3 (Playground tab).
 *
 * Real fetch of ``GET /v1/agents/{name}/{version}``. Route shape moved
 * from the demo's ``/agents/:agentId/:tab`` (mock id) to the canonical
 * ``/agents/:name/:version/:tab`` to match the backend identity
 * (Mini-ADR B-3 keeps ``name + version`` as the agent's natural key —
 * the row UUID is internal).
 *
 * All tabs are wired: ``overview`` / ``manifest`` / ``history`` /
 * ``playground`` plus the per-agent sub-views (Runs / Skills /
 * Triggers / Memory, Stream H.6 PR 2 — backed by the agent list
 * filters from H.6 PR 1).
 */
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Empty,
  Input,
  Popconfirm,
  Row,
  Skeleton,
  Space,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { Ban, Bot, CircleCheck, Network, ShieldOff } from "lucide-react";
import { useTranslation } from "react-i18next";

import { disableAgent, enableAgent, getAgent, type AgentDetailResponse } from "../api/agents";
import { ApiError } from "../api/client";
import { PageHeader } from "../components/PageHeader";
import { readSecurity } from "../components/manifest-editor/form_model";
import { ReadonlyTooltip } from "../components/ReadonlyTooltip";
import { concreteTenantScope, useTenantScope } from "../tenant/TenantScopeContext";
import { useIsTenantSwitched } from "../tenant/useIsTenantSwitched";
import { ConversationsTab } from "./agent_detail/ConversationsTab";
import { UsersTab } from "./agent_detail/UsersTab";
import { HistoryTab } from "./agent_detail/HistoryTab";
import { ManifestTab } from "./agent_detail/ManifestTab";
import { PlaygroundTab } from "./agent_detail/PlaygroundTab";
import { SkillsTab } from "./agent_detail/SkillsTab";
import { TriggersTab } from "./agent_detail/TriggersTab";

const { Text } = Typography;

const STATUS_COLOR: Record<string, string> = {
  active: "success",
  draft: "warning",
  archived: "default",
  deleted: "error",
};

/** Stream RT-4 (RT-ADR-16) — the agent-level kill switch. Disabling rejects
 *  new runs/sessions across all versions of ``name`` and bulk-cancels the
 *  in-flight ones; enabling reverses it. Danger-confirmed (Popconfirm, mirrors
 *  the tenant suspend control on SettingsTenants), with an optional free-text
 *  reason captured on the audit row + kill-switch record.
 *
 *  The endpoints require ``manifest:write`` (a tenant-admin action); the button
 *  is shown to anyone who can read the detail page and a read-only member gets a
 *  handled 403 toast on click. We deliberately don't hide it: gating on
 *  ``isSystemAdmin`` (the SettingsTenants pattern) would wrongly lock out tenant
 *  admins, and there's no per-permission helper on the auth identity to gate
 *  precisely — the server is the authority. */
function AgentKillSwitch({
  name,
  disabled,
  onChanged,
}: {
  name: string;
  disabled: boolean;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  // Track C W2 — 切入态只读:启用/禁用是写操作,切入目标租户后置灰。
  const isTenantSwitched = useIsTenantSwitched();
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  const trimmed = reason.trim();

  const runDisable = useCallback(async () => {
    setBusy(true);
    try {
      const result = await disableAgent(name, trimmed === "" ? undefined : trimmed);
      message.success(
        t("agent_detail.disable_ok", { cancelled: result.cancelled_runs ?? 0 }),
      );
      setReason("");
      onChanged();
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : t("agent_detail.disable_failed"));
    } finally {
      setBusy(false);
    }
  }, [name, trimmed, message, t, onChanged]);

  const runEnable = useCallback(async () => {
    setBusy(true);
    try {
      await enableAgent(name);
      message.success(t("agent_detail.enable_ok"));
      onChanged();
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : t("agent_detail.enable_failed"));
    } finally {
      setBusy(false);
    }
  }, [name, message, t, onChanged]);

  if (disabled) {
    return (
      <ReadonlyTooltip on={isTenantSwitched}>
        <Popconfirm
          title={t("agent_detail.enable_confirm")}
          onConfirm={runEnable}
          okText={t("agent_detail.enable")}
          cancelText={t("common.cancel")}
          disabled={isTenantSwitched}
        >
          <Button
            size="small"
            loading={busy}
            disabled={isTenantSwitched}
            icon={<CircleCheck size={14} strokeWidth={1.75} />}
            data-testid="agent-enable-btn"
          >
            {t("agent_detail.enable")}
          </Button>
        </Popconfirm>
      </ReadonlyTooltip>
    );
  }

  return (
    <ReadonlyTooltip on={isTenantSwitched}>
      <Popconfirm
        icon={null}
        title={t("agent_detail.disable_confirm_title")}
        description={
          <div style={{ maxWidth: 280 }}>
            <div style={{ marginBottom: 8, color: "var(--ew-text-tertiary)", fontSize: 12 }}>
              {t("agent_detail.disable_confirm_body")}
            </div>
            <Input.TextArea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder={t("agent_detail.disable_reason_placeholder")}
              aria-label={t("agent_detail.disable_reason_placeholder")}
              maxLength={500}
              autoSize={{ minRows: 2, maxRows: 4 }}
            />
          </div>
        }
        onConfirm={runDisable}
        okButtonProps={{ danger: true }}
        okText={t("agent_detail.disable")}
        cancelText={t("common.cancel")}
        disabled={isTenantSwitched}
      >
        <Button
          size="small"
          danger
          loading={busy}
          disabled={isTenantSwitched}
          icon={<Ban size={14} strokeWidth={1.75} />}
          data-testid="agent-disable-btn"
        >
          {t("agent_detail.disable")}
        </Button>
      </Popconfirm>
    </ReadonlyTooltip>
  );
}

export function AgentDetail() {
  const { t } = useTranslation();
  const { name, version, tab } = useParams<{
    name: string;
    version: string;
    tab?: string;
  }>();
  const nav = useNavigate();
  // Track C W2 — 切入态读透传:getAgent 带 ?tenant_id=(组件内直取)。
  const { apiTenantScope } = useTenantScope();

  const [detail, setDetail] = useState<AgentDetailResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!name || !version) return;
    setLoading(true);
    setError(null);
    try {
      const result = await getAgent(name, version, concreteTenantScope(apiTenantScope));
      setDetail(result);
    } catch (err) {
      const message =
        err instanceof ApiError
          ? `${err.code}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "unknown error";
      setError(message);
    } finally {
      setLoading(false);
    }
  }, [name, version, apiTenantScope]);

  /** #2 — post-save refetch WITHOUT the loading skeleton. ``refresh`` flips
   *  ``loading`` and the whole detail body is swapped for a <Skeleton/>,
   *  unmounting ManifestTab (and every bit of editor state with it). The
   *  save path refetches in place instead, so the editor stays mounted. */
  const refreshQuietly = useCallback(async () => {
    if (!name || !version) return;
    try {
      const result = await getAgent(name, version, concreteTenantScope(apiTenantScope));
      setDetail(result);
      setError(null);
    } catch (err) {
      const message =
        err instanceof ApiError
          ? `${err.code}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "unknown error";
      setError(message);
    }
  }, [name, version, apiTenantScope]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const activeTab = tab ?? "overview";

  if (!name || !version) {
    return <Empty description="Missing :name or :version in URL" style={{ marginTop: 80 }} />;
  }

  if (loading) {
    return (
      <div>
        <Skeleton.Input active size="large" style={{ marginBottom: 16 }} />
        <Skeleton active paragraph={{ rows: 6 }} />
      </div>
    );
  }

  if (error !== null || detail === null) {
    return (
      <Alert
        type="error"
        showIcon
        message={t("agent_detail.failed_to_load")}
        description={error ?? "agent not found"}
        data-testid="agent-detail-error"
        style={{ marginTop: 16 }}
      />
    );
  }

  const record = detail.record;

  return (
    <div data-testid="agent-detail-root">
      <PageHeader
        title={record.name}
        icon={<Bot size={20} strokeWidth={1.5} />}
        backTo={{ label: t("nav.agents"), to: "/agents" }}
        actions={
          <AgentKillSwitch
            name={record.name}
            disabled={detail.disabled ?? false}
            onChanged={refresh}
          />
        }
        subtitle={
          <Space size={12} align="center" wrap>
            <Tag color={STATUS_COLOR[record.status] ?? "default"} bordered={false}>
              {t(`agents_page.status_${record.status}`, { defaultValue: record.status })}
            </Tag>
            {detail.disabled && (
              <Tooltip
                title={
                  detail.disable?.reason
                    ? t("agent_detail.disabled_reason", { reason: detail.disable.reason })
                    : undefined
                }
              >
                <Tag color="red" bordered={false} data-testid="agent-disabled-tag">
                  {t("agent_detail.disabled_tag")}
                </Tag>
              </Tooltip>
            )}
            <Text code style={{ fontSize: 12 }}>
              v{record.version}
            </Text>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {record.spec_sha256.slice(0, 12)}…
            </Text>
          </Space>
        }
      />

      <Tabs
        activeKey={activeTab}
        onChange={(k) =>
          nav(`/agents/${encodeURIComponent(name)}/${encodeURIComponent(version)}/${k}`)
        }
        items={[
          { key: "overview", label: t("agent_detail.tab_overview") },
          { key: "manifest", label: t("agent_detail.tab_manifest") },
          { key: "history", label: t("agent_detail.tab_history") },
          { key: "playground", label: t("agent_detail.tab_playground") },
          { key: "users", label: t("agent_detail.tab_users") },
          { key: "conversations", label: t("agent_detail.tab_conversations") },
          { key: "skills", label: t("agent_detail.tab_skills") },
          { key: "triggers", label: t("agent_detail.tab_triggers") },
        ]}
      />

      {activeTab === "overview" && <OverviewTab detail={detail} />}
      {activeTab === "manifest" && <ManifestTab detail={detail} onSaved={refreshQuietly} />}
      {activeTab === "history" && <HistoryTab detail={detail} onRolledBack={refresh} />}
      {activeTab === "playground" && <PlaygroundTab detail={detail} />}
      {activeTab === "users" && <UsersTab detail={detail} />}
      {activeTab === "conversations" && <ConversationsTab detail={detail} />}
      {activeTab === "skills" && <SkillsTab detail={detail} />}
      {activeTab === "triggers" && <TriggersTab detail={detail} />}
      {/* The per-agent Memory tab is gone (conversation-centric IA M3):
          memory is a per-user cross-agent asset — it lives on the user
          detail's Memory tab and the tenant-level /memory governance page. */}
      {![
        "overview",
        "manifest",
        "history",
        "playground",
        "users",
        "conversations",
        "skills",
        "triggers",
      ].includes(activeTab) && (
        <Empty
          description={t("agent_detail.tab_coming_soon", { tab: activeTab })}
          style={{ marginTop: 64 }}
          data-testid="agent-detail-tab-placeholder"
        />
      )}
    </div>
  );
}

function OverviewTab({ detail }: { detail: AgentDetailResponse }) {
  const { t } = useTranslation();
  const r = detail.record;
  // ``r.spec`` is the full manifest ({apiVersion, kind, metadata, spec}) —
  // same「多包一层壳」bug family as PlaygroundTab.tsx:138/:213. ``readSecurity``
  // (form_model.ts) already reads ``specOf(m).sandbox?.network`` correctly for
  // a full manifest; a hand-rolled ``readEgress(spec.sandbox)`` here read
  // ``spec.sandbox`` directly (undefined on a full manifest) and silently
  // always showed the "proxy, no allowlist" default.
  const sec = readSecurity(r.spec);
  const egress = { egress: sec.egress ?? "proxy", allowlist: sec.allowlist ?? [] };
  return (
    <Row gutter={16}>
      <Col span={24}>
        <Card title={t("agent_detail.config_summary")}>
          <dl
            style={{
              display: "grid",
              gridTemplateColumns: "160px 1fr",
              rowGap: 8,
              columnGap: 16,
              margin: 0,
              fontSize: 13,
            }}
          >
            <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("agent_detail.field_id")}</dt>
            <dd style={{ margin: 0 }} className="mono">
              {r.id}
            </dd>
            <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("agent_detail.field_tenant")}</dt>
            <dd style={{ margin: 0 }} className="mono">
              {r.tenant_id}
            </dd>
            <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("agent_detail.field_spec_sha")}</dt>
            <dd style={{ margin: 0 }} className="mono">
              {r.spec_sha256}
            </dd>
            <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("agent_detail.field_status")}</dt>
            <dd style={{ margin: 0 }}>
              {t(`agents_page.status_${r.status}`, { defaultValue: r.status })}
            </dd>
            <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("agent_detail.field_created")}</dt>
            <dd style={{ margin: 0 }}>
              {new Date(r.created_at).toLocaleString()} · {r.created_by}
            </dd>
            <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("agent_detail.field_updated")}</dt>
            <dd style={{ margin: 0 }}>{new Date(r.updated_at).toLocaleString()}</dd>
          </dl>
        </Card>
      </Col>
      <Col span={24} style={{ marginTop: 16 }}>
        <Card title={t("agent_detail.egress_title")} data-testid="agent-egress-card">
          <dl
            style={{
              display: "grid",
              gridTemplateColumns: "160px 1fr",
              rowGap: 8,
              columnGap: 16,
              margin: 0,
              fontSize: 13,
            }}
          >
            <dt style={{ color: "var(--ew-text-tertiary)" }}>
              {t("agent_detail.egress_policy")}
            </dt>
            <dd style={{ margin: 0 }}>
              {egress.egress === "none" ? (
                <Tag icon={<ShieldOff size={11} strokeWidth={1.75} />}>
                  {t("agent_detail.egress_isolated")}
                </Tag>
              ) : (
                <Tag color="cyan" icon={<Network size={11} strokeWidth={1.75} />}>
                  {t("agent_detail.egress_proxied")}
                </Tag>
              )}
            </dd>
            <dt style={{ color: "var(--ew-text-tertiary)" }}>
              {t("agent_detail.egress_allowlist")}
            </dt>
            <dd style={{ margin: 0 }}>
              {egress.egress === "none" ? (
                <span style={{ color: "var(--ew-text-tertiary)" }}>—</span>
              ) : egress.allowlist.length === 0 ? (
                <span style={{ color: "var(--ew-text-tertiary)" }}>
                  {t("agent_detail.egress_allow_all")}
                </span>
              ) : (
                <Space size={[4, 4]} wrap>
                  {egress.allowlist.map((host) => (
                    <Tag key={host} className="mono">
                      {host}
                    </Tag>
                  ))}
                </Space>
              )}
            </dd>
          </dl>
        </Card>
      </Col>
    </Row>
  );
}
