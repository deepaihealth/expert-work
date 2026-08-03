/**
 * Knowledge-base (RAG) picker — Tier 2 capability, its own form tab. The agent
 * can retrieve from multiple tenant knowledge bases (the ref list is an array);
 * each option shows the base's chunking config so the author can tell bases
 * apart. Emits the FULL merged manifest via the form_model writers.
 */
import { useEffect, useState, type CSSProperties, type ReactNode } from "react";
import { Select, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { listBases, type KnowledgeBase } from "../../api/knowledge";
import { concreteTenantScope, useTenantScope } from "../../tenant/TenantScopeContext";
import { FieldHelp } from "../FieldHelp";
import { readKnowledgeRefs, setKnowledgeRefs } from "./form_model";

const { Text } = Typography;

const SECTION: CSSProperties = { marginBottom: 24 };

function Heading({ children }: { children: ReactNode }) {
  return <h3 style={{ fontSize: 15, margin: "0 0 12px" }}>{children}</h3>;
}

interface KnowledgePickerProps {
  formData: unknown;
  onChange: (data: unknown) => void;
}

export function KnowledgePicker({ formData, onChange }: KnowledgePickerProps) {
  const { t } = useTranslation();
  const [bases, setBases] = useState<KnowledgeBase[]>([]);
  // Cross-tenant W3 — list the switched-in tenant's bases for the picker.
  const { apiTenantScope } = useTenantScope();
  // W4: the list endpoint aggregates every tenant under ``"*"``, but a ref
  // binds a base BY NAME inside the agent's own tenant — offering foreign
  // bases would let the author pick one the runtime can never resolve.
  // Collapse the aggregate to the caller's tenant (same as McpToolPicker).
  const pickerScope = concreteTenantScope(apiTenantScope);

  useEffect(() => {
    let alive = true;
    listBases(pickerScope).then(
      (b) => alive && setBases(b ?? []),
      () => {},
    );
    return () => {
      alive = false;
    };
  }, [pickerScope]);

  const knowledgeRefs = readKnowledgeRefs(formData);

  return (
    <section data-testid="af-knowledge" style={SECTION}>
      <Heading>
        {t("agent_form.section_knowledge")}
        <FieldHelp
          text={t("agent_form.section_knowledge_help")}
          testId="af-knowledge"
        />
      </Heading>
      <Text type="secondary" style={{ display: "block", marginBottom: 8 }}>
        {t("agent_form.knowledge_hint")}
      </Text>
      <div data-testid="af-knowledge-select">
        <Select
          mode="tags"
          style={{ width: "100%" }}
          value={knowledgeRefs}
          options={bases.map((b) => ({
            value: b.name,
            label: `${b.name} · ${t("agent_form.knowledge_chunk_label", {
              max: b.chunk_max_tokens,
              overlap: b.chunk_overlap_tokens,
            })}`,
          }))}
          aria-label={t("agent_form.section_knowledge")}
          placeholder={t("agent_form.knowledge_placeholder")}
          onChange={(v: string[]) => onChange(setKnowledgeRefs(formData, v))}
        />
      </div>
    </section>
  );
}
