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
 * Commit model (B-315 name rule, see output_schema_tree.ts): the full tree
 * lives in local state. While ANY field name is blank, invalid or duplicated
 * (``hasNameIssues``), edits stay local only — ``formData`` keeps its last
 * valid state, every offending row is flagged, a banner says why, and the
 * editor reports the error up (``useReportFormErrors``) so the host blocks
 * saving. Once every name is fixed, the next edit writes the whole tree at
 * once. Resync: before paint (``useLayoutEffect``, so stale rows never
 * flash), the tree is re-read when ``json_schema`` in ``formData`` differs
 * from what this widget last wrote; our own echoes and sibling-field edits
 * are skipped, so local edits survive them. A re-read keeps collapsed
 * fields that are still at the same name path. An unmount (group switch,
 * YAML view) drops local edits that were never written back.
 */
import { useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { App, Alert, Button, Switch, Typography } from "antd";
import { useTranslation } from "react-i18next";

import {
  hasOutputSchema,
  readOutputJsonSchema,
  readOutputSchemaStrict,
  readOutputSchemaTree,
  setOutputSchemaTree,
} from "../form_model";
import { useReportFormErrors } from "../form_errors";
import {
  matchFieldUids,
  newSchemaField,
  hasNameIssues,
  newSchemaNode,
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

// Collapse keys are JSON arrays: ``[uid]`` or ``[uid, "items", …]``. Carry
// them over to the matching new uids; keys with no match are dropped.
function remapCollapsed(keys: ReadonlySet<string>, uids: Map<string, string>): ReadonlySet<string> {
  const out = new Set<string>();
  for (const key of keys) {
    const [uid, ...rest] = JSON.parse(key) as string[];
    const mapped = uids.get(uid);
    if (mapped !== undefined) out.add(JSON.stringify([mapped, ...rest]));
  }
  return out;
}

// Local tree + collapse state, and the commit / resync logic (module doc).
// The tree is parsed once at mount and again only when ``json_schema``
// really changed from outside — never per render.
function useOutputSchemaTree({ formData, onChange }: OutputSchemaEditorProps) {
  const [tree, setTree] = useState<SchemaNode | undefined>(() => readOutputSchemaTree(formData));
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set());
  const [mountSnapshot] = useState(() => snapshot(formData));
  const lastWrittenRef = useRef<string | undefined>(mountSnapshot);

  useLayoutEffect(() => {
    const current = snapshot(formData);
    if (current === lastWrittenRef.current) return;
    lastWrittenRef.current = current;
    const next = readOutputSchemaTree(formData);
    setCollapsed((prev) => (tree && next ? remapCollapsed(prev, matchFieldUids(tree, next)) : new Set()));
    setTree(next);
  }, [formData]);

  const commit = (next: SchemaNode): void => {
    setTree(next);
    if (hasNameIssues(next)) return; // paused: formData keeps its last valid state
    const written = setOutputSchemaTree(formData, next);
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

  const nameErrors = tree !== undefined && hasNameIssues(tree);
  useReportFormErrors("output_schema", nameErrors);
  return { tree, commit, collapsed, toggleCollapsed, nameErrors };
}

interface TreeTableProps {
  ctx: TreeContext;
  fields: SchemaField[];
  /** Some name is blank / invalid / duplicated: nothing is written back yet. */
  nameErrors: boolean;
}

function TreeTable({ ctx, fields, nameErrors }: TreeTableProps) {
  const { t } = useTranslation();
  return (
    <>
      {nameErrors && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 8 }}
          data-testid="af-output-schema-name-errors"
          message={t("agent_form.output_schema.name_errors_block")}
        />
      )}
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

function OnSwitch({ on, onToggle }: { on: boolean; onToggle: (checked: boolean) => void }) {
  const { t } = useTranslation();
  return (
    <>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
        <Switch
          checked={on}
          data-testid="af-output-schema-switch"
          aria-label={t("agent_form.output_schema.on_label")}
          onChange={onToggle}
        />
        <Text>{t("agent_form.output_schema.on_label")}</Text>
      </div>
      {!on && <Text type="secondary">{t("agent_form.output_schema.hint_off")}</Text>}
    </>
  );
}

export function OutputSchemaEditor({ formData, onChange }: OutputSchemaEditorProps) {
  const { t } = useTranslation();
  const { modal } = App.useApp();
  const { tree, commit, collapsed, toggleCollapsed, nameErrors } = useOutputSchemaTree({
    formData,
    onChange,
  });

  const on = hasOutputSchema(formData);
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
      {on && tree?.kind === "readonly" ? (
        <Alert
          type="info"
          showIcon
          data-testid="af-output-schema-readonly"
          message={t("agent_form.output_schema.complex_readonly")}
        />
      ) : (
        <>
          <OnSwitch on={on} onToggle={handleToggle} />
          {on && ctx && <TreeTable ctx={ctx} fields={fields} nameErrors={nameErrors} />}
        </>
      )}
    </section>
  );
}
