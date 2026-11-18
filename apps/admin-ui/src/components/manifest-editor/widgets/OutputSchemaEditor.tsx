/**
 * OutputSchemaEditor — the "structured output" block of the agent form, over
 * ``spec.output_schema.json_schema``. B-315 replaced the flat field list
 * (config-page redesign v2 Task 7) with an expandable field tree: objects
 * nest fields, lists carry a fixed "each item" row, up to MAX_DEPTH levels;
 * each row has a "Limits" popover. Parsing / write-back live in
 * ``output_schema_tree.ts`` (see its header for the round-trip guarantee).
 *
 * Render states (``readOutputSchemaTree``):
 *  - not configured (``undefined``) — Switch off + one hint line.
 *  - root readonly (``json_schema`` missing, not an object schema, or the
 *    root itself uses an unsupported structure) — the Switch is HIDDEN and
 *    a read-only Alert points at the YAML view; nothing calls ``onChange``.
 *  - otherwise — Switch on + the tree. A node the form can't represent shows
 *    greyed inside the tree (only that node; see OutputSchemaRows).
 *
 * ``name``/``strict`` never surface as controls (``setOutputSchemaTree``
 * keeps them); ``strict`` (default true) only drives the optional-field
 * reminder.
 *
 * Commit model (unchanged from the flat editor): the full tree — including
 * fields whose name is currently blank/invalid/duplicate — lives in local
 * state; every edit writes ``pruneInvalidFields(tree)`` immediately, so one
 * bad name never blocks other edits. Resync: an effect re-reads the tree when
 * ``json_schema`` in ``formData`` differs from what this widget last wrote
 * (echoes of our own writes and sibling-field edits are skipped, so an
 * in-progress invalid-name field is not lost).
 */
import { useEffect, useRef, useState, type CSSProperties } from "react";
import { App, Alert, Button, Switch, Typography } from "antd";
import { useTranslation } from "react-i18next";

import {
  readOutputJsonSchema,
  readOutputSchemaStrict,
  readOutputSchemaTree,
  setOutputSchemaTree,
} from "../form_model";
import {
  newSchemaField,
  newSchemaNode,
  pruneInvalidFields,
  updateFieldsAt,
  type SchemaField,
  type SchemaNode,
} from "../output_schema_tree";
import {
  COL_NAME,
  COL_REQUIRED,
  COL_TYPE,
  FieldRows,
  type TreeContext,
} from "./OutputSchemaRows";

const { Text } = Typography;

const SECTION: CSSProperties = { marginBottom: 24 };
const HEAD: CSSProperties = { fontSize: 12, color: "var(--ew-text-tertiary, #888)", flex: "none" };

const snapshot = (m: unknown): string | undefined => JSON.stringify(readOutputJsonSchema(m));

interface OutputSchemaEditorProps {
  formData: unknown;
  onChange: (data: unknown) => void;
}

// Local tree + collapse state, and the commit / resync logic (module doc).
function useOutputSchemaTree({ formData, onChange }: OutputSchemaEditorProps) {
  const [tree, setTree] = useState<SchemaNode | undefined>(() => readOutputSchemaTree(formData));
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set());
  const lastWrittenRef = useRef<string | undefined>(snapshot(formData));

  useEffect(() => {
    const current = snapshot(formData);
    if (current === lastWrittenRef.current) return;
    lastWrittenRef.current = current;
    setTree(readOutputSchemaTree(formData));
  }, [formData]);

  const commit = (next: SchemaNode): void => {
    setTree(next);
    const written = setOutputSchemaTree(formData, pruneInvalidFields(next));
    lastWrittenRef.current = snapshot(written);
    onChange(written);
  };

  const toggleCollapsed = (rowKey: string): void =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(rowKey)) next.delete(rowKey);
      else next.add(rowKey);
      return next;
    });

  return { tree, commit, collapsed, toggleCollapsed };
}

function TreeTable({ ctx, fields }: { ctx: TreeContext; fields: SchemaField[] }) {
  const { t } = useTranslation();
  return (
    <>
      <div style={{ display: "flex", gap: 8, marginBottom: 4 }}>
        <span style={{ ...HEAD, width: 20 + COL_NAME }}>{t("agent_form.output_schema.col_name")}</span>
        <span style={{ ...HEAD, width: COL_TYPE }}>{t("agent_form.output_schema.col_type")}</span>
        <span style={{ ...HEAD, width: COL_REQUIRED, textAlign: "center" }}>
          {t("agent_form.output_schema.col_required")}
        </span>
        <span style={{ ...HEAD, flex: 1 }}>{t("agent_form.output_schema.col_desc")}</span>
      </div>
      <FieldRows parentPath={[]} fields={fields} ctx={ctx} />
      <Button
        type="dashed"
        size="small"
        data-testid="af-output-schema-add"
        onClick={() => ctx.commit(updateFieldsAt(ctx.root, [], (fs) => [...fs, newSchemaField()]))}
      >
        {t("agent_form.output_schema.add_field")}
      </Button>
    </>
  );
}

export function OutputSchemaEditor({ formData, onChange }: OutputSchemaEditorProps) {
  const { t } = useTranslation();
  const { modal } = App.useApp();
  const { tree, commit, collapsed, toggleCollapsed } = useOutputSchemaTree({ formData, onChange });

  const external = readOutputSchemaTree(formData);
  const on = external !== undefined;
  const root = tree?.kind === "editable" ? tree : undefined;
  const fields = root?.fields ?? [];

  const handleToggle = (checked: boolean): void => {
    if (checked) return commit(newSchemaNode("object"));
    const off = (): void => onChange(setOutputSchemaTree(formData, null));
    if (fields.length === 0) return off();
    modal.confirm({ title: t("agent_form.output_schema.off_confirm"), onOk: off });
  };

  const strict = readOutputSchemaStrict(formData);
  const ctx: TreeContext | undefined = root && { root, strict, commit, collapsed, toggleCollapsed };

  return (
    <section data-testid="af-output-schema" style={SECTION}>
      <h3 style={{ fontSize: 15, margin: "0 0 12px" }}>{t("agent_form.section_output_schema")}</h3>
      {external?.kind === "readonly" ? (
        <Alert
          type="info"
          showIcon
          data-testid="af-output-schema-readonly"
          message={t("agent_form.output_schema.complex_readonly")}
        />
      ) : (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
            <Switch
              checked={on}
              data-testid="af-output-schema-switch"
              aria-label={t("agent_form.output_schema.on_label")}
              onChange={handleToggle}
            />
            <Text>{t("agent_form.output_schema.on_label")}</Text>
          </div>
          {!on && <Text type="secondary">{t("agent_form.output_schema.hint_off")}</Text>}
          {on && ctx && <TreeTable ctx={ctx} fields={fields} />}
        </>
      )}
    </section>
  );
}
