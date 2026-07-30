/**
 * SandboxSection — "沙箱与资源" (Sandbox & Resources) group, Task 2 of the
 * agent-config-page redesign PR4. Unlike ``RunBudgetSection``/``SecuritySection``
 * this group has exactly one field that is actually consumed at run time
 * (``spec.sandbox.filesystem.persistent_workspace`` — the plan-projection
 * switch); everything else a manifest can author under ``spec.sandbox``
 * (runtime/image/image_build/resources/readonly_root/writable/mounts, plus
 * the top-level ``code`` block) is declarative-only: it passes schema
 * validation but the sandbox supervisor never reads it — real resource
 * limits, the image, and the container runtime are all decided by the
 * platform deployment. So this pane, unlike its siblings, isn't "the rest of
 * the fields are TODO" — it's "the rest of the fields are inherently
 * YAML-only, and this note block says so explicitly" rather than silently
 * hiding them.
 *
 * Rendering the one live field is delegated to ``PolicyFieldList`` (Task 1's
 * FieldDef config-array pattern); this component only wires the
 * ``readSandboxFs``/``patchSandboxFs`` pair (form_model.ts) to it. No
 * ``Collapse`` — content is short enough to lay out flat (mirrors
 * ``RunBudgetSection``, the other single-panel curated pane).
 */
import { Descriptions, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { PolicyFieldList, type FieldDef } from "./field_defs";
import { patchSandboxFs, readSandboxFs } from "../form_model";

const { Text } = Typography;

// Platform-decided runtime facts, rendered as a compact definition table
// (they used to be one three-line prose wall — UX feedback 2026-07-30 #6).
// Label/value pairs live in i18n under sandbox_group.pn_*.
const PLATFORM_FACTS = ["image", "resources", "timeout", "paths", "runtime"] as const;

interface SandboxSectionProps {
  formData: unknown;
  onChange: (data: unknown) => void;
}

// ① spec.sandbox.filesystem.persistent_workspace — the only sandbox knob the
// runtime actually reads. A switch, so PolicyFieldList shows no default
// badge (existing switch semantics — see field_defs.tsx).
const SANDBOX_DEFS: readonly FieldDef[] = [
  {
    fieldId: "sandbox.filesystem.persistent_workspace",
    i18nKey: "sandbox_group.pw",
    valueKey: "persistentWorkspace",
    kind: "switch",
    effectiveDefault: false,
  },
];

export function SandboxSection({ formData, onChange }: SandboxSectionProps) {
  const { t } = useTranslation();
  const fs = readSandboxFs(formData);

  const handlePatch = (patch: Record<string, boolean | undefined>): void => {
    onChange(patchSandboxFs(formData, patch));
  };

  return (
    <div data-testid="sandbox-section" style={{ maxWidth: 760 }}>
      <PolicyFieldList
        defs={SANDBOX_DEFS}
        values={fs as Record<string, boolean | undefined>}
        onPatch={handlePatch}
      />
      <div data-testid="sandbox-platform-note" style={{ marginTop: 24 }}>
        <Text strong style={{ display: "block", marginBottom: 4 }}>
          {t("sandbox_group.platform_note_title")}
        </Text>
        <Text type="secondary" style={{ display: "block", marginBottom: 8 }}>
          {t("sandbox_group.platform_note_intro")}
        </Text>
        <Descriptions
          size="small"
          bordered
          column={1}
          style={{ maxWidth: 640 }}
          items={PLATFORM_FACTS.map((k) => ({
            key: k,
            label: t(`sandbox_group.pn_${k}`),
            children: t(`sandbox_group.pn_${k}_v`),
          }))}
        />
      </div>
      <Text
        type="secondary"
        data-testid="sandbox-declarative-note"
        style={{ display: "block", marginTop: 16 }}
      >
        {t("sandbox_group.declarative_note")}
      </Text>
    </div>
  );
}
