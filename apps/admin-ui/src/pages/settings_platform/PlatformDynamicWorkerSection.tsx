/**
 * Platform dynamic-worker guardrail section (B3 PR2; 弹性 worker 预算).
 *
 * Self-contained section: GETs the platform dynamic-worker limits on mount
 * and shows the two guardrail tiers — the *default* tier (what an agent gets
 * when its manifest doesn't ask: per-run concurrency, per-run cumulative
 * spawn cap, per-worker step cap) and the *hard-cap* tier (the ceiling a
 * per-agent ``dynamic_workers.max_*`` request is clamped to). Saving writes
 * an explicit platform override that takes effect on the next run/build — no
 * redeploy, overriding the process's env-default settings snapshot.
 * system_admin-only at the route level; surfaces backend errors.
 */
import { useCallback, useEffect, useState, type ReactElement } from "react";
import { Alert, App, Button, InputNumber, Space, Spin, Tag, Typography } from "antd";
import { useTranslation } from "react-i18next";

import {
  getPlatformDynamicWorkerConfig,
  putPlatformDynamicWorkerConfig,
  type DynamicWorkerLimits,
  type PlatformDynamicWorkerConfigView,
} from "../../api/platform_dynamic_worker_config";
import { ApiError } from "../../api/client";

const { Paragraph, Text } = Typography;

type Knob = keyof DynamicWorkerLimits;

/** default-tier knob → its hard-cap counterpart(校验 default ≤ cap 用)。 */
const KNOB_PAIRS: ReadonlyArray<{ defaultKey: Knob; capKey: Knob }> = [
  { defaultKey: "max_concurrent", capKey: "cap_max_concurrent" },
  { defaultKey: "max_per_run", capKey: "cap_max_per_run" },
  { defaultKey: "max_iterations", capKey: "cap_max_iterations" },
];

/** Wide sanity bounds — mirror the API's static ge/le; the meaningful
 * invariant (default ≤ cap) is checked separately. */
const KNOB_MAX: Record<Knob, number> = {
  max_concurrent: 64,
  max_per_run: 1024,
  max_iterations: 512,
  cap_max_concurrent: 64,
  cap_max_per_run: 1024,
  cap_max_iterations: 512,
};

const KNOB_TESTID: Record<Knob, string> = {
  max_concurrent: "pdw-max-concurrent",
  max_per_run: "pdw-max-per-run",
  max_iterations: "pdw-max-iterations",
  cap_max_concurrent: "pdw-cap-max-concurrent",
  cap_max_per_run: "pdw-cap-max-per-run",
  cap_max_iterations: "pdw-cap-max-iterations",
};

const KNOB_LABEL_KEY: Record<Knob, string> = {
  max_concurrent: "settings_platform.dynamic_worker_max_concurrent_label",
  max_per_run: "settings_platform.dynamic_worker_max_per_run_label",
  max_iterations: "settings_platform.dynamic_worker_max_iterations_label",
  cap_max_concurrent: "settings_platform.dynamic_worker_cap_max_concurrent_label",
  cap_max_per_run: "settings_platform.dynamic_worker_cap_max_per_run_label",
  cap_max_iterations: "settings_platform.dynamic_worker_cap_max_iterations_label",
};

// ``null`` while a field is transiently empty during editing (e.g. the user
// has cleared it but hasn't typed a new digit yet) — coercing to a fallback
// number immediately would fight the user's keystrokes.
type Draft = Record<Knob, number | null>;

export interface PlatformDynamicWorkerSectionProps {
  /** Invoked after a successful save (so a parent page can refresh/notify). */
  onSaved?: () => void;
}

export function PlatformDynamicWorkerSection({
  onSaved,
}: PlatformDynamicWorkerSectionProps): ReactElement {
  const { t } = useTranslation();
  const { message } = App.useApp();

  const [view, setView] = useState<PlatformDynamicWorkerConfigView | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [draft, setDraft] = useState<Draft>({
    max_concurrent: 1,
    max_per_run: 1,
    max_iterations: 1,
    cap_max_concurrent: 1,
    cap_max_per_run: 1,
    cap_max_iterations: 1,
  });

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const next = await getPlatformDynamicWorkerConfig();
      setView(next);
      setDraft({ ...next.effective });
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : "unknown error");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // ``== null`` 兜 undefined:GET 载荷缺键(老形状/异常)时禁存,而不是
  // 发出一个缺字段的 PUT 去撞后端 422。
  const hasEmptyField = Object.values(draft).some((v) => v == null);
  const pairsAboveCap = KNOB_PAIRS.filter(({ defaultKey, capKey }) => {
    const d = draft[defaultKey];
    const c = draft[capKey];
    return d !== null && c !== null && d > c;
  });

  const onSave = useCallback(async () => {
    if (hasEmptyField || pairsAboveCap.length > 0) {
      return;
    }
    setSaving(true);
    try {
      setView(await putPlatformDynamicWorkerConfig(draft as DynamicWorkerLimits));
      message.success(t("settings_platform.dynamic_worker_saved"));
      onSaved?.();
    } catch (err) {
      message.error(
        err instanceof ApiError
          ? `${err.code}: ${err.message}`
          : t("settings_platform.dynamic_worker_save_failed"),
      );
    } finally {
      setSaving(false);
    }
  }, [draft, hasEmptyField, pairsAboveCap, message, t, onSaved]);

  if (loading) {
    return (
      <div
        style={{ padding: 24, textAlign: "center" }}
        data-testid="pdw-loading"
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
        message={t("settings_platform.dynamic_worker_heading")}
        description={loadError ?? "unknown error"}
        data-testid="pdw-load-error"
      />
    );
  }

  const renderKnob = (key: Knob) => (
    <Space align="center" key={key}>
      <span>{t(KNOB_LABEL_KEY[key])}</span>
      <InputNumber
        min={1}
        max={KNOB_MAX[key]}
        value={draft[key]}
        onChange={(v) => setDraft((prev) => ({ ...prev, [key]: v }))}
        aria-label={t(KNOB_LABEL_KEY[key])}
        data-testid={KNOB_TESTID[key]}
      />
    </Space>
  );

  return (
    <div data-testid="pdw-root">
      <Alert
        type="info"
        showIcon
        message={t("settings_platform.dynamic_worker_help_title")}
        description={t("settings_platform.dynamic_worker_help_body")}
        style={{ marginBottom: 16 }}
        data-testid="pdw-help"
      />

      <Space direction="vertical" size={12}>
        <Text strong>{t("settings_platform.dynamic_worker_default_tier_heading")}</Text>
        {KNOB_PAIRS.map(({ defaultKey }) => renderKnob(defaultKey))}
        <Text strong style={{ marginTop: 8, display: "inline-block" }}>
          {t("settings_platform.dynamic_worker_cap_tier_heading")}
        </Text>
        {KNOB_PAIRS.map(({ capKey }) => renderKnob(capKey))}
        {view.configured === null && (
          <Tag data-testid="pdw-env-default">
            {t("settings_platform.dynamic_worker_env_default")}
          </Tag>
        )}
      </Space>

      {pairsAboveCap.length > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginTop: 12 }}
          message={t("settings_platform.dynamic_worker_default_above_cap")}
          data-testid="pdw-default-above-cap"
        />
      )}

      <div style={{ marginTop: 16 }}>
        <Button
          type="primary"
          loading={saving}
          disabled={hasEmptyField || pairsAboveCap.length > 0}
          onClick={onSave}
          data-testid="pdw-save"
        >
          {t("settings_platform.dynamic_worker_save")}
        </Button>
      </div>

      <Paragraph
        type="secondary"
        style={{ marginTop: 8 }}
        data-testid="pdw-hint"
      >
        {t("settings_platform.dynamic_worker_hint")}
      </Paragraph>
    </div>
  );
}
