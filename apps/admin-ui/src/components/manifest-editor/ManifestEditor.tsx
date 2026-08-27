/**
 * Visual manifest editor — Stream S PR C (Mini-ADRs S-1/S-2/S-6), migrated to
 * the group-nav + detail-pane layout (agent-config-page redesign PR1).
 *
 * Left: a ``GroupNav`` tree over ``CONFIG_GROUPS`` (+ an optional caller
 * leading node). Right: a detail pane that stacks the active group's
 * ``FormView`` sections. A top-right toggle swaps the pane to a raw YAML
 * escape hatch over the same in-memory ``manifestObject``: toggling on
 * serialises (Form→YAML); toggling off parses+validates (YAML→Form), and an
 * invalid switch is blocked with an inline error — same semantics as the
 * flat-tab row this replaces. ``onChange`` always carries the latest manifest
 * as a YAML string so the parent submits exactly what's shown.
 */
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Alert, Button, Spin } from "antd";
import validator from "@rjsf/validator-ajv8";
import { useTranslation } from "react-i18next";

import type { JsonSchema } from "../../api/manifest_schema";
import { loadAgentSchema } from "./schema";
import { dumpYaml, parseYaml } from "./yaml";
import { normalizeForSubmit } from "./form_model";
import { FormView, type FormSection } from "./FormView";
import { YamlView } from "./YamlView";
import { GroupNav } from "./GroupNav";
import { SettingsSearch } from "./SettingsSearch";
import { CONFIG_GROUPS } from "./groups";
import { BasicSection } from "./groups/BasicSection";
import { RunBudgetSection } from "./groups/RunBudgetSection";
import { ContextGatesSection } from "./groups/ContextGatesSection";
import { CapabilitiesSection } from "./groups/CapabilitiesSection";
import { MemorySection } from "./groups/MemorySection";
import { ModelRoutingSection } from "./groups/ModelRoutingSection";
import { SecuritySection } from "./groups/SecuritySection";
import { SandboxSection } from "./groups/SandboxSection";
import { ObservabilitySection } from "./groups/ObservabilitySection";
import type { AgentRef } from "./widgets/DelegationPolicyButton";
import type { McpPickerSource } from "./widgets/McpToolPicker";

/** Curated group panes — a hand-written component that replaces FormView's
 * registered-sections pathway for that group entirely, checked BEFORE the
 * plain ``FormView`` stacked-sections fallback below — so it wins even for
 * "basic"/"model"/"capabilities", whose ``CONFIG_GROUPS`` entries still list
 * real sections — ``BasicSection``/``ModelRoutingSection``/
 * ``CapabilitiesSection`` embed those themselves (config-page redesign v2
 * Task 6 adds ``RunProfileCard`` above the plain "basic" FormView section;
 * capabilities embeds five ``Tabs`` sub-tabs instead of one stacked pane).
 * "budget"/"context"/"memory"/"security"/"sandbox"/"observability" instead
 * have a statically-empty entry (``sections: []``) and render ONLY their
 * curated pane — "memory" and "security" used to embed real FormView
 * sections too ("memory" / "defenses"+"governance"), but config-page
 * redesign v2 Tasks 2 and 4 moved every field they held into
 * ``MemorySection``/``SecuritySection`` themselves, so neither needs one
 * anymore. Every ``CONFIG_GROUPS`` entry now has either real sections or a
 * curated pane here, so there's no longer a group that can fall through to a
 * generic "pending" hint (that branch — and its ``cfg-pane-pending`` testid
 * — has been removed). ``BasicSection`` never renders on the leading-tab
 * ``mergeSection="basic"`` path (``AgentTemplateConfigForm``) — that path
 * folds the plain FormView section into the leading tab directly, bypassing
 * this record entirely (see ``lt.mergeSection`` below). */
interface CuratedPaneProps {
  formData: unknown;
  onChange: (data: unknown) => void;
  /** Only consumed by ``CapabilitiesSection`` (its "mcp" sub-tab) — every
   *  other curated pane ignores it. */
  mcpSource?: McpPickerSource;
  /** #2 — sub-tab persistence: the four tabbed panes (capabilities/context/
   *  memory/security) report their active sub-tab up so the selection
   *  survives the pane unmounting on a group switch; single-pane sections
   *  ignore both. */
  activeSubTab?: string;
  onSubTabChange?: (key: string) => void;
}
const CURATED_GROUP_PANES: Record<
  string,
  (props: CuratedPaneProps) => ReactNode
> = {
  basic: BasicSection,
  budget: RunBudgetSection,
  context: ContextGatesSection,
  capabilities: CapabilitiesSection,
  memory: MemorySection,
  model: ModelRoutingSection,
  security: SecuritySection,
  sandbox: SandboxSection,
  observability: ObservabilitySection,
};

/** A caller-supplied node rendered ABOVE the registered groups in the tree —
 * e.g. an Agent template's marketplace-metadata form. Its content is kept
 * mounted (hidden when inactive) so any embedded antd Form keeps its state
 * across group switches. Switching to/from a leading tab never touches the
 * manifest, so no (de)serialisation happens. ``GroupNav`` renders a single
 * leading node, so only ``leadingTabs[0]`` gets a tree entry — every current
 * caller passes at most one. */
export interface LeadingTab {
  value: string;
  label: string;
  content: ReactNode;
  /** Optionally fold one manifest section into this leading tab — that
   * section renders below ``content`` and is dropped from its mapped
   * ``CONFIG_GROUPS`` group so it never renders twice. Used to merge a
   * template's "basic info" with the manifest's "basic" section. */
  mergeSection?: FormSection;
}

interface ManifestEditorProps {
  mode: "create" | "edit";
  initialYaml: string;
  onChange: (yaml: string) => void;
  leadingTabs?: ReadonlyArray<LeadingTab>;
  /** Forwarded to the MCP section — ``catalog`` for a platform template. */
  mcpSource?: McpPickerSource;
  /** #2 — optionally lift the active-group selection to the caller (e.g.
   *  ManifestTab persists it in the URL so it survives remounts). Undefined
   *  ⇒ the editor keeps its own internal selection. */
  activeGroup?: string;
  onActiveGroupChange?: (id: string) => void;
  /** 委派增强层 3 — 已保存 Agent 的身份(仅编辑页宿主 ManifestTab 传)。
   *  转发给 FormView,提示词区据此渲染「生成委派策略」按钮。 */
  agentRef?: AgentRef;
}

/** BUG-5 —— 编辑器主体 min-height 扣除的宿主 chrome 估算(agent 详情页
 * 页头+tabs+Card padding 一档)。三个宿主(详情页 / 模板表单 /
 * CreateAgentModal)chrome 不同,取最深的一档 —— Modal 宿主(top:100+
 * header+footer)按 768px 视口实算仍不截断;真截断时再开 fillViewport 开关。 */
const VIEWPORT_CHROME_PX = 320;

function safeSeed(initialYaml: string): unknown {
  try {
    const parsed = parseYaml(initialYaml);
    // The Form view (RJSF) expects an object. A scalar/array/empty seed (e.g.
    // a stray "42") would render a broken form, so fall back to {} — the raw
    // value is still recoverable via the YAML view.
    return parsed !== null &&
      typeof parsed === "object" &&
      !Array.isArray(parsed)
      ? parsed
      : {};
  } catch {
    return {};
  }
}

export function ManifestEditor({
  mode,
  initialYaml,
  onChange,
  leadingTabs = [],
  mcpSource,
  activeGroup,
  onActiveGroupChange,
  agentRef,
}: ManifestEditorProps) {
  const { t } = useTranslation();
  const seed = useMemo(() => safeSeed(initialYaml), [initialYaml]);
  const primaryLeading = leadingTabs[0];

  const [schema, setSchema] = useState<JsonSchema | null>(null);
  const [schemaError, setSchemaError] = useState(false);
  const [internalGroup, setInternalGroup] = useState<string>(
    primaryLeading?.value ?? CONFIG_GROUPS[0].id,
  );
  // #2 — caller-controlled group (when provided) wins over the internal
  // selection; both are written on every switch so an uncontrolled caller
  // keeps today's behaviour untouched.
  const group = activeGroup ?? internalGroup;
  // #2 — active sub-tab per tabbed curated pane (capabilities/context/
  // memory/security), keyed by group id and held HERE so the selection
  // survives the pane unmounting on a group switch.
  const [subTabs, setSubTabs] = useState<Record<string, string>>({});
  const [manifestObject, setManifestObject] = useState<unknown>(seed);
  const [yamlText, setYamlText] = useState<string>(initialYaml);
  const [yamlActive, setYamlActive] = useState(false);
  const [switchError, setSwitchError] = useState<string | null>(null);

  function setGroup(id: string): void {
    setInternalGroup(id);
    onActiveGroupChange?.(id);
  }

  // #2 — controlled resync: ManifestTab no longer key-remounts this editor
  // when the server snapshot changes; the refreshed snapshot arrives as a
  // NEW ``initialYaml`` instead. Adopt it wholesale — callers only change
  // ``initialYaml`` when the incoming snapshot is authoritative (the
  // post-save refetch), so overwriting the working copy IS the resync. On
  // mount both setters are identity no-ops (state was seeded from the same
  // values).
  useEffect(() => {
    setManifestObject(seed);
    setYamlText(initialYaml);
  }, [seed, initialYaml]);

  useEffect(() => {
    let alive = true;
    loadAgentSchema().then(
      (s) => alive && setSchema(s),
      () => alive && setSchemaError(true),
    );
    return () => {
      alive = false;
    };
  }, []);

  function handleFormChange(data: unknown): void {
    // The curated Form merges edits into the full manifest and preserves
    // non-curated fields: keys a user hand-added in raw YAML survive a Form
    // round-trip (the form_model writers patch only the curated paths). The
    // backend ManifestLoader re-validates on submit regardless.
    //
    // Keep the raw ``data`` as the form's working state (so an added-but-unfilled
    // fallback row still shows its picker), but serialize a normalized copy so
    // the submitted manifest never carries an incomplete / duplicate fallback
    // entry the backend would reject.
    setManifestObject(data);
    const y = dumpYaml(normalizeForSubmit(data));
    setYamlText(y);
    onChange(y);
  }

  function handleYamlChange(text: string): void {
    setYamlText(text);
    onChange(text);
  }

  // Guarded YAML→Form transition — parses + validates ``yamlText`` before
  // adopting it; an invalid switch is refused (inline error, stays on YAML)
  // — moved verbatim from the old ``switchTo``. Shared by the YAML toggle
  // button AND by clicking a group node while YAML mode is active (below),
  // so both paths run the exact same guard and neither can desync the tree
  // highlight from the pane. Returns whether the switch succeeded.
  function tryExitYaml(): boolean {
    let parsed: unknown;
    try {
      parsed = parseYaml(yamlText);
    } catch {
      setSwitchError(t("manifest_editor.invalid_yaml_hint"));
      return false;
    }
    if (
      schema &&
      validator.validateFormData(parsed, schema).errors.length > 0
    ) {
      setSwitchError(t("manifest_editor.invalid_yaml_hint"));
      return false;
    }
    setManifestObject(parsed);
    setSwitchError(null);
    setYamlActive(false);
    return true;
  }

  // Top-right YAML toggle — replaces the old flat-tab row's "yaml" tab.
  // Entering YAML serialises the current curated manifest (normalized so an
  // incomplete fallback row doesn't leak into the YAML view / submit).
  function toggleYaml(): void {
    if (!yamlActive) {
      const y = dumpYaml(normalizeForSubmit(manifestObject));
      setYamlText(y);
      onChange(y);
      setSwitchError(null);
      setYamlActive(true);
      return;
    }
    tryExitYaml();
  }

  // Group-nav click — mirrors the old UI where clicking a form tab from the
  // yaml tab ran the same guarded switch as the toggle: while YAML mode is
  // active, attempt the YAML→Form transition first. On success, exit YAML
  // mode AND move to the clicked group; on failure, stay in YAML mode with
  // the error shown and DON'T move the tree highlight (setGroup is skipped).
  function handleGroupSelect(id: string): void {
    if (yamlActive) {
      if (tryExitYaml()) {
        setGroup(id);
      }
      return;
    }
    setGroup(id);
  }

  // Without leading tabs the editor has nothing to show until the schema
  // resolves, so it gates the whole component (unchanged behaviour). With
  // leading tabs (e.g. a template's metadata form) the tree + that form stay
  // usable while the schema loads; only the manifest pane waits.
  if (leadingTabs.length === 0) {
    if (schemaError) {
      return (
        <Alert
          type="error"
          showIcon
          message={t("manifest_editor.schema_load_failed")}
          data-testid="manifest-schema-error"
        />
      );
    }
    if (schema === null) {
      return (
        <div
          data-testid="manifest-schema-loading"
          style={{ padding: 24, textAlign: "center" }}
        >
          <Spin />{" "}
          <span style={{ marginLeft: 8 }}>
            {t("manifest_editor.loading_schema")}
          </span>
        </div>
      );
    }
  }

  const isLeadingActive = leadingTabs.some((lt) => lt.value === group);
  // Manifest sections folded into a leading tab — dropped from their mapped
  // group's stacked render so they never render twice.
  const mergedSections = new Set<string>(
    leadingTabs.flatMap((lt) => (lt.mergeSection ? [lt.mergeSection] : [])),
  );
  // A group whose EVERY (statically registered) section got folded into a
  // leading tab renders nothing on its own — e.g. "basic" once its only
  // section is merged. Its node must be unreachable, or clicking it would
  // show FormView with an empty sections array (a blank pane). Groups that
  // are statically empty to begin with ("budget"/"context"/"sandbox"/
  // "observability", ``sections: []``) are untouched — they're all
  // special-cased below via ``CURATED_GROUP_PANES`` to render a hand-written
  // component instead (Task 6 / Task 3 / Task 2 pilots / Task 2 PR7).
  const hiddenGroups = CONFIG_GROUPS.filter(
    (g) =>
      g.sections.length > 0 &&
      g.sections.every((s) => mergedSections.has(s)),
  ).map((g) => g.id);
  const activeConfigGroup = CONFIG_GROUPS.find((g) => g.id === group);
  const CuratedPane = activeConfigGroup
    ? CURATED_GROUP_PANES[activeConfigGroup.id]
    : undefined;

  const pane = schemaError ? (
    <Alert
      type="error"
      showIcon
      message={t("manifest_editor.schema_load_failed")}
      data-testid="manifest-schema-error"
    />
  ) : schema === null ? (
    <div
      data-testid="manifest-schema-loading"
      style={{ padding: 24, textAlign: "center" }}
    >
      <Spin />{" "}
      <span style={{ marginLeft: 8 }}>
        {t("manifest_editor.loading_schema")}
      </span>
    </div>
  ) : yamlActive ? (
    <YamlView value={yamlText} onChange={handleYamlChange} />
  ) : CuratedPane ? (
    <CuratedPane
      formData={manifestObject}
      onChange={handleFormChange}
      mcpSource={mcpSource}
      activeSubTab={subTabs[group]}
      onSubTabChange={(key) => setSubTabs((m) => ({ ...m, [group]: key }))}
    />
  ) : activeConfigGroup ? (
    <FormView
      formData={manifestObject}
      onChange={handleFormChange}
      sections={activeConfigGroup.sections.filter(
        (s) => !mergedSections.has(s),
      )}
      mcpSource={mcpSource}
      agentRef={agentRef}
    />
  ) : null;

  // The leading pane wins over the group/YAML pane only when it's active AND
  // the YAML toggle is off — toggling YAML always shows YAML, whatever group
  // was selected before.
  const showLeadingPane = !yamlActive && isLeadingActive;

  return (
    <div data-testid={`manifest-editor-${mode}`}>
      {/* BUG-5:内容按需收高时卡片停在半屏,底部整片留白 —— 编辑器主体
          拉到接近视口底,少内容分组也占满版心。 */}
      <div
        style={{
          display: "flex",
          gap: 24,
          alignItems: "flex-start",
          minHeight: `calc(100vh - ${VIEWPORT_CHROME_PX}px)`,
        }}
      >
        <div data-testid="cfg-nav" style={{ flex: "0 0 200px", width: 200 }}>
          <GroupNav
            active={group}
            onSelect={handleGroupSelect}
            leading={
              primaryLeading
                ? { value: primaryLeading.value, label: primaryLeading.label }
                : undefined
            }
            hiddenGroups={hiddenGroups}
          />
        </div>

        <div data-testid="cfg-pane" style={{ flex: 1, minWidth: 0 }}>
          <div
            style={{
              display: "flex",
              justifyContent: "flex-end",
              alignItems: "center",
              gap: 12,
              marginBottom: 12,
            }}
          >
            <SettingsSearch onPick={handleGroupSelect} exclude={hiddenGroups} />
            <Button
              type={yamlActive ? "primary" : "default"}
              size="small"
              data-testid="cfg-yaml-toggle"
              aria-pressed={yamlActive}
              onClick={toggleYaml}
            >
              {t("manifest_editor.tab_yaml")}
            </Button>
          </div>

          {switchError !== null && (
            <Alert
              type="warning"
              showIcon
              message={t("manifest_editor.invalid_yaml_title")}
              description={switchError}
              style={{ marginBottom: 12 }}
              data-testid="manifest-switch-error"
            />
          )}

          {/* Leading tabs stay mounted (hidden when inactive) so an embedded
              antd Form keeps its state across group switches. */}
          {leadingTabs.map((lt) => (
            <div
              key={lt.value}
              data-testid={`manifest-leading-${lt.value}`}
              style={{
                display: !yamlActive && group === lt.value ? "block" : "none",
              }}
            >
              {/* Folded manifest section first (e.g. agent name/description on
                  top), then the caller's own content (e.g. template
                  marketplace fields). */}
              {lt.mergeSection && (
                <FormView
                  formData={manifestObject}
                  onChange={handleFormChange}
                  section={lt.mergeSection}
                  mcpSource={mcpSource}
                  bare
                />
              )}
              {lt.content}
            </div>
          ))}

          {!showLeadingPane && pane}
        </div>
      </div>
    </div>
  );
}
