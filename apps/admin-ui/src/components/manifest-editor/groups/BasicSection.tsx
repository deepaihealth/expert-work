/**
 * BasicSection — "基础" (Basic) group, config-page redesign v2 Task 6. The
 * group used to render the plain "basic" ``FormView`` section (name +
 * description) via ``ManifestEditor``'s stacked-sections fallback; this
 * curated pane adds ``RunProfileCard`` ABOVE that same ``FormView``, unchanged.
 *
 * ``RunProfileCard`` is a one-click preset over 18 fields scattered across
 * the memory/budget/context groups (``RUN_PROFILES``, form_model.ts): pick
 * "balanced" / "cost-saving" / "high-capability" and every managed field
 * jumps to that preset's value in one write (``applyRunProfile``). The
 * radio reflects the CURRENT manifest (``inferRunProfile``): checked when
 * all 18 fields exactly match a preset, otherwise none checked + a "Custom"
 * tag — any individual field can still be hand-tuned afterward in its own
 * group without the card fighting that edit (it just reads as "custom").
 *
 * Picking a preset that would change nothing (``countProfileDiff`` — the
 * PICKED radio, since a preset that already matches wouldn't render as
 * pickable to begin with under a native ``Radio.Group``) applies instantly;
 * otherwise an ``App.useApp()`` modal.confirm names how many settings will change before
 * committing — a run profile touches fields the user may not be looking at
 * (e.g. compression thresholds while on the "basic" tab), so silently
 * overwriting them without warning would surprise.
 *
 * This does NOT affect ``AgentTemplateConfigForm``'s leading-tab path: that
 * merges the "basic" ``FormView`` section into its OWN tab directly
 * (``ManifestEditor``'s ``lt.mergeSection`` branch, a separate render path
 * that never consults ``CURATED_GROUP_PANES``), so a template's basic-info
 * tab never gains a run-profile card.
 */
import { App, Radio, Tag, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { FormView } from "../FormView";
import {
  applyRunProfile,
  countProfileDiff,
  inferRunProfile,
  RUN_PROFILE_IDS,
  type RunProfile,
} from "../form_model";

const { Text } = Typography;

interface BasicSectionProps {
  formData: unknown;
  onChange: (data: unknown) => void;
}

function RunProfileCard({ formData, onChange }: BasicSectionProps) {
  const { t } = useTranslation();
  // App.useApp()'s modal (project convention — every other confirm in this
  // codebase goes through it): the static ``Modal.confirm`` renders outside
  // the App context (no theme, invisible under test) and is unused here.
  const { modal } = App.useApp();
  const current = inferRunProfile(formData);

  const pick = (profile: RunProfile): void => {
    const diff = countProfileDiff(formData, profile);
    if (diff === 0) {
      onChange(applyRunProfile(formData, profile));
      return;
    }
    modal.confirm({
      title: t("run_profile.confirm_title", {
        name: t(`run_profile.${profile}`),
      }),
      content: t("run_profile.confirm_body", { count: diff }),
      onOk: () => onChange(applyRunProfile(formData, profile)),
    });
  };

  return (
    <div data-testid="run-profile-card" style={{ marginBottom: 24 }}>
      <Text strong style={{ display: "block", marginBottom: 4 }}>
        {t("run_profile.title")}
      </Text>
      <Text type="secondary" style={{ display: "block", marginBottom: 12 }}>
        {t("run_profile.hint")}
      </Text>
      <Radio.Group
        value={current === "custom" ? undefined : current}
        onChange={(e) => pick(e.target.value as RunProfile)}
        style={{ display: "flex", flexDirection: "column", gap: 8 }}
      >
        {RUN_PROFILE_IDS.map((profile) => (
          <Radio
            key={profile}
            value={profile}
            data-testid={`run-profile-${profile}`}
          >
            <div>
              <div>{t(`run_profile.${profile}`)}</div>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {t(`run_profile.${profile}_desc`)}
              </Text>
            </div>
          </Radio>
        ))}
      </Radio.Group>
      {current === "custom" && (
        <div style={{ marginTop: 8, display: "flex", alignItems: "center", gap: 8 }}>
          <Tag
            color="blue"
            bordered={false}
            data-testid="run-profile-custom-tag"
            style={{ margin: 0 }}
          >
            {t("run_profile.custom")}
          </Tag>
          {(() => {
            // Nearest preset + how far off — answers "why is nothing
            // selected?" after a manual tweak knocks the config off a
            // preset (UX feedback 2026-07-30 #12: the deselection is by
            // design, the mystery wasn't).
            const [nearest, diff] = RUN_PROFILE_IDS.map(
              (p) => [p, countProfileDiff(formData, p)] as const,
            ).sort((a, b) => a[1] - b[1])[0];
            return (
              <Text
                type="secondary"
                style={{ fontSize: 12 }}
                data-testid="run-profile-nearest"
              >
                {t("run_profile.nearest_hint", {
                  name: t(`run_profile.${nearest}`),
                  count: diff,
                })}
              </Text>
            );
          })()}
        </div>
      )}
    </div>
  );
}

export function BasicSection({ formData, onChange }: BasicSectionProps) {
  return (
    <div data-testid="basic-section" style={{ maxWidth: 760 }}>
      <RunProfileCard formData={formData} onChange={onChange} />
      <FormView formData={formData} onChange={onChange} section="basic" />
    </div>
  );
}
