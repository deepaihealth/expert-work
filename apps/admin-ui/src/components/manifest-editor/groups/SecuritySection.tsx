/**
 * SecuritySection — "安全与防护" (Security) group, config-page redesign v2
 * Task 4. The group used to embed the "defenses"/"governance" FormView
 * sections wholesale (prompt-injection / output-screen / output-judge /
 * output-dlp / action-screen switches, plus the approval gate /
 * dynamic-workers / advanced knobs) and bolt two curated panels on top
 * (sandbox egress, tool-use enforcement) below them in Collapse panels. Both
 * FormView sections are now gone entirely — every field lives here, laid out
 * as three ``Tabs`` (v2) sub-tabs instead of one long embedded FormView plus
 * two collapsed panels:
 *
 * - **defenses** — the whole former "defenses" FormView section, migrated
 *   verbatim (same manifest paths, same on/off value mappings, same
 *   conditional-Alert trigger conditions), each switch/select now wrapped in
 *   a ``FieldRow`` (v2): a short new ``brief`` plus the ORIGINAL
 *   ``agent_form.defenses_*_help`` copy as ``help``. The on-error sub-selects
 *   (output_judge/action_screen) stay nested plain controls under their
 *   parent's conditional block — the brief's own field count ("prompt_
 *   injection/output_screen/output_judge(+on_error)/output_dlp/action_
 *   screen(+on_error)") bundles them with their parent rather than giving
 *   them their own row.
 * - **approval** — the former "governance" section's approval gate
 *   (``GATEABLE_TOOLS``, migrated from FormView) plus ``approval_timeout_s``
 *   (previously buried in governance's collapsed "Advanced" panel, now a
 *   first-class ``FieldRow``).
 * - **network** — the two curated ``PolicyFieldList`` panels this component
 *   already had (sandbox egress / tool-use enforcement) — pulled out of
 *   their Collapse wrappers and laid flat in the tab. ``dynamic_workers``
 *   lived here too until 2026-08-26 — it is an orchestration capability,
 *   not a network policy, and users couldn't find it; it now renders in
 *   the capabilities group's 子 Agent tab (``SubagentPicker``).
 *
 * Trajectory recording (``policies.trajectory_recording``) — previously the
 * other half of governance's "Advanced" panel — is NOT migrated here: Task 4
 * removed its curated toggle as (mistaken) dead-opt-in; Task 8b restored it,
 * but in ``ObservabilitySection`` (it's a run-archiving concern, not a
 * security/governance one), not here.
 */
import { useState } from "react";
import { Alert, Checkbox, InputNumber, Select, Switch, Tabs, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { FieldRow } from "../FieldRow";
import { PolicyFieldList, type FieldDef } from "./field_defs";
import {
  patchSecurity,
  readActionScreen,
  readActionScreenOnError,
  readApprovalTimeout,
  readClarificationTimeout,
  readApprovalTools,
  readExtends,
  readOutputDlp,
  readOutputJudge,
  readOutputJudgeOnError,
  readOutputScreen,
  readPromptInjection,
  readSecurity,
  setActionScreen,
  setActionScreenOnError,
  setApprovalTimeout,
  setClarificationTimeout,
  setApprovalTools,
  setOutputDlp,
  setOutputJudge,
  setOutputJudgeOnError,
  setOutputScreen,
  setPromptInjection,
  type SecurityFields,
} from "../form_model";

const { Text } = Typography;

interface SecuritySectionProps {
  formData: unknown;
  onChange: (data: unknown) => void;
  /** #2 — controlled sub-tab, lifted to ManifestEditor so the selection
   *  survives this pane unmounting on a group switch. Falls back to local
   *  state when rendered standalone (tests). */
  activeSubTab?: string;
  onSubTabChange?: (key: string) => void;
}

const LABEL: React.CSSProperties = { display: "block", marginBottom: 4 };

// Tools the approval gate can require a human verdict for — the base
// capabilities most worth gating (always-on code exec / file writes) plus the
// opt-in network tools. The gate can't remove a capability, only pause it.
// Migrated verbatim from FormView.tsx (Task 4).
const GATEABLE_TOOLS = [
  "exec_python",
  "bash",
  "write_file",
  "edit_file",
  "web_search",
  "http",
  "mcp",
] as const;

// ① spec.sandbox.network — the sandbox's outbound network policy. i18nKey
// prefixes mirror SecurityFields' egress/allowlist/denylist value keys.
const NETWORK_DEFS: readonly FieldDef[] = [
  {
    fieldId: "sandbox.network.egress",
    i18nKey: "security_gates.egress",
    valueKey: "egress",
    kind: "select",
    effectiveDefault: "proxy",
    options: ["proxy", "direct", "none"],
    optionLabelKey: "security_gates.egress_opt",
  },
  {
    fieldId: "sandbox.network.allowlist",
    i18nKey: "security_gates.allowlist",
    valueKey: "allowlist",
    kind: "tags",
    effectiveDefault: [],
  },
  {
    fieldId: "sandbox.network.denylist",
    i18nKey: "security_gates.denylist",
    valueKey: "denylist",
    kind: "tags",
    effectiveDefault: [],
  },
];

// ② policies.tool_use_enforcement — whether the tool-use-enforcement block
// is appended to the system prompt (and for which model families).
const ENFORCE_DEFS: readonly FieldDef[] = [
  {
    fieldId: "policies.tool_use_enforcement",
    i18nKey: "security_gates.enforce",
    valueKey: "toolUseEnforcement",
    kind: "select",
    effectiveDefault: "auto",
    options: ["auto", "on", "off"],
    optionLabelKey: "security_gates.enforce_opt",
  },
];

export function SecuritySection({
  formData,
  onChange,
  activeSubTab,
  onSubTabChange,
}: SecuritySectionProps) {
  const { t } = useTranslation();
  const [fallbackSubTab, setFallbackSubTab] = useState("defenses");
  const subTab = activeSubTab ?? fallbackSubTab;
  const handleSubTabChange = (key: string): void => {
    setFallbackSubTab(key);
    onSubTabChange?.(key);
  };
  const security = readSecurity(formData);
  // NETWORK_DEFS mixes a select (string) and two tags (string[]) fields;
  // ENFORCE_DEFS is select-only (string) — cast ``readSecurity``'s output to
  // each panel's narrower value domain, mirroring how ContextGatesSection
  // casts its (number|boolean)-only reader output for PolicyFieldList.
  const networkValues = security as Record<
    string,
    string | readonly string[] | undefined
  >;
  const enforceValues = security as Record<string, string | undefined>;

  const handleSecurityPatch = (patch: Partial<SecurityFields>): void => {
    onChange(patchSecurity(formData, patch));
  };

  const extendsTemplate = readExtends(formData);
  const promptInjection = readPromptInjection(formData);
  const outputScreen = readOutputScreen(formData);
  const outputJudge = readOutputJudge(formData);
  const outputJudgeOnError = readOutputJudgeOnError(formData);
  const outputDlp = readOutputDlp(formData);
  const actionScreen = readActionScreen(formData);
  const actionScreenOnError = readActionScreenOnError(formData);

  const approvalTools = readApprovalTools(formData);
  const approvalTimeout = readApprovalTimeout(formData);
  const clarificationTimeout = readClarificationTimeout(formData);

  const toggleApproval = (name: string, on: boolean): void => {
    const next = on
      ? [...approvalTools, name]
      : approvalTools.filter((tool) => tool !== name);
    onChange(setApprovalTools(formData, next));
  };

  return (
    <div data-testid="security-section" style={{ maxWidth: 760 }}>
      <Tabs
        size="small"
        activeKey={subTab}
        onChange={handleSubTabChange}
        items={[
          {
            key: "defenses",
            label: t("security_gates.tab_defenses"),
            children: (
              <div data-testid="security-tab-defenses">
                {extendsTemplate !== undefined && (
                  <Alert
                    type="info"
                    showIcon
                    data-testid="af-defenses-extends-note"
                    style={{ marginBottom: 16 }}
                    message={t("agent_form.defenses_extends_note")}
                  />
                )}

                {/* 输入防护 */}
                <Text
                  type="secondary"
                  style={{ display: "block", margin: "0 0 8px" }}
                >
                  {t("agent_form.defenses_group_input")}
                </Text>
                <FieldRow
                  fieldId="defenses.prompt_injection"
                  label={t("agent_form.defenses_prompt_injection")}
                  brief={t("agent_form.defenses_prompt_injection_brief")}
                  help={t("agent_form.defenses_prompt_injection_help")}
                  isDefault={promptInjection === "spotlight"}
                  onReset={() =>
                    onChange(setPromptInjection(formData, "spotlight"))
                  }
                  resetHint="spotlight"
                >
                  <Switch
                    checked={promptInjection === "spotlight"}
                    aria-label={t("agent_form.defenses_prompt_injection")}
                    onChange={(on) =>
                      onChange(
                        setPromptInjection(formData, on ? "spotlight" : "off"),
                      )
                    }
                  />
                </FieldRow>
                {promptInjection === "off" && (
                  <Alert
                    type="warning"
                    showIcon
                    style={{ marginBottom: 12 }}
                    message={t("agent_form.defenses_prompt_injection_off_warn")}
                  />
                )}

                {/* 输出防护 */}
                <Text
                  type="secondary"
                  style={{ display: "block", margin: "16px 0 8px" }}
                >
                  {t("agent_form.defenses_group_output")}
                </Text>
                <FieldRow
                  fieldId="defenses.output_screen"
                  label={t("agent_form.defenses_output_screen")}
                  brief={t("agent_form.defenses_output_screen_brief")}
                  help={t("agent_form.defenses_output_screen_help")}
                  isDefault={outputScreen === "block"}
                  onReset={() => onChange(setOutputScreen(formData, "block"))}
                  resetHint="block"
                >
                  <Switch
                    checked={outputScreen === "block"}
                    aria-label={t("agent_form.defenses_output_screen")}
                    onChange={(on) =>
                      onChange(setOutputScreen(formData, on ? "block" : "off"))
                    }
                  />
                </FieldRow>
                {outputScreen === "off" && (
                  <Alert
                    type="warning"
                    showIcon
                    style={{ marginBottom: 12 }}
                    message={t("agent_form.defenses_output_screen_off_warn")}
                  />
                )}

                <FieldRow
                  fieldId="defenses.output_judge"
                  label={t("agent_form.defenses_output_judge")}
                  brief={t("agent_form.defenses_output_judge_brief")}
                  help={t("agent_form.defenses_output_judge_help")}
                  isDefault={outputJudge === "off"}
                  onReset={() => onChange(setOutputJudge(formData, "off"))}
                  resetHint="off"
                >
                  <Switch
                    checked={outputJudge === "block"}
                    aria-label={t("agent_form.defenses_output_judge")}
                    onChange={(on) =>
                      onChange(setOutputJudge(formData, on ? "block" : "off"))
                    }
                  />
                </FieldRow>
                {outputJudge === "block" && (
                  <div style={{ marginBottom: 12 }}>
                    <Alert
                      type="warning"
                      showIcon
                      style={{ marginBottom: 8 }}
                      message={t("agent_form.defenses_output_judge_on_warn")}
                    />
                    <label style={LABEL}>
                      {t("agent_form.defenses_output_judge_on_error")}
                    </label>
                    <Select
                      data-testid="af-defenses-output-judge-on-error"
                      style={{ width: 240 }}
                      value={outputJudgeOnError}
                      onChange={(v) =>
                        onChange(
                          setOutputJudgeOnError(
                            formData,
                            v as "open" | "closed",
                          ),
                        )
                      }
                      options={[
                        {
                          value: "open",
                          label: t("agent_form.defenses_on_error_open"),
                        },
                        {
                          value: "closed",
                          label: t("agent_form.defenses_on_error_closed"),
                        },
                      ]}
                    />
                  </div>
                )}

                <FieldRow
                  fieldId="defenses.output_dlp"
                  label={t("agent_form.defenses_output_dlp")}
                  brief={t("agent_form.defenses_output_dlp_brief")}
                  help={t("agent_form.defenses_output_dlp_help")}
                  isDefault={outputDlp === "off"}
                  onReset={() => onChange(setOutputDlp(formData, "off"))}
                  resetHint="off"
                >
                  <Switch
                    checked={outputDlp === "redact"}
                    aria-label={t("agent_form.defenses_output_dlp")}
                    onChange={(on) =>
                      onChange(setOutputDlp(formData, on ? "redact" : "off"))
                    }
                  />
                </FieldRow>
                {outputDlp === "redact" && (
                  <Alert
                    type="info"
                    showIcon
                    style={{ marginBottom: 12 }}
                    message={t("agent_form.defenses_output_dlp_on_note")}
                  />
                )}

                {/* 工具行为防护 */}
                <Text
                  type="secondary"
                  style={{ display: "block", margin: "16px 0 8px" }}
                >
                  {t("agent_form.defenses_group_action")}
                </Text>
                <FieldRow
                  fieldId="defenses.action_screen"
                  label={t("agent_form.defenses_action_screen")}
                  brief={t("agent_form.defenses_action_screen_brief")}
                  help={t("agent_form.defenses_action_screen_help")}
                  isDefault={actionScreen === "off"}
                  onReset={() => onChange(setActionScreen(formData, "off"))}
                  resetHint="off"
                >
                  <Select
                    data-testid="af-defenses-action-screen-select"
                    style={{ width: 240 }}
                    value={actionScreen}
                    aria-label={t("agent_form.defenses_action_screen")}
                    onChange={(v) =>
                      onChange(
                        setActionScreen(
                          formData,
                          v as "off" | "block" | "approval",
                        ),
                      )
                    }
                    options={[
                      {
                        value: "off",
                        label: t("agent_form.defenses_action_screen_off"),
                      },
                      {
                        value: "block",
                        label: t("agent_form.defenses_action_screen_block"),
                      },
                      {
                        value: "approval",
                        label: t(
                          "agent_form.defenses_action_screen_approval",
                        ),
                      },
                    ]}
                  />
                </FieldRow>
                {actionScreen !== "off" && (
                  <div style={{ marginBottom: 12 }}>
                    <Alert
                      type="info"
                      showIcon
                      style={{ marginBottom: 8 }}
                      message={t("agent_form.defenses_action_screen_on_note")}
                    />
                    <label style={LABEL}>
                      {t("agent_form.defenses_action_screen_on_error")}
                    </label>
                    <Select
                      data-testid="af-defenses-action-screen-on-error"
                      style={{ width: 240 }}
                      value={actionScreenOnError}
                      onChange={(v) =>
                        onChange(
                          setActionScreenOnError(
                            formData,
                            v as "open" | "closed",
                          ),
                        )
                      }
                      options={[
                        {
                          value: "open",
                          label: t("agent_form.defenses_on_error_open"),
                        },
                        {
                          value: "closed",
                          label: t("agent_form.defenses_on_error_closed"),
                        },
                      ]}
                    />
                  </div>
                )}
              </div>
            ),
          },
          {
            key: "approval",
            label: t("security_gates.tab_approval"),
            children: (
              <div data-testid="security-tab-approval">
                <Text
                  type="secondary"
                  style={{ display: "block", marginBottom: 12 }}
                >
                  {t("agent_form.approval_hint")}
                </Text>
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: 8,
                    marginBottom: 16,
                  }}
                >
                  {GATEABLE_TOOLS.map((name) => (
                    <Checkbox
                      key={name}
                      data-testid={`af-approval-${name}`}
                      checked={approvalTools.includes(name)}
                      onChange={(e) => toggleApproval(name, e.target.checked)}
                    >
                      {name}
                    </Checkbox>
                  ))}
                </div>
                <FieldRow
                  fieldId="policies.approval_timeout_s"
                  label={t("agent_form.approval_timeout")}
                  brief={t("agent_form.approval_timeout_brief")}
                  help={t("agent_form.approval_timeout_help")}
                  isDefault={approvalTimeout === 86400}
                  onReset={() => onChange(setApprovalTimeout(formData, 86400))}
                  resetHint="86400"
                >
                  <InputNumber
                    min={60}
                    max={604800}
                    value={approvalTimeout}
                    aria-label={t("agent_form.approval_timeout")}
                    onChange={(v) =>
                      onChange(setApprovalTimeout(formData, v ?? 86400))
                    }
                  />
                </FieldRow>
                <FieldRow
                  fieldId="policies.clarification_timeout_s"
                  label={t("agent_form.clarification_timeout")}
                  brief={t("agent_form.clarification_timeout_brief")}
                  help={t("agent_form.clarification_timeout_help")}
                  isDefault={clarificationTimeout === 3600}
                  onReset={() => onChange(setClarificationTimeout(formData, 3600))}
                  resetHint="3600"
                >
                  <InputNumber
                    min={60}
                    max={604800}
                    value={clarificationTimeout}
                    aria-label={t("agent_form.clarification_timeout")}
                    onChange={(v) =>
                      onChange(setClarificationTimeout(formData, v ?? 3600))
                    }
                  />
                </FieldRow>
              </div>
            ),
          },
          {
            key: "network",
            label: t("security_gates.tab_network"),
            children: (
              <div data-testid="security-tab-network">
                {/* dynamic_workers.enabled 原先在这里 —— 它是编排能力不是
                    网络策略,已挪进能力分区的「子 Agent」tab(SubagentPicker,
                    2026-08-26 用户反馈「界面上找不到」)。 */}
                <Text
                  type="secondary"
                  style={{ display: "block", margin: "16px 0 8px" }}
                >
                  {t("security_gates.panel_network")}
                </Text>
                <PolicyFieldList
                  defs={NETWORK_DEFS}
                  values={networkValues}
                  onPatch={handleSecurityPatch}
                />

                <Text
                  type="secondary"
                  style={{ display: "block", margin: "16px 0 8px" }}
                >
                  {t("security_gates.panel_enforce")}
                </Text>
                <PolicyFieldList
                  defs={ENFORCE_DEFS}
                  values={enforceValues}
                  onPatch={handleSecurityPatch}
                />
              </div>
            ),
          },
        ]}
      />
      <Text
        type="secondary"
        data-testid="security-gates-dict-note"
        style={{ display: "block", marginTop: 16 }}
      >
        {t("security_gates.dict_note")}
      </Text>
    </div>
  );
}
