/**
 * Plain controlled model picker for the curated agent form. Linked
 * provider→model dropdowns (configured providers only, from the model catalog);
 * selecting a model copies its vision capability into supports_vision. A
 * temperature slider is surfaced directly; the remaining scalars
 * (max_tokens, rate_limit_rpm) live in a collapsed "Advanced" panel.
 *
 * Refactored from the old RJSF model field — no RJSF coupling.
 */
import { Collapse, InputNumber, Select, Slider, Switch, Tag, Tooltip } from "antd";
import { useTranslation } from "react-i18next";

import type { ModelCatalog } from "../../../api/model_catalog";
import type { ModelFields } from "../form_model";
import { lookupModel, modelsFor, providerNames } from "../catalog";

interface ModelSelectProps {
  value: ModelFields;
  catalog?: ModelCatalog;
  onChange: (next: ModelFields) => void;
  /** Restrict to vision-capable models — hides providers with no vision model
   *  and non-vision models within a provider. Used by the VL fallback picker. */
  visionOnly?: boolean;
}

export function ModelSelect({
  value,
  catalog,
  onChange,
  visionOnly = false,
}: ModelSelectProps) {
  const { t } = useTranslation();
  const allProviders = catalog ? providerNames(catalog) : [];
  const providers =
    catalog && visionOnly
      ? allProviders.filter((p) => modelsFor(catalog, p).some((m) => m.vision))
      : allProviders;
  const allModels =
    catalog && value.provider ? modelsFor(catalog, value.provider) : [];
  const models = visionOnly ? allModels.filter((m) => m.vision) : allModels;

  function onProvider(provider: string): void {
    onChange({
      ...value,
      provider,
      name: undefined,
      supports_vision: false,
      thinking_enabled: undefined,
      effort: undefined,
      adaptive_thinking: undefined,
      cache_enabled: undefined,
    });
  }
  function onModel(name: string): void {
    const entry =
      catalog && value.provider
        ? lookupModel(catalog, value.provider, name)
        : undefined;
    onChange({
      ...value,
      name,
      supports_vision: entry?.vision ?? false,
      // Thinking-Toggle — seed the switch from the model's real default; a
      // model with no thinking knob clears the field (manifest stays clean).
      thinking_enabled: entry?.thinking ? (entry.thinking_default ?? false) : undefined,
      // effort only makes sense for a thinking model — scrub it on downgrade
      // to a non-thinking model (agent_factory rejects effort with no
      // thinking knob); survives thinking→thinking switches.
      effort: entry?.thinking ? value.effort : undefined,
    });
  }

  const currentEntry =
    catalog && value.provider && value.name
      ? lookupModel(catalog, value.provider, value.name)
      : undefined;
  const hasThinkingKnob = !!currentEntry?.thinking;
  // reasoning_effort vendors have no off level — off degrades to "minimal".
  // GLM (5.2+, shape "effort") keeps a real off via thinking.type=disabled.
  const cannotFullyDisable =
    currentEntry?.thinking === "effort" &&
    value.provider !== "anthropic" &&
    value.provider !== "glm";
  const thinkingOn = value.thinking_enabled ?? currentEntry?.thinking_default ?? false;

  const temperature = value.temperature ?? 0.2;

  return (
    <div data-testid="model-select-field">
      <div data-testid="model-select-provider" style={{ marginBottom: 8 }}>
        <Select
          aria-label={t("model_select.provider_label")}
          placeholder={t("model_select.provider_placeholder")}
          loading={!catalog}
          disabled={!catalog}
          value={value.provider}
          onChange={onProvider}
          options={providers.map((p) => ({ label: p, value: p }))}
          style={{ width: "100%" }}
        />
      </div>
      <div data-testid="model-select-name" style={{ marginBottom: 8 }}>
        <Select
          aria-label={t("model_select.model_label")}
          placeholder={t("model_select.model_placeholder")}
          disabled={!value.provider}
          value={value.name}
          onChange={onModel}
          options={models.map((m) => ({ label: m.name, value: m.name }))}
          style={{ width: "100%" }}
        />
      </div>
      <div data-testid="model-select-vision" style={{ marginBottom: 8 }}>
        <Tag color={value.supports_vision ? "cyan" : "default"}>
          {value.supports_vision
            ? t("model_select.vision_on")
            : t("model_select.vision_off")}
        </Tag>
      </div>
      {hasThinkingKnob && (
        <div
          data-testid="model-select-thinking"
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            marginBottom: 8,
          }}
        >
          <Switch
            size="small"
            checked={thinkingOn}
            aria-label={t("model_select.thinking_label")}
            onChange={(checked) =>
              onChange({ ...value, thinking_enabled: checked })
            }
          />
          <span>{t("model_select.thinking_label")}</span>
          {cannotFullyDisable && (
            <Tooltip title={t("model_select.thinking_cannot_disable")}>
              <span
                data-testid="model-select-thinking-hint"
                style={{ cursor: "help", opacity: 0.6 }}
                aria-label={t("model_select.thinking_cannot_disable")}
              >
                ⓘ
              </span>
            </Tooltip>
          )}
        </div>
      )}
      <label
        data-testid="model-select-temperature"
        style={{ display: "block", marginBottom: 8 }}
      >
        <span style={{ display: "block", marginBottom: 4 }}>
          {t("model_select.temperature")}: {temperature}
        </span>
        <Slider
          min={0}
          max={2}
          step={0.1}
          value={temperature}
          ariaLabelForHandle={t("model_select.temperature")}
          onChange={(v) => onChange({ ...value, temperature: v })}
        />
      </label>
      <Collapse
        data-testid="model-select-advanced"
        defaultActiveKey={[]}
        items={[
          {
            key: "advanced",
            label: t("model_select.advanced"),
            children: (
              <>
                <label style={{ display: "block", marginBottom: 8 }}>
                  <span style={{ display: "block", marginBottom: 4 }}>
                    max_tokens
                  </span>
                  <InputNumber
                    value={value.max_tokens}
                    onChange={(v) =>
                      onChange({ ...value, max_tokens: v ?? undefined })
                    }
                    style={{ width: "100%" }}
                  />
                  <span
                    style={{
                      display: "block",
                      marginTop: 4,
                      fontSize: 12,
                      color: "var(--ew-text-muted, #888)",
                    }}
                  >
                    {t("model_select.max_tokens_hint")}
                  </span>
                </label>
                <label style={{ display: "block", marginBottom: 8 }}>
                  <span style={{ display: "block", marginBottom: 4 }}>
                    rate_limit_rpm
                  </span>
                  <InputNumber
                    value={value.rate_limit_rpm}
                    onChange={(v) =>
                      onChange({ ...value, rate_limit_rpm: v ?? undefined })
                    }
                    style={{ width: "100%" }}
                  />
                  <span
                    style={{
                      display: "block",
                      marginTop: 4,
                      fontSize: 12,
                      color: "var(--ew-text-muted, #888)",
                    }}
                  >
                    {t("model_select.rate_limit_hint")}
                  </span>
                </label>
                <label style={{ display: "block", marginBottom: 8 }}>
                  <span style={{ display: "block", marginBottom: 4 }}>
                    {t("model_select.context_window")}
                  </span>
                  <InputNumber
                    value={value.context_window}
                    min={1}
                    onChange={(v) =>
                      onChange({ ...value, context_window: v ?? undefined })
                    }
                    style={{ width: "100%" }}
                    aria-label={t("model_select.context_window")}
                    data-testid="model-select-context-window"
                  />
                  <span
                    style={{
                      display: "block",
                      marginTop: 4,
                      fontSize: 12,
                      color: "var(--ew-text-muted, #888)",
                    }}
                  >
                    {t("model_select.context_window_hint")}
                  </span>
                </label>
                {hasThinkingKnob && (
                  <label
                    data-testid="model-select-effort"
                    style={{ display: "block", marginBottom: 8 }}
                  >
                    <span style={{ display: "block", marginBottom: 4 }}>
                      {t("model_select.effort_label")}
                    </span>
                    <Select
                      allowClear
                      aria-label={t("model_select.effort_label")}
                      value={value.effort}
                      onChange={(v) =>
                        onChange({ ...value, effort: v ?? undefined })
                      }
                      options={[
                        { label: "low", value: "low" },
                        { label: "medium", value: "medium" },
                        { label: "high", value: "high" },
                        { label: "max", value: "max" },
                      ]}
                      style={{ width: "100%" }}
                    />
                    <span
                      style={{
                        display: "block",
                        marginTop: 4,
                        fontSize: 12,
                        color: "var(--ew-text-muted, #888)",
                      }}
                    >
                      {t("model_select.effort_hint")}
                    </span>
                  </label>
                )}
                {value.provider === "anthropic" && (
                  <div
                    data-testid="model-select-adaptive"
                    style={{ marginBottom: 8 }}
                  >
                    <div
                      style={{ display: "flex", alignItems: "center", gap: 8 }}
                    >
                      <Switch
                        size="small"
                        checked={value.adaptive_thinking === true}
                        aria-label={t("model_select.adaptive_label")}
                        onChange={(checked) =>
                          onChange({
                            ...value,
                            adaptive_thinking: checked ? true : undefined,
                          })
                        }
                      />
                      <span>{t("model_select.adaptive_label")}</span>
                    </div>
                    <span
                      style={{
                        display: "block",
                        marginTop: 4,
                        fontSize: 12,
                        color: "var(--ew-text-muted, #888)",
                      }}
                    >
                      {t("model_select.adaptive_hint")}
                    </span>
                  </div>
                )}
                {value.provider === "anthropic" && (
                  <div
                    data-testid="model-select-cache"
                    style={{ marginBottom: 8 }}
                  >
                    <div
                      style={{ display: "flex", alignItems: "center", gap: 8 }}
                    >
                      <Switch
                        size="small"
                        checked={value.cache_enabled !== false}
                        aria-label={t("model_select.cache_label")}
                        onChange={(checked) =>
                          onChange({
                            ...value,
                            cache_enabled: checked ? undefined : false,
                          })
                        }
                      />
                      <span>{t("model_select.cache_label")}</span>
                    </div>
                    <span
                      style={{
                        display: "block",
                        marginTop: 4,
                        fontSize: 12,
                        color: "var(--ew-text-muted, #888)",
                      }}
                    >
                      {t("model_select.cache_hint")}
                    </span>
                  </div>
                )}
              </>
            ),
          },
        ]}
      />
    </div>
  );
}
