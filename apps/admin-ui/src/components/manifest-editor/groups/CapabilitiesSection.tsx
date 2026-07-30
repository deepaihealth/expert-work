/**
 * CapabilitiesSection — "能力" (Capabilities) group, config-page redesign v2
 * Task 5. The group used to stack all five FormView sections
 * (tools/mcp/knowledge/skills/subagents) in one long scrolling pane via
 * ``ManifestEditor``'s plain ``FormView sections={[...]}`` fallback path —
 * this curated pane replaces that with an antd ``Tabs`` (``size="small"``),
 * one sub-tab per section, mirroring ``MemorySection``/``ContextGatesSection``/
 * ``SecuritySection``'s own sub-tab split.
 *
 * Each tab renders the SAME ``FormView`` for that lone ``section``,
 * unchanged — no field logic moved here, this is purely a layout change
 * (stacked → tabbed). ``mcpSource`` is forwarded to the "mcp" tab's
 * ``FormView`` exactly as ``ManifestEditor`` forwarded it to the old stacked
 * pane, so a template's ``mcpSource="catalog"`` still reaches the MCP tool
 * picker.
 */
import { useState } from "react";
import { Tabs } from "antd";
import { useTranslation } from "react-i18next";

import { FormView } from "../FormView";
import type { McpPickerSource } from "../widgets/McpToolPicker";

interface CapabilitiesSectionProps {
  formData: unknown;
  onChange: (data: unknown) => void;
  /** Where the MCP tab sources servers — forwarded verbatim from
   *  ``ManifestEditor``'s own ``mcpSource`` prop. */
  mcpSource?: McpPickerSource;
  /** #2 — controlled sub-tab, lifted to ManifestEditor so the selection
   *  survives this pane unmounting on a group switch. Falls back to local
   *  state when rendered standalone (tests). */
  activeSubTab?: string;
  onSubTabChange?: (key: string) => void;
}

export function CapabilitiesSection({
  formData,
  onChange,
  mcpSource,
  activeSubTab,
  onSubTabChange,
}: CapabilitiesSectionProps) {
  const { t } = useTranslation();
  const [fallbackSubTab, setFallbackSubTab] = useState("tools");
  const subTab = activeSubTab ?? fallbackSubTab;
  const handleSubTabChange = (key: string): void => {
    setFallbackSubTab(key);
    onSubTabChange?.(key);
  };

  return (
    <div data-testid="capabilities-section" style={{ maxWidth: 760 }}>
      <Tabs
        size="small"
        activeKey={subTab}
        onChange={handleSubTabChange}
        items={[
          {
            key: "tools",
            label: t("manifest_editor.tab_tools"),
            children: (
              <FormView
                formData={formData}
                onChange={onChange}
                section="tools"
              />
            ),
          },
          {
            key: "mcp",
            label: t("manifest_editor.tab_mcp"),
            children: (
              <FormView
                formData={formData}
                onChange={onChange}
                section="mcp"
                mcpSource={mcpSource}
              />
            ),
          },
          {
            key: "knowledge",
            label: t("manifest_editor.tab_knowledge"),
            children: (
              <FormView
                formData={formData}
                onChange={onChange}
                section="knowledge"
              />
            ),
          },
          {
            key: "skills",
            label: t("manifest_editor.tab_skills"),
            children: (
              <FormView
                formData={formData}
                onChange={onChange}
                section="skills"
              />
            ),
          },
          {
            key: "subagents",
            label: t("manifest_editor.tab_subagents"),
            children: (
              <FormView
                formData={formData}
                onChange={onChange}
                section="subagents"
              />
            ),
          },
        ]}
      />
    </div>
  );
}
