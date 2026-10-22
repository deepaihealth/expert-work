/**
 * Platform 「文档识别」 section (B-209 §5.7).
 *
 * Which models the knowledge base reads scanned PDF pages with (primary +
 * fallback), which model describes figures (used by the figure step), and how
 * many pages one document may send to recognition. Platform-wide — every
 * tenant shares it; recognition is billed to the uploading tenant. Each model
 * slot can be cleared (= switched off: scanned pages then wait for
 * 「补识别」 instead of being read). system_admin-only at the route level.
 */
import { useCallback, useEffect, useState, type ReactElement } from "react";
import { Alert, App, Button, InputNumber, Select, Space, Spin, Tag, Typography } from "antd";
import { useTranslation } from "react-i18next";

import {
  getPlatformRecognitionConfig,
  putPlatformRecognitionConfig,
  type PlatformRecognitionConfig,
  type PlatformRecognitionConfigView,
  type ProviderModel,
} from "../../api/platform_recognition_config";
import { ApiError } from "../../api/client";

const { Paragraph } = Typography;

/** The per-document page cap the backend accepts. */
const MAX_PAGES_LIMIT = 10_000;

type Slot = "ocr" | "fallback" | "figure";

interface FormState {
  ocr: string | null;
  fallback: string | null;
  figure: string | null;
  maxPages: number;
}

function key(pair: ProviderModel | null): string | null {
  return pair === null ? null : `${pair.provider}/${pair.model}`;
}

function split(value: string | null): [string | null, string | null] {
  if (value === null) return [null, null];
  const at = value.indexOf("/");
  return [value.slice(0, at), value.slice(at + 1)];
}

function toForm(config: PlatformRecognitionConfig): FormState {
  return {
    ocr: key(config.ocr),
    fallback: key(config.fallback),
    figure: key(config.figure),
    maxPages: config.max_pages,
  };
}

function options(models: readonly ProviderModel[]): { label: string; value: string }[] {
  return models.map((m) => ({ label: `${m.provider} / ${m.model}`, value: `${m.provider}/${m.model}` }));
}

export interface PlatformRecognitionSectionProps {
  onSaved?: () => void;
}

export function PlatformRecognitionSection({
  onSaved,
}: PlatformRecognitionSectionProps): ReactElement {
  const { t } = useTranslation();
  const { message } = App.useApp();

  const [view, setView] = useState<PlatformRecognitionConfigView | null>(null);
  const [form, setForm] = useState<FormState | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const next = await getPlatformRecognitionConfig();
      setView(next);
      setForm(toForm(next));
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : "unknown error");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const errMessage = useCallback(
    (err: ApiError): string => {
      const translated = t(`settings_platform.recognition_err_${err.code}`);
      return translated === `settings_platform.recognition_err_${err.code}` ? err.message : translated;
    },
    [t],
  );

  const save = useCallback(async () => {
    if (form === null || view === null) return;
    setSaving(true);
    setSaveError(null);
    const [ocrProvider, ocrModel] = split(form.ocr);
    const [fallbackProvider, fallbackModel] = split(form.fallback);
    const [figureProvider, figureModel] = split(form.figure);
    try {
      const saved = await putPlatformRecognitionConfig({
        ocr_provider: ocrProvider,
        ocr_model: ocrModel,
        fallback_provider: fallbackProvider,
        fallback_model: fallbackModel,
        figure_provider: figureProvider,
        figure_model: figureModel,
        max_pages: form.maxPages,
      });
      setView({ ...view, ...saved });
      setForm(toForm(saved));
      message.success(t("settings_platform.recognition_saved"));
      onSaved?.();
    } catch (err) {
      setSaveError(
        err instanceof ApiError ? errMessage(err) : t("settings_platform.recognition_save_failed"),
      );
    } finally {
      setSaving(false);
    }
  }, [form, view, message, t, onSaved, errMessage]);

  if (loading) {
    return (
      <div style={{ padding: 24, textAlign: "center" }} data-testid="pr-loading">
        <Spin />
      </div>
    );
  }

  if (loadError !== null || view === null || form === null) {
    return (
      <Alert
        type="error"
        showIcon
        message={t("settings_platform.recognition_load_failed")}
        description={loadError ?? "unknown error"}
        data-testid="pr-load-error"
      />
    );
  }

  const slot = (name: Slot, models: readonly ProviderModel[]): ReactElement => (
    <label>
      <div style={{ marginBottom: 4 }}>{t(`settings_platform.recognition_${name}_label`)}</div>
      <div data-testid={`pr-${name}`}>
        <Select
          style={{ width: "100%" }}
          allowClear
          placeholder={t("settings_platform.recognition_off")}
          value={form[name] ?? undefined}
          onChange={(value: string | undefined) => setForm({ ...form, [name]: value ?? null })}
          options={options(models)}
          aria-label={t(`settings_platform.recognition_${name}_label`)}
        />
      </div>
      <Paragraph type="secondary" style={{ margin: "4px 0 0", fontSize: 12 }}>
        {t(`settings_platform.recognition_${name}_hint`)}
      </Paragraph>
    </label>
  );

  return (
    <div data-testid="pr-root">
      <Alert
        type="info"
        showIcon
        message={t("settings_platform.recognition_help_title")}
        description={t("settings_platform.recognition_help_body")}
        style={{ marginBottom: 16 }}
        data-testid="pr-help"
      />
      {view.is_default && (
        <Tag style={{ marginBottom: 12 }} data-testid="pr-default">
          {t("settings_platform.recognition_default")}
        </Tag>
      )}
      <div style={{ display: "flex", flexDirection: "column", gap: 16, maxWidth: 480 }}>
        {slot("ocr", view.available_ocr)}
        {slot("fallback", view.available_ocr)}
        {slot("figure", view.available_figure)}
        <label>
          <div style={{ marginBottom: 4 }}>{t("settings_platform.recognition_max_pages_label")}</div>
          <InputNumber
            min={1}
            max={MAX_PAGES_LIMIT}
            precision={0}
            value={form.maxPages}
            onChange={(value: number | null) =>
              setForm({ ...form, maxPages: value ?? form.maxPages })
            }
            aria-label={t("settings_platform.recognition_max_pages_label")}
            data-testid="pr-max-pages"
          />
          <Paragraph type="secondary" style={{ margin: "4px 0 0", fontSize: 12 }}>
            {t("settings_platform.recognition_max_pages_hint")}
          </Paragraph>
        </label>
        {saveError !== null && (
          <Alert type="error" showIcon message={saveError} data-testid="pr-error" />
        )}
        <Space>
          <Button
            type="primary"
            loading={saving}
            disabled={saving}
            onClick={() => void save()}
            data-testid="pr-save"
          >
            {t("settings_platform.recognition_save")}
          </Button>
        </Space>
      </div>
    </div>
  );
}
