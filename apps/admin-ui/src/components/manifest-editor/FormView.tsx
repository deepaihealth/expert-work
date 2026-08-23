/**
 * Curated agent form — a hand-built view over the canonical fields of an agent
 * manifest. The fields are grouped into named sections (basic / model / prompt /
 * tools / capabilities); the parent ``ManifestEditor`` renders one section per
 * tab, so the form reads as a short focused panel instead of one long scroll.
 * Every control emits the FULL merged manifest via the form_model writers, so
 * non-curated fields a user hand-added in raw YAML are preserved across a
 * Form round-trip. The model catalog is loaded once and handed to
 * ModelSelect.
 *
 * "memory" is no longer one of these sections — config-page redesign v2 Task
 * 2 moved every memory field into ``MemorySection`` (its own three-sub-tab
 * curated pane), so ``ManifestEditor``'s "memory" ``CONFIG_GROUPS`` entry now
 * has a statically-empty ``sections: []`` like "budget"/"context"/etc.
 * "defenses"/"governance" are gone the same way — config-page redesign v2
 * Task 4 moved every field they held into ``SecuritySection`` (its own
 * three-sub-tab curated pane: defenses/approval/network), so "security"'s
 * ``CONFIG_GROUPS`` entry is now statically-empty too.
 */
import { useEffect, useState, type ReactNode } from "react";
import { Button, Checkbox, Input, Switch, Typography } from "antd";
import { useTranslation } from "react-i18next";

import type { ModelCatalog } from "../../api/model_catalog";
import { FieldHelp } from "../FieldHelp";
import { KnowledgePicker } from "./KnowledgePicker";
import { SkillPicker } from "./SkillPicker";
import { SubagentPicker } from "./SubagentPicker";
import { PromptVariablesEditor } from "./PromptVariablesEditor";
import { loadModelCatalog } from "./catalog";
import { ModelSelect } from "./widgets/ModelSelect";
import {
  hasBuiltinTool,
  readDescription,
  readDisplayName,
  readFallback,
  readInjectCurrentDate,
  readMainSupportsVision,
  readModel,
  readName,
  readPromptJinja,
  readPromptVariables,
  readSystemPrompt,
  readTools,
  readVisionModel,
  readVisionOn,
  setBuiltinTool,
  setDescription,
  setDisplayName,
  setFallback,
  setInjectCurrentDate,
  setMcp,
  setModel,
  setName,
  setSystemPrompt,
  setTool,
  setVisionModel,
} from "./form_model";
import { FallbackChainEditor } from "./widgets/FallbackChainEditor";
import { McpToolPicker, type McpPickerSource } from "./widgets/McpToolPicker";
import { OutputSchemaEditor } from "./widgets/OutputSchemaEditor";
import { PromptTemplateEditor } from "./widgets/PromptTemplateEditor";

const { Text } = Typography;

/** The named field groups; each maps to one tab in ``ManifestEditor``. */
export type FormSection =
  | "basic"
  | "model"
  | "prompt"
  | "tools"
  | "mcp"
  | "knowledge"
  | "skills"
  | "subagents";

interface FormViewProps {
  formData: unknown;
  onChange: (data: unknown) => void;
  /** Which field group to render. Defaults to ``basic`` for stand-alone use.
   *  Ignored when ``sections`` is supplied. */
  section?: FormSection;
  /** Stack-render several sections in one pane — the group-nav + detail-pane
   *  layout's "capabilities" group (tools/mcp/knowledge/skills/subagents) is
   *  the main user. Each section is wrapped in a ``data-section-id`` anchor
   *  div with a small sub-section title (the section's own ``manifest_editor
   *  .tab_<section>`` label). Takes precedence over ``section`` when given. */
  sections?: readonly FormSection[];
  /** Where the MCP tab sources servers — ``catalog`` for a platform template,
   *  ``available`` (default) for a tenant agent. */
  mcpSource?: McpPickerSource;
  /** Drop the section heading — used when the section is folded into another
   *  tab (e.g. "basic" merged into a template's "basic info") so there's no
   *  redundant sub-heading. */
  bare?: boolean;
}

const SECTION: React.CSSProperties = { marginBottom: 24 };
const FIELD: React.CSSProperties = { marginBottom: 16 };
const LABEL: React.CSSProperties = { display: "block", marginBottom: 4 };

// Builtin tools that get a form toggle. exec_python/bash are seeded default-ON
// (removable); the rest are opt-in default-OFF (ask_for_approval since
// 2026-08-23 — chat agents should just ask in conversation; the tool is for
// unattended runs / audited sign-offs). The essential file/artifact/
// read/remember builtins are seeded but intentionally have NO toggle (edit YAML
// to change) — they are NOT in this list.
const BUILTIN_TOGGLES = [
  { name: "exec_python", key: "tool_exec_python" },
  { name: "bash", key: "tool_bash" },
  { name: "ask_for_approval", key: "tool_ask_for_approval" },
  { name: "manage_task", key: "tool_manage_task" },
  { name: "author_skill", key: "tool_author_skill" },
  { name: "refine_skill", key: "tool_refine_skill" },
  { name: "fork_skill", key: "tool_fork_skill" },
  { name: "propose_skill_to_tenant", key: "tool_propose_skill" },
  { name: "note_behavior_patch", key: "tool_note_behavior_patch" },
  { name: "clarify_tool_usage", key: "tool_clarify_tool_usage" },
] as const;

function Heading({ children }: { children: React.ReactNode }) {
  return <h3 style={{ fontSize: 15, margin: "0 0 12px" }}>{children}</h3>;
}

export function FormView({
  formData,
  onChange,
  section = "basic",
  sections,
  mcpSource = "available",
  bare = false,
}: FormViewProps) {
  const { t } = useTranslation();
  const [catalog, setCatalog] = useState<ModelCatalog | undefined>(undefined);

  useEffect(() => {
    let alive = true;
    loadModelCatalog().then(
      (c) => {
        if (alive) setCatalog(c);
      },
      () => {
        /* catalog optional — ModelSelect degrades to a disabled/loading select */
      },
    );
    return () => {
      alive = false;
    };
  }, []);

  const tools = readTools(formData);
  // Stacked panes (the ``sections`` prop) already render a ``data-section-id``
  // sub-heading per section (see the final return below). Only suppress a
  // section's own ``<Heading>`` where it's a pure duplicate of that
  // sub-heading (basic/model's primary heading/tools/mcp — each says exactly
  // what the tab already said, e.g. "MCP" under "MCP"). Leave every OTHER
  // heading alone even when stacked: several FormSection entries bundle
  // multiple distinctly-titled sub-parts under one tab (model's
  // fallback/reflection-evaluator/vision; prompt's output-schema) or add a
  // real qualifier the tab label lacks (memory's "Long-term memory") —
  // hiding those would remove the only label distinguishing that content,
  // not fix a duplicate.
  // The singular ``section=`` path (``stacked`` false) is unaffected, and so
  // is ``bare`` (a separate, independent switch for the "basic" section).
  const stacked = sections !== undefined;

  const sectionsRecord: Record<FormSection, ReactNode> = {
    basic: (
      <section data-testid="af-basic" style={SECTION}>
        {!bare && !stacked && <Heading>{t("agent_form.section_basic")}</Heading>}
        <div style={FIELD} data-testid="af-name">
          <label style={LABEL}>
            {t("agent_form.field_name")}{" "}
            <span style={{ color: "#ff4d4f" }}>*</span>
            <FieldHelp
              text={t("agent_form.field_name_help")}
              testId="af-name"
            />
          </label>
          <Input
            value={readName(formData)}
            placeholder={t("agent_form.field_name_placeholder")}
            aria-label={t("agent_form.field_name")}
            onChange={(e) => onChange(setName(formData, e.target.value))}
          />
        </div>
        {/* 显示名 —— 给终端用户看的名字(阶段 3, 3.1)。``name`` 是机器
            标识(对外的 agent_code),直接显示在第三方界面上很难看。
            与描述同款:折进别的 tab(``bare``)时不渲染。 */}
        {!bare && (
          <div style={FIELD} data-testid="af-display-name">
            <label style={LABEL}>
              {t("agent_form.field_display_name")}
              <FieldHelp
                text={t("agent_form.field_display_name_help")}
                testId="af-display-name"
              />
            </label>
            <Input
              value={readDisplayName(formData)}
              aria-label={t("agent_form.field_display_name")}
              onChange={(e) =>
                onChange(setDisplayName(formData, e.target.value))
              }
            />
          </div>
        )}
        {/* When folded into another tab (``bare``) the description is dropped —
            that tab carries its own description field (no duplicate). */}
        {!bare && (
          <div style={FIELD} data-testid="af-description">
            <label style={LABEL}>
              {t("agent_form.field_description")}
              <FieldHelp
                text={t("agent_form.field_description_help")}
                testId="af-description"
              />
            </label>
            <Input
              value={readDescription(formData)}
              aria-label={t("agent_form.field_description")}
              onChange={(e) =>
                onChange(setDescription(formData, e.target.value))
              }
            />
          </div>
        )}
        <Text
          type="secondary"
          data-testid="af-basic-yaml-note"
          style={{ display: "block" }}
        >
          {t("agent_form.basic_yaml_note")}
        </Text>
      </section>
    ),

    model: (
      <>
        <section data-testid="af-model" style={SECTION}>
          {!stacked && (
            <Heading>
              {t("agent_form.section_model")}
              <FieldHelp
                text={t("agent_form.section_model_help")}
                testId="af-model"
              />
            </Heading>
          )}
          <ModelSelect
            value={readModel(formData)}
            catalog={catalog}
            onChange={(mdl) => onChange(setModel(formData, mdl))}
          />
          {/* E.11 model fallback chain — a lightweight sub-area of the main
            model (not a sibling section); only meaningful once a primary
            model is picked. A slow / failing model falls over to the next
            instead of killing the run (the failure mode this feature exists
            to prevent). */}
          {!!readModel(formData).provider && !!readModel(formData).name && (
            <div data-testid="af-fallback" style={{ marginTop: 16 }}>
              <label style={LABEL}>
                {t("agent_form.section_fallback")}
                <FieldHelp
                  text={t("agent_form.section_fallback_help")}
                  testId="af-fallback"
                />
              </label>
              <Text
                type="secondary"
                style={{ display: "block", marginBottom: 8, fontSize: 12 }}
              >
                {t("agent_form.fallback_hint")}
              </Text>
              <FallbackChainEditor
                value={readFallback(formData)}
                catalog={catalog}
                onChange={(chain) => onChange(setFallback(formData, chain))}
              />
            </div>
          )}
        </section>

        {/* The reflection-evaluator model picker lives in
          ModelRoutingSection's reflection panel (next to the switch that
          governs it), not here. */}

        {/* Stream J.6 Path B — shown whenever the main model can't see images
          itself (including before one is picked); a separate VL model handles
          image questions via the ask_image tool. Hidden only when the main
          model is itself vision-capable (no fallback needed). */}
        {!readMainSupportsVision(formData) && (
          <section data-testid="af-vision" style={SECTION}>
            <Heading>
              {t("agent_form.section_vision")}
              <FieldHelp
                text={t("agent_form.section_vision_help")}
                testId="af-vision"
              />
            </Heading>
            <Text
              type="secondary"
              style={{ display: "block", marginBottom: 12 }}
            >
              {t("agent_form.vision_hint")}
            </Text>
            <ModelSelect
              visionOnly
              value={readVisionModel(formData) ?? {}}
              catalog={catalog}
              onChange={(mdl) => onChange(setVisionModel(formData, mdl))}
            />
            {readVisionOn(formData) && (
              <Button
                type="link"
                size="small"
                data-testid="af-vision-clear"
                style={{ paddingLeft: 0 }}
                onClick={() => onChange(setVisionModel(formData, null))}
              >
                {t("agent_form.vision_clear")}
              </Button>
            )}
          </section>
        )}
      </>
    ),

    prompt: (
      <>
        <section data-testid="af-prompt" style={SECTION}>
          <Heading>
            {t("agent_form.section_prompt")}
            <FieldHelp
              text={t("agent_form.section_prompt_help")}
              testId="af-prompt"
            />
          </Heading>
          <div data-testid="af-prompt-input">
            {readPromptJinja(formData) ? (
              <PromptTemplateEditor
                value={readSystemPrompt(formData)}
                variables={readPromptVariables(formData)}
                onChange={(v) => onChange(setSystemPrompt(formData, v))}
              />
            ) : (
              <Input.TextArea
                rows={6}
                value={readSystemPrompt(formData)}
                placeholder={t("agent_form.field_prompt_placeholder")}
                aria-label={t("agent_form.section_prompt")}
                onChange={(e) =>
                  onChange(setSystemPrompt(formData, e.target.value))
                }
              />
            )}
          </div>
        </section>
        <PromptVariablesEditor formData={formData} onChange={onChange} />
        {/* Stream RT-1 (RT-ADR-4) — structured final reply. config-page
            redesign v2 Task 7 replaced the former state-only hint block with
            a flat field-list editor (falls back to a read-only notice for
            hand-authored non-flat schemas — see OutputSchemaEditor's own doc
            comment). */}
        <OutputSchemaEditor formData={formData} onChange={onChange} />
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            marginBottom: 8,
          }}
        >
          <Switch
            checked={readInjectCurrentDate(formData) ?? true}
            data-testid="af-inject-current-date"
            aria-label={t("agent_form.inject_date_label")}
            onChange={(on) => onChange(setInjectCurrentDate(formData, on))}
          />
          <Text>{t("agent_form.inject_date_label")}</Text>
        </div>
        <Text type="secondary" style={{ display: "block", marginBottom: 12 }}>
          {t("agent_form.inject_date_hint")}
        </Text>
        <Text
          type="secondary"
          data-testid="af-dynamic-context-note"
          style={{ display: "block" }}
        >
          {t("agent_form.dynamic_context_note")}
        </Text>
      </>
    ),

    tools: (
      <section data-testid="af-tools" style={SECTION}>
        {!stacked && (
          <Heading>
            {t("agent_form.section_tools")}
            <FieldHelp
              text={t("agent_form.section_tools_help")}
              testId="af-tools"
            />
          </Heading>
        )}
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <span>
            <Checkbox
              data-testid="af-tool-web_search"
              checked={tools.webSearch}
              onChange={(e) =>
                onChange(setTool(formData, "webSearch", e.target.checked))
              }
            >
              {t("agent_form.tool_web_search")}
            </Checkbox>
            <FieldHelp
              text={t("agent_form.tool_web_search_help")}
              testId="af-tool-web_search"
            />
          </span>
          <span>
            <Checkbox
              data-testid="af-tool-http"
              checked={tools.http}
              onChange={(e) =>
                onChange(setTool(formData, "http", e.target.checked))
              }
            >
              {t("agent_form.tool_http")}
            </Checkbox>
            <FieldHelp
              text={t("agent_form.tool_http_help")}
              testId="af-tool-http"
            />
          </span>
          {BUILTIN_TOGGLES.map((tool) => (
            <span key={tool.name}>
              <Checkbox
                data-testid={`af-tool-${tool.name}`}
                checked={hasBuiltinTool(formData, tool.name)}
                onChange={(e) =>
                  onChange(setBuiltinTool(formData, tool.name, e.target.checked))
                }
              >
                {t(`agent_form.${tool.key}`)}
              </Checkbox>
              <FieldHelp
                text={t(`agent_form.${tool.key}_help`)}
                testId={`af-tool-${tool.name}`}
              />
            </span>
          ))}
        </div>
        <Text
          type="secondary"
          data-testid="af-tools-config-note"
          style={{ display: "block", marginTop: 12 }}
        >
          {t("agent_form.tools_config_note")}
        </Text>
      </section>
    ),

    mcp: (
      <section data-testid="af-mcp" style={SECTION}>
        {!stacked && (
          <Heading>
            {t("agent_form.section_mcp")}
            <FieldHelp text={t("agent_form.section_mcp_help")} testId="af-mcp" />
          </Heading>
        )}
        <McpToolPicker
          source={mcpSource}
          servers={tools.mcpServers}
          allowTools={tools.mcpAllowTools}
          onChange={(nextServers, nextAllow) =>
            onChange(setMcp(formData, nextServers, nextAllow))
          }
        />
      </section>
    ),

    knowledge: <KnowledgePicker formData={formData} onChange={onChange} />,
    skills: <SkillPicker formData={formData} onChange={onChange} />,
    subagents: <SubagentPicker formData={formData} onChange={onChange} />,
  };

  return (
    <div data-testid="manifest-form-view" style={{ maxWidth: 760 }}>
      {sections
        ? sections.map((s) => (
            <div key={s} data-section-id={s}>
              <Text
                strong
                type="secondary"
                style={{ display: "block", fontSize: 13, margin: "0 0 8px" }}
              >
                {t(`manifest_editor.tab_${s}`)}
              </Text>
              {sectionsRecord[s]}
            </div>
          ))
        : sectionsRecord[section]}
    </div>
  );
}
