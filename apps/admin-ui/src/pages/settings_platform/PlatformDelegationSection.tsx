/**
 * Platform delegation-gate capacity section (perf phase2 PR3).
 *
 * Self-contained section: GETs the platform delegation-gate capacity on
 * mount and shows one number input for the resolved (effective) cap on
 * concurrent sub-agent delegations across the whole platform (per process,
 * shared by every run). Saving writes an explicit
 * platform override that takes effect on the next run/build — no redeploy,
 * overriding the process's built-in default. system_admin-only at the route
 * level; surfaces backend errors.
 */
import { useCallback, useEffect, useState, type ReactElement } from "react";
import { Alert, App, Button, InputNumber, Space, Spin, Tag, Typography } from "antd";
import { useTranslation } from "react-i18next";

import {
  getPlatformDelegationConfig,
  putPlatformDelegationConfig,
  type DelegationCapacity,
  type PlatformDelegationConfigView,
} from "../../api/platform_delegation_config";
import { ApiError } from "../../api/client";

const { Paragraph } = Typography;

export interface PlatformDelegationSectionProps {
  /** Invoked after a successful save (so a parent page can refresh/notify). */
  onSaved?: () => void;
}

export function PlatformDelegationSection({
  onSaved,
}: PlatformDelegationSectionProps): ReactElement {
  const { t } = useTranslation();
  const { message } = App.useApp();

  const [view, setView] = useState<PlatformDelegationConfigView | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  // ``null`` while the field is transiently empty during editing (e.g. the
  // user has cleared it but hasn't typed a new digit yet) — coercing to a
  // fallback number immediately would fight the user's keystrokes.
  const [maxConcurrentDelegations, setMaxConcurrentDelegations] = useState<number | null>(1);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const next = await getPlatformDelegationConfig();
      setView(next);
      setMaxConcurrentDelegations(next.effective.max_concurrent_delegations);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : "unknown error");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const hasEmptyField = maxConcurrentDelegations === null;

  const onSave = useCallback(async () => {
    if (maxConcurrentDelegations === null) {
      return;
    }
    const capacity: DelegationCapacity = {
      max_concurrent_delegations: maxConcurrentDelegations,
    };
    setSaving(true);
    try {
      setView(await putPlatformDelegationConfig(capacity));
      message.success(t("settings_platform.delegation_saved"));
      onSaved?.();
    } catch (err) {
      message.error(
        err instanceof ApiError
          ? `${err.code}: ${err.message}`
          : t("settings_platform.delegation_save_failed"),
      );
    } finally {
      setSaving(false);
    }
  }, [maxConcurrentDelegations, message, t, onSaved]);

  if (loading) {
    return (
      <div
        style={{ padding: 24, textAlign: "center" }}
        data-testid="pdg-loading"
      >
        <Spin />
      </div>
    );
  }

  if (loadError !== null || view === null) {
    return (
      <Alert
        type="error"
        showIcon
        message={t("settings_platform.delegation_heading")}
        description={loadError ?? "unknown error"}
        data-testid="pdg-load-error"
      />
    );
  }

  return (
    <div data-testid="pdg-root">
      <Alert
        type="info"
        showIcon
        message={t("settings_platform.delegation_help_title")}
        description={t("settings_platform.delegation_help_body")}
        style={{ marginBottom: 16 }}
        data-testid="pdg-help"
      />

      <Space direction="vertical" size={12}>
        <Space align="center">
          <span>{t("settings_platform.delegation_max_concurrent_delegations_label")}</span>
          <InputNumber
            min={1}
            max={64}
            value={maxConcurrentDelegations}
            onChange={setMaxConcurrentDelegations}
            aria-label={t("settings_platform.delegation_max_concurrent_delegations_label")}
            data-testid="pdg-max-concurrent-delegations"
          />
        </Space>
        {view.configured === null && (
          <Tag data-testid="pdg-env-default">
            {t("settings_platform.delegation_env_default")}
          </Tag>
        )}
      </Space>

      <div style={{ marginTop: 16 }}>
        <Button
          type="primary"
          loading={saving}
          disabled={hasEmptyField}
          onClick={onSave}
          data-testid="pdg-save"
        >
          {t("settings_platform.delegation_save")}
        </Button>
      </div>

      <Paragraph
        type="secondary"
        style={{ marginTop: 8 }}
        data-testid="pdg-hint"
      >
        {t("settings_platform.delegation_hint")}
      </Paragraph>
    </div>
  );
}
