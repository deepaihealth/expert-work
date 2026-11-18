/**
 * OutputSchemaRows — B-315. The rows of the structured-output field tree,
 * rendered recursively: one row per field (name / type / required /
 * description / limits / add-sub-field / remove), an object row's fields
 * nested below it, and a list row's fixed "each item" row below it.
 *
 * Identity: React keys and the collapse state use each field's local
 * ``uid`` — row key ``JSON.stringify([uid])``; an item row appends
 * ``"items"`` to its parent's — so they follow the field when siblings are
 * added or removed. Test ids use the tree path
 * (``0``, ``0.items``, ``1.items.0`` …).
 *
 * A readonly node (a structure the form can't represent) renders greyed
 * with a "go to the YAML view" note and no node controls; its row still has
 * the required checkbox and remove button, since both edit the (editable)
 * parent. A name-locked field (original name breaks the name rule) shows
 * its name as text; everything else on it is editable.
 */
import type { CSSProperties, ReactNode } from "react";
import { Button, Checkbox, Input, Select, Tag, Tooltip, Typography } from "antd";
import { useTranslation } from "react-i18next";

import {
  MAX_DEPTH,
  SCHEMA_NODE_TYPES,
  changeNodeType,
  fieldIssue,
  isNameFixed,
  newSchemaField,
  updateFieldsAt,
  updateNodeAt,
  type EditableSchemaNode,
  type FieldIssue,
  type ReadonlySchemaNode,
  type SchemaField,
  type SchemaNode,
  type SchemaNodeType,
  type SchemaPath,
} from "../output_schema_tree";
import { OutputSchemaLimits, hasInvertedRange } from "./OutputSchemaLimits";

const { Text } = Typography;

export const INDENT = 20;
export const COL_NAME = 170;
export const COL_TYPE = 110;
export const COL_REQUIRED = 50;

const ROW: CSSProperties = { display: "flex", gap: 8, marginBottom: 6, alignItems: "flex-start" };
const TOGGLE: CSSProperties = { width: 20, flex: "none", padding: 0 };
const CELL_TEXT: CSSProperties = { width: COL_NAME, flex: "none", paddingTop: 4 };

export interface TreeContext {
  root: SchemaNode;
  strict: boolean;
  commit: (next: SchemaNode) => void;
  /** Collapsed rows, keyed by row key (JSON: ``[uid]`` / ``[uid, "items", …]``). */
  collapsed: ReadonlySet<string>;
  toggleCollapsed: (rowKey: string) => void;
}

interface RowProps {
  path: SchemaPath;
  rowKey: string;
  ctx: TreeContext;
}

const pathKey = (path: SchemaPath): string => path.join(".");

function hasChildren(node: SchemaNode): boolean {
  return node.kind === "editable" && (node.type === "object" || node.type === "array");
}

// Indent spacer + expand/collapse toggle (or a blank of the same width).
function Lead({ path, rowKey, ctx, node }: RowProps & { node: SchemaNode }) {
  const { t } = useTranslation();
  const closed = ctx.collapsed.has(rowKey);
  return (
    <>
      <span style={{ width: (path.length - 1) * INDENT, flex: "none" }} />
      {hasChildren(node) ? (
        <Button
          type="text"
          size="small"
          style={TOGGLE}
          data-testid={`af-output-schema-toggle-${pathKey(path)}`}
          aria-label={t(closed ? "agent_form.output_schema.expand" : "agent_form.output_schema.collapse")}
          onClick={() => ctx.toggleCollapsed(rowKey)}
        >
          {closed ? "+" : "-"}
        </Button>
      ) : (
        <span style={TOGGLE} />
      )}
    </>
  );
}

function TypeSelect({ path, ctx, node }: RowProps & { node: EditableSchemaNode }) {
  const { t } = useTranslation();
  // At the deepest level a new object/list would have nowhere to put children.
  const options = SCHEMA_NODE_TYPES.filter(
    (type) => path.length < MAX_DEPTH || node.type === type || (type !== "object" && type !== "array"),
  ).map((type) => ({ value: type, label: t(`agent_form.output_schema.type_${type}`) }));
  return (
    <Select
      style={{ width: COL_TYPE, flex: "none" }}
      value={node.type}
      options={options}
      data-testid={`af-output-schema-type-${pathKey(path)}`}
      aria-label={t("agent_form.output_schema.col_type")}
      onChange={(v: SchemaNodeType) => ctx.commit(updateNodeAt(ctx.root, path, (n) => changeNodeType(n, v)))}
    />
  );
}

// Description / limits / add-sub-field — everything right of "required".
function NodeExtras({ path, ctx, node }: RowProps & { node: EditableSchemaNode }) {
  const { t } = useTranslation();
  const key = pathKey(path);
  const patch = (fn: (n: EditableSchemaNode) => EditableSchemaNode): void =>
    ctx.commit(updateNodeAt(ctx.root, path, fn));
  const canAddChild = path.length < MAX_DEPTH;
  return (
    <>
      <Input
        style={{ flex: 1, minWidth: 80 }}
        value={node.description ?? ""}
        data-testid={`af-output-schema-desc-${key}`}
        aria-label={t("agent_form.output_schema.col_desc")}
        onChange={(e) => patch((n) => ({ ...n, description: e.target.value === "" ? undefined : e.target.value }))}
      />
      <OutputSchemaLimits node={node} pathKey={key} onChange={(next) => patch(() => next)} />
      {node.type === "object" && (
        <Tooltip title={canAddChild ? undefined : t("agent_form.output_schema.depth_limit")}>
          <Button
            size="small"
            disabled={!canAddChild}
            data-testid={`af-output-schema-add-child-${key}`}
            onClick={() => ctx.commit(updateFieldsAt(ctx.root, path, (fs) => [...fs, newSchemaField()]))}
          >
            {t("agent_form.output_schema.add_child")}
          </Button>
        </Tooltip>
      )}
    </>
  );
}

function ReadonlyTag() {
  const { t } = useTranslation();
  return (
    <span style={{ width: COL_TYPE, flex: "none", paddingTop: 4 }}>
      <Tag>{t("agent_form.output_schema.readonly_tag")}</Tag>
    </span>
  );
}

function ReadonlyNote({ path, node }: { path: SchemaPath; node: ReadonlySchemaNode }) {
  const { t } = useTranslation();
  const note = node.reason === "depth" ? "readonly_depth" : "readonly_node";
  return (
    <Text
      type="secondary"
      style={{ flex: 1, fontSize: 12, paddingTop: 6 }}
      data-testid={`af-output-schema-readonly-${pathKey(path)}`}
    >
      {t(`agent_form.output_schema.${note}`)}
    </Text>
  );
}

interface RowCellsProps extends RowProps {
  node: SchemaNode;
  nameCell: ReactNode;
  requiredCell: ReactNode;
  trailing?: ReactNode;
}

// The shared row skeleton: lead | name | type | required | extras | trailing.
function RowCells({ path, rowKey, ctx, node, nameCell, requiredCell, trailing }: RowCellsProps) {
  const editable = node.kind === "editable";
  return (
    <div data-testid={`af-output-schema-row-${pathKey(path)}`} style={{ ...ROW, opacity: editable ? 1 : 0.6 }}>
      <Lead path={path} rowKey={rowKey} ctx={ctx} node={node} />
      {nameCell}
      {node.kind === "editable" ? <TypeSelect path={path} rowKey={rowKey} ctx={ctx} node={node} /> : <ReadonlyTag />}
      <span style={{ width: COL_REQUIRED, flex: "none", paddingTop: 4, textAlign: "center" }}>{requiredCell}</span>
      {node.kind === "editable" ? (
        <NodeExtras path={path} rowKey={rowKey} ctx={ctx} node={node} />
      ) : (
        <ReadonlyNote path={path} node={node} />
      )}
      {trailing}
    </div>
  );
}

function Children({ path, rowKey, ctx, node }: RowProps & { node: SchemaNode }) {
  if (node.kind !== "editable" || ctx.collapsed.has(rowKey)) return null;
  if (node.type === "object") return <FieldRows parentPath={path} fields={node.fields ?? []} ctx={ctx} />;
  if (node.type === "array" && node.items) {
    const itemsKey = JSON.stringify([...(JSON.parse(rowKey) as string[]), "items"]);
    return <ItemsRow path={[...path, "items"]} rowKey={itemsKey} ctx={ctx} node={node.items} />;
  }
  return null;
}

function ItemsRow({ path, rowKey, ctx, node }: RowProps & { node: SchemaNode }) {
  const { t } = useTranslation();
  const nameCell = (
    <Text type="secondary" style={CELL_TEXT}>
      {t("agent_form.output_schema.items_label")}
    </Text>
  );
  return (
    <>
      <RowCells path={path} rowKey={rowKey} ctx={ctx} node={node} nameCell={nameCell} requiredCell={null} />
      <RowHints depth={path.length} testKey={pathKey(path)} node={node} strictOptional={false} />
      <Children path={path} rowKey={rowKey} ctx={ctx} node={node} />
    </>
  );
}

interface NameCellProps {
  field: SchemaField;
  issue: FieldIssue;
  testKey: string;
  onRename: (name: string) => void;
}

function IssueText({ issue }: { issue: Exclude<FieldIssue, null> }) {
  const { t } = useTranslation();
  return (
    <Text type="danger" style={{ fontSize: 12, display: "block" }}>
      {t(issue === "duplicate" ? "agent_form.output_schema.name_duplicate" : "agent_form.output_schema.name_invalid")}
    </Text>
  );
}

// A fixed name (readonly / locked) is text; it can still clash with a
// sibling's name, and then it is flagged like any other row.
function FieldNameCell({ field, issue, testKey, onRename }: NameCellProps) {
  const { t } = useTranslation();
  if (isNameFixed(field)) {
    return (
      <div style={{ ...CELL_TEXT }}>
        <Text
          type={issue ? "danger" : "secondary"}
          aria-invalid={issue !== null}
          data-testid={`af-output-schema-name-${testKey}`}
        >
          {field.name}
        </Text>
        {issue && <IssueText issue={issue} />}
      </div>
    );
  }
  return (
    <div style={{ width: COL_NAME, flex: "none" }}>
      <Input
        value={field.name}
        status={issue ? "error" : undefined}
        aria-invalid={issue !== null}
        data-testid={`af-output-schema-name-${testKey}`}
        aria-label={t("agent_form.output_schema.col_name")}
        onChange={(e) => onRename(e.target.value)}
      />
      {issue && <IssueText issue={issue} />}
    </div>
  );
}

interface RowHintsProps {
  depth: number;
  testKey: string;
  node: SchemaNode;
  /** Strict mode is on and this is an optional field. */
  strictOptional: boolean;
}

// Reminders under a row; none of them blocks saving.
function RowHints({ depth, testKey, node, strictOptional }: RowHintsProps) {
  const { t } = useTranslation();
  const inverted = node.kind === "editable" && hasInvertedRange(node.constraints);
  if (!strictOptional && !inverted) return null;
  return (
    <div style={{ display: "flex", gap: 12, margin: "-4px 0 6px", paddingLeft: (depth - 1) * INDENT + 20 }}>
      {strictOptional && (
        <Tooltip title={t("agent_form.output_schema.strict_optional_tip")}>
          <Text type="warning" style={{ fontSize: 12 }} data-testid={`af-output-schema-strict-warn-${testKey}`}>
            {t("agent_form.output_schema.strict_optional_warn")}
          </Text>
        </Tooltip>
      )}
      {inverted && (
        <Text type="warning" style={{ fontSize: 12 }} data-testid={`af-output-schema-row-range-warn-${testKey}`}>
          {t("agent_form.output_schema.limit_range_warn")}
        </Text>
      )}
    </div>
  );
}

interface FieldRowProps {
  parentPath: SchemaPath;
  fields: SchemaField[];
  index: number;
  ctx: TreeContext;
}

function FieldRow({ parentPath, fields, index, ctx }: FieldRowProps) {
  const { t } = useTranslation();
  const field = fields[index];
  const path = [...parentPath, index];
  const key = pathKey(path);
  const patchField = (patch: Partial<SchemaField>): void =>
    ctx.commit(updateFieldsAt(ctx.root, parentPath, (fs) => fs.map((f, i) => (i === index ? { ...f, ...patch } : f))));
  const rowKey = JSON.stringify([field.uid]);
  const remove = (): void =>
    ctx.commit(updateFieldsAt(ctx.root, parentPath, (fs) => fs.filter((_, i) => i !== index)));
  const requiredCell = (
    <Checkbox
      checked={field.required}
      data-testid={`af-output-schema-required-${key}`}
      aria-label={t("agent_form.output_schema.col_required")}
      onChange={(e) => patchField({ required: e.target.checked })}
    />
  );
  const removeButton = (
    <Button type="text" danger size="small" data-testid={`af-output-schema-remove-${key}`} onClick={remove}>
      {t("agent_form.output_schema.remove_field")}
    </Button>
  );
  const nameCell = (
    <FieldNameCell field={field} issue={fieldIssue(fields, index)} testKey={key} onRename={(name) => patchField({ name })} />
  );
  return (
    <>
      <RowCells
        path={path}
        rowKey={rowKey}
        ctx={ctx}
        node={field.node}
        nameCell={nameCell}
        requiredCell={requiredCell}
        trailing={removeButton}
      />
      <RowHints depth={path.length} testKey={key} node={field.node} strictOptional={ctx.strict && !field.required} />
      <Children path={path} rowKey={rowKey} ctx={ctx} node={field.node} />
    </>
  );
}

interface FieldRowsProps {
  parentPath: SchemaPath;
  fields: SchemaField[];
  ctx: TreeContext;
}

export function FieldRows({ parentPath, fields, ctx }: FieldRowsProps) {
  return (
    <>
      {fields.map((f, i) => (
        <FieldRow key={f.uid} parentPath={parentPath} fields={fields} index={i} ctx={ctx} />
      ))}
    </>
  );
}
