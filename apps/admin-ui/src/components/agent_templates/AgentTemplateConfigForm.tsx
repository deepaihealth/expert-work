/**
 * Platform Agent template config form — Stream Agent-Templates (M1-6).
 *
 * The system_admin authoring surface: marketplace metadata (display_name /
 * category / icon / required_tier / status / enabled) via an antd Form + the
 * base manifest via the shared ``ManifestEditor`` (Monaco, YAML). Used by both
 * the create Modal (``editing === null``) and the detail page.
 *
 * Exposes an imperative ``submit()`` via ``forwardRef`` so the parent (Modal
 * OK / detail Save button) drives submission. On create → ``POST`` the parsed
 * manifest + metadata; on edit → ``PATCH`` metadata + ``PUT`` the manifest.
 */
import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useMemo,
  useState,
} from "react";
import { App, Form, Input, Select, Switch } from "antd";
import { useTranslation } from "react-i18next";

import { ManifestEditor } from "../manifest-editor/ManifestEditor";
import { FieldHelp } from "../FieldHelp";
import { dumpYaml, parseYaml } from "../manifest-editor/yaml";
import { ApiError } from "../../api/client";
import {
  TEMPLATE_CATEGORIES,
  createAgentTemplate,
  patchTemplateMeta,
  updateTemplateSpec,
  type AgentTemplate,
  type TemplateStatus,
  type TemplateTier,
} from "../../api/agent-templates";

const TIERS: TemplateTier[] = ["free", "pro", "enterprise"];
const STATUSES: TemplateStatus[] = ["draft", "published"];

const _STARTER_MANIFEST = `apiVersion: expert_work.io/v1
kind: Agent
metadata:
  name: my-template
  version: 1.0.0
  tenant: platform
spec:
  tenant_config: {}
  model: {}
  system_prompt:
    template: You are a helpful assistant.
  sandbox:
    resources: { cpu: "1.0", memory: 1Gi }
    network: { egress: proxy, allowlist: ["api.anthropic.com"] }
    filesystem: { readonly_root: true, writable: ["/workspace"] }
`;

export interface AgentTemplateConfigFormHandle {
  submit: () => Promise<void>;
}

interface MetaFields {
  display_name: string;
  description?: string;
  category: string;
  icon?: string;
  required_tier: TemplateTier;
  status: TemplateStatus;
  enabled: boolean;
}

interface Props {
  editing: AgentTemplate | null;
  onSaved: (saved: AgentTemplate) => void;
  onSubmittingChange?: (submitting: boolean) => void;
}

export const AgentTemplateConfigForm = forwardRef<
  AgentTemplateConfigFormHandle,
  Props
>(function AgentTemplateConfigForm(
  { editing, onSaved, onSubmittingChange },
  ref,
) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [form] = Form.useForm<MetaFields>();
  const isEditing = editing !== null;

  // Label + a "?" help affordance (meaning + example on hover), uniform with
  // the manifest FormView's FieldHelp.
  const lbl = (labelKey: string, helpKey: string, id: string) => (
    <>
      {t(labelKey)}
      <FieldHelp text={t(helpKey)} testId={id} />
    </>
  );

  const initialYaml = useMemo(
    () => (editing ? dumpYaml(editing.spec) : _STARTER_MANIFEST),
    [editing],
  );
  const [yaml, setYaml] = useState(initialYaml);
  // B-315 — see ManifestEditor ``onValidityChange``; ``submit`` refuses meanwhile.
  const [formHasErrors, setFormHasErrors] = useState(false);

  useEffect(() => {
    setYaml(initialYaml);
    if (editing) {
      form.setFieldsValue({
        display_name: editing.display_name,
        description: editing.description,
        category: editing.category || "general",
        icon: editing.icon ?? undefined,
        required_tier: editing.required_tier,
        status: editing.status,
        enabled: editing.enabled,
      });
    } else {
      form.setFieldsValue({
        category: "general",
        required_tier: "free",
        status: "draft",
        enabled: true,
      });
    }
  }, [editing, form, initialYaml]);

  const submit = useMemo(
    () => async (): Promise<void> => {
      const meta = await form.validateFields();
      if (formHasErrors) {
        message.error(t("manifest_editor.form_has_errors"));
        return;
      }
      onSubmittingChange?.(true);
      try {
        let spec: Record<string, unknown>;
        try {
          const parsed = parseYaml(yaml);
          if (
            parsed === null ||
            typeof parsed !== "object" ||
            Array.isArray(parsed)
          ) {
            throw new Error("not an object");
          }
          spec = parsed as Record<string, unknown>;
        } catch {
          message.error(t("agent_templates.invalid_manifest"));
          return;
        }
        let saved: AgentTemplate;
        if (isEditing && editing) {
          await patchTemplateMeta(editing.name, editing.version, {
            display_name: meta.display_name,
            description: meta.description ?? "",
            category: meta.category,
            icon: meta.icon ?? null,
            required_tier: meta.required_tier,
            status: meta.status,
            enabled: meta.enabled,
          });
          saved = await updateTemplateSpec(editing.name, editing.version, spec);
        } else {
          saved = await createAgentTemplate({
            spec,
            display_name: meta.display_name,
            description: meta.description ?? "",
            category: meta.category,
            icon: meta.icon ?? null,
            required_tier: meta.required_tier,
            status: meta.status,
            enabled: meta.enabled,
          });
        }
        message.success(t("agent_templates.saved_ok"));
        onSaved(saved);
      } catch (err) {
        message.error(
          err instanceof ApiError
            ? `${err.code}: ${err.message}`
            : t("agent_templates.save_failed"),
        );
      } finally {
        onSubmittingChange?.(false);
      }
    },
    [editing, form, formHasErrors, isEditing, message, onSaved, onSubmittingChange, t, yaml],
  );

  useImperativeHandle(ref, () => ({ submit }), [submit]);

  // The template's marketplace metadata form rides as the FIRST tab in the
  // manifest editor's single flat tab row (peer to the manifest sections),
  // so there is one level of tabs instead of a metadata/manifest nesting.
  const metaForm = (
    <Form<MetaFields> form={form} layout="vertical" data-testid="atcf-meta">
      <Form.Item
        name="display_name"
        label={lbl(
          "agent_templates.field_display_name",
          "agent_templates.field_display_name_help",
          "display-name",
        )}
        rules={[
          {
            required: true,
            message: t("agent_templates.display_name_required"),
          },
        ]}
      >
        <Input
          aria-label={t("agent_templates.field_display_name")}
          data-testid="atcf-display-name"
        />
      </Form.Item>
      <Form.Item
        name="description"
        label={lbl(
          "agent_templates.field_description",
          "agent_templates.field_description_help",
          "description",
        )}
      >
        <Input.TextArea
          rows={2}
          aria-label={t("agent_templates.field_description")}
          data-testid="atcf-description"
        />
      </Form.Item>
      <Form.Item
        name="category"
        label={lbl(
          "agent_templates.field_category",
          "agent_templates.field_category_help",
          "category",
        )}
        rules={[{ required: true }]}
      >
        <Select
          aria-label={t("agent_templates.field_category")}
          data-testid="atcf-category"
          options={TEMPLATE_CATEGORIES.map((c) => ({
            value: c.value,
            label: t(c.labelKey),
          }))}
        />
      </Form.Item>
      <Form.Item
        name="icon"
        label={lbl(
          "agent_templates.field_icon",
          "agent_templates.field_icon_help",
          "icon",
        )}
      >
        <Input
          placeholder="🤖"
          aria-label={t("agent_templates.field_icon")}
          data-testid="atcf-icon"
        />
      </Form.Item>
      <Form.Item
        name="required_tier"
        label={lbl(
          "agent_templates.field_tier",
          "agent_templates.field_tier_help",
          "tier",
        )}
        rules={[{ required: true }]}
      >
        <Select
          aria-label={t("agent_templates.field_tier")}
          data-testid="atcf-tier"
          options={TIERS.map((v) => ({
            value: v,
            label: t(`agent_templates.tier_${v}`),
          }))}
        />
      </Form.Item>
      <Form.Item
        name="status"
        label={lbl(
          "agent_templates.field_status",
          "agent_templates.field_status_help",
          "status",
        )}
        rules={[{ required: true }]}
      >
        <Select
          aria-label={t("agent_templates.field_status")}
          data-testid="atcf-status"
          options={STATUSES.map((v) => ({
            value: v,
            label: t(`agent_templates.status_${v}`),
          }))}
        />
      </Form.Item>
      <Form.Item
        name="enabled"
        label={lbl(
          "agent_templates.field_enabled",
          "agent_templates.field_enabled_help",
          "enabled",
        )}
        valuePropName="checked"
      >
        <Switch
          aria-label={t("agent_templates.field_enabled")}
          data-testid="atcf-enabled"
        />
      </Form.Item>
    </Form>
  );

  return (
    <div data-testid="atcf-form">
      <ManifestEditor
        mode={isEditing ? "edit" : "create"}
        initialYaml={initialYaml}
        onChange={setYaml}
        onValidityChange={setFormHasErrors}
        mcpSource="catalog"
        leadingTabs={[
          {
            value: "meta",
            label: t("agent_templates.tab_basic"),
            content: metaForm,
            // Fold the manifest's "basic" (agent name/description) into this tab
            // so there's one "basic info" tab, not a metadata + basic split.
            mergeSection: "basic",
          },
        ]}
      />
    </div>
  );
});
