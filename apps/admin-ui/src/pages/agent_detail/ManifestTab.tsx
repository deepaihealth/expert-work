/**
 * Manifest tab — visual-first config (mirrors the create flow).
 *
 * Renders the same schema-driven :class:`ManifestEditor` the create modal
 * uses (Form tabs: basic / model / prompt / tools / mcp / knowledge /
 * skills / subagents / memory / governance, plus a raw YAML escape-hatch
 * tab). Edits accumulate in a buffer; ``Save`` writes through
 * ``PUT /v1/agents/{name}/{version}`` (the backend re-runs the full
 * :class:`ManifestLoader`, so server errors flow back through the
 * envelope); ``Reset`` re-derives the buffer from the latest server
 * snapshot by remounting the editor.
 *
 * Form-position persistence (#2): the editor is keyed by ``resetNonce``
 * only — a refreshed server snapshot flows in through ``initialYaml``
 * (the editor resyncs in place) instead of forcing a remount that reset
 * the whole form position on every save. The active config group also
 * persists in the URL (``?group=``) so it survives remounts and tab
 * switches.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Alert, Button, Card, Space, Typography } from "antd";
import { dump as yamlDump } from "js-yaml";
import { RotateCcw, Save } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../../api/client";
import { updateAgent, type AgentDetailResponse } from "../../api/agents";
import { ManifestEditor } from "../../components/manifest-editor";
import { CONFIG_GROUPS } from "../../components/manifest-editor/groups";
import { ReadonlyTooltip } from "../../components/ReadonlyTooltip";
import { useIsTenantSwitched } from "../../tenant/useIsTenantSwitched";

const { Text } = Typography;

interface ManifestTabProps {
  detail: AgentDetailResponse;
  /** Called after a successful save — parent refetches so the SHA, the
   *  ``updated_at`` timestamp, and any server-side coercion show up. */
  onSaved: () => void;
}

export function ManifestTab({ detail, onSaved }: ManifestTabProps) {
  const { t } = useTranslation();
  const r = detail.record;
  // Track C W2 — 切入态只读:保存是写操作,切入目标租户后置灰。
  const isTenantSwitched = useIsTenantSwitched();

  /** Server snapshot serialised as YAML — the editor's seed. */
  const snapshotYaml = useMemo(() => yamlDump(r.spec, { lineWidth: 120 }), [r.spec]);

  const [buffer, setBuffer] = useState<string>(snapshotYaml);
  // Bumped on Reset to remount the editor and re-seed it from the snapshot.
  const [resetNonce, setResetNonce] = useState(0);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // #2 — the editor is no longer key-remounted when the server snapshot
  // changes; adopt the refreshed snapshot into the buffer instead. In
  // practice ``snapshotYaml`` only changes right after a successful save
  // (the parent's quiet refetch), so this is the explicit post-save resync,
  // not a mid-edit clobber.
  useEffect(() => {
    setBuffer(snapshotYaml);
  }, [snapshotYaml]);

  // #2 — active config group lives in the URL (``?group=``): it survives the
  // editor remounting (Reset), page-tab switches, even a full reload. An
  // unknown/absent value falls back to the editor's own default (basic).
  const [searchParams, setSearchParams] = useSearchParams();
  const groupParam = searchParams.get("group");
  const activeGroup =
    groupParam !== null && CONFIG_GROUPS.some((g) => g.id === groupParam)
      ? groupParam
      : undefined;
  const handleGroupChange = useCallback(
    (id: string) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.set("group", id);
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  const handleReset = useCallback(() => {
    setBuffer(snapshotYaml);
    setError(null);
    setResetNonce((n) => n + 1);
  }, [snapshotYaml]);

  const handleSave = useCallback(async () => {
    setSaving(true);
    setError(null);
    try {
      await updateAgent(r.name, r.version, { manifest_yaml: buffer });
      onSaved();
    } catch (err) {
      const message =
        err instanceof ApiError
          ? `${err.code}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "unknown error";
      setError(message);
    } finally {
      setSaving(false);
    }
  }, [buffer, r.name, r.version, onSaved]);

  return (
    <Card data-testid="manifest-tab">
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 12,
        }}
      >
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t("manifest_tab.hint")}
        </Text>
        <Space size={8}>
          <Button
            size="small"
            icon={<RotateCcw size={14} strokeWidth={1.75} />}
            onClick={handleReset}
            disabled={saving}
            data-testid="manifest-reset-btn"
          >
            {t("manifest_tab.reset")}
          </Button>
          <ReadonlyTooltip on={isTenantSwitched}>
            <Button
              size="small"
              type="primary"
              icon={<Save size={14} strokeWidth={1.75} />}
              onClick={handleSave}
              loading={saving}
              disabled={isTenantSwitched}
              data-testid="manifest-save-btn"
            >
              {t("manifest_tab.save")}
            </Button>
          </ReadonlyTooltip>
        </Space>
      </div>

      {error !== null && (
        <Alert
          type="error"
          showIcon
          message={t("manifest_tab.save_failed")}
          description={error}
          style={{ marginBottom: 12 }}
          data-testid="manifest-error"
        />
      )}

      <ManifestEditor
        key={resetNonce}
        mode="edit"
        initialYaml={snapshotYaml}
        onChange={setBuffer}
        activeGroup={activeGroup}
        onActiveGroupChange={handleGroupChange}
      />
    </Card>
  );
}
