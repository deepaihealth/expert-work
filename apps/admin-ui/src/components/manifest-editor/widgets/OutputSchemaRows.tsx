/**
 * OutputSchemaRows — B-315. The rows of the structured-output field tree,
 * rendered recursively: one row per field (name / type / required /
 * description / limits / add-sub-field / remove), an object row's fields
 * nested below it, and a list row's fixed "each item" row below it.
 *
 * Rows are addressed by their tree path (``SchemaPath``); test ids use the
 * dotted form (``0``, ``0.items``, ``1.items.0`` …). A readonly node (a
 * structure the form can't represent) renders greyed with a "go to the YAML
 * view" note and no editable control; a readonly FIELD can still be removed
 * as a whole, since that is an edit of its (editable) parent.
 */
import type { CSSProperties, ReactNode } from "react";
import { Button, Checkbox, Input, Select, Tag, Tooltip, Typography } from "antd";
import { useTranslation } from "react-i18next";

import {
  MAX_DEPTH,
  SCHEMA_NODE_TYPES,
  changeNodeType,
  fieldIssue,
  newSchemaField,
  updateFieldsAt,
  updateNodeAt,
  type EditableSchemaNode,
  type SchemaField,
  type SchemaNode,
  type SchemaNodeType,
  type SchemaPath,
} from "../output_schema_tree";
import { OutputSchemaLimits } from "./OutputSchemaLimits";

const { Text } = Typography;

export const INDENT = 20;
export const COL_NAME = 170;
export const COL_TYPE = 110;
export const COL_REQUIRED = 50;

const ROW: CSSProperties = { display: "flex", gap: 8, marginBottom: 6, alignItems: "flex-start" };
const TOGGLE: CSSProperties = { width: 20, flex: "none", padding: 0 };

export interface TreeContext {
  root: SchemaNode;
  strict: boolean;
  commit: (next: SchemaNode) => void;
  collapsed: ReadonlySet<string>;
  toggleCollapsed: (key: string) => void;
}

const pathKey = (path: SchemaPath): string => path.join(".");

function hasChildren(node: SchemaNode): boolean {
  return node.kind === "editable" && (node.type === "object" || node.type === "array");
}

function Toggle({ path, ctx }: { path: SchemaPath; ctx: TreeContext }) {
  const { t } = useTranslation();
  const key = pathKey(path);
  const closed = ctx.collapsed.has(key);
  return (
    <Button
      type="text"
      size="small"
      style={TOGGLE}
      data-testid={`af-output-schema-toggle-${key}`}
      aria-label={t(closed ? "agent_form.output_schema.expand" : "agent_form.output_schema.collapse")}
      onClick={() => ctx.toggleCollapsed(key)}
    >
      {closed ? "+" : "-"}
    </Button>
  );
}

// Indent spacer + expand/collapse toggle (or a blank of the same width).
function Lead({ path, node, ctx }: { path: SchemaPath; node: SchemaNode; ctx: TreeContext }) {
  return (
    <>
      <span style={{ width: (path.length - 1) * INDENT, flex: "none" }} />
      {hasChildren(node) ? <Toggle path={path} ctx={ctx} /> : <span style={TOGGLE} />}
    </>
  );
}

function ReadonlyNote({ path }: { path: SchemaPath }) {
  const { t } = useTranslation();
  return (
    <span style={{ flex: 1, paddingTop: 4 }} data-testid={`af-output-schema-readonly-${pathKey(path)}`}>
      <Tag>{t("agent_form.output_schema.readonly_tag")}</Tag>
      <Text type="secondary" style={{ fontSize: 12 }}>
        {t("agent_form.output_schema.readonly_node")}
      </Text>
    </span>
  );
}

interface NodeControlsProps {
  path: SchemaPath;
  node: EditableSchemaNode;
  ctx: TreeContext;
  /** What sits in the "required" column (a checkbox on field rows). */
  requiredSlot: ReactNode;
}

// Type / description / limits / add-child controls shared by field and item rows.
function NodeControls({ path, node, ctx, requiredSlot }: NodeControlsProps) {
  const { t } = useTranslation();
  const key = pathKey(path);
  const depth = path.length;
  const patch = (fn: (n: EditableSchemaNode) => EditableSchemaNode): void =>
    ctx.commit(updateNodeAt(ctx.root, path, fn));
  // At the deepest level a new object/list would have nowhere to put children.
  const typeOptions = SCHEMA_NODE_TYPES.filter(
    (type) => depth < MAX_DEPTH || node.type === type || (type !== "object" && type !== "array"),
  ).map((type) => ({ value: type, label: t(`agent_form.output_schema.type_${type}`) }));
  const canAddChild = depth < MAX_DEPTH;
  return (
    <>
      <Select
        style={{ width: COL_TYPE, flex: "none" }}
        value={node.type}
        options={typeOptions}
        data-testid={`af-output-schema-type-${key}`}
        aria-label={t("agent_form.output_schema.col_type")}
        onChange={(v: SchemaNodeType) => patch((n) => changeNodeType(n, v))}
      />
      <span style={{ width: COL_REQUIRED, flex: "none", paddingTop: 4, textAlign: "center" }}>{requiredSlot}</span>
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

function Children({ path, node, ctx }: { path: SchemaPath; node: SchemaNode; ctx: TreeContext }) {
  if (node.kind !== "editable" || ctx.collapsed.has(pathKey(path))) return null;
  if (node.type === "object") return <FieldRows parentPath={path} fields={node.fields ?? []} ctx={ctx} />;
  if (node.type === "array" && node.items) return <ItemsRow path={[...path, "items"]} node={node.items} ctx={ctx} />;
  return null;
}

function ItemsRow({ path, node, ctx }: { path: SchemaPath; node: SchemaNode; ctx: TreeContext }) {
  const { t } = useTranslation();
  const key = pathKey(path);
  return (
    <>
      <div data-testid={`af-output-schema-row-${key}`} style={ROW}>
        <Lead path={path} node={node} ctx={ctx} />
        <Text type="secondary" style={{ width: COL_NAME, flex: "none", paddingTop: 4 }}>
          {t("agent_form.output_schema.items_label")}
        </Text>
        {node.kind === "editable" ? (
          <NodeControls path={path} node={node} ctx={ctx} requiredSlot={null} />
        ) : (
          <ReadonlyNote path={path} />
        )}
      </div>
      <Children path={path} node={node} ctx={ctx} />
    </>
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
  const editable = field.node.kind === "editable";
  const issue = fieldIssue(fields, index);
  const patchField = (patch: Partial<SchemaField>): void =>
    ctx.commit(updateFieldsAt(ctx.root, parentPath, (fs) => fs.map((f, i) => (i === index ? { ...f, ...patch } : f))));
  const remove = (): void =>
    ctx.commit(updateFieldsAt(ctx.root, parentPath, (fs) => fs.filter((_, i) => i !== index)));

  let nameCell: ReactNode;
  if (editable) {
    nameCell = (
      <div style={{ width: COL_NAME, flex: "none" }}>
        <Input
          value={field.name}
          status={issue ? "error" : undefined}
          aria-invalid={issue !== null}
          data-testid={`af-output-schema-name-${key}`}
          aria-label={t("agent_form.output_schema.col_name")}
          onChange={(e) => patchField({ name: e.target.value })}
        />
        {issue && (
          <Text type="danger" style={{ fontSize: 12, display: "block" }}>
            {t(issue === "duplicate" ? "agent_form.output_schema.name_duplicate" : "agent_form.output_schema.name_invalid")}
          </Text>
        )}
      </div>
    );
  } else {
    nameCell = (
      <Text type="secondary" style={{ width: COL_NAME, flex: "none", paddingTop: 4 }} data-testid={`af-output-schema-name-${key}`}>
        {field.name}
      </Text>
    );
  }

  return (
    <>
      <div
        data-testid={`af-output-schema-row-${key}`}
        style={{ ...ROW, opacity: editable ? 1 : 0.6 }}
      >
        <Lead path={path} node={field.node} ctx={ctx} />
        {nameCell}
        {field.node.kind === "editable" ? (
          <NodeControls
            path={path}
            node={field.node}
            ctx={ctx}
            requiredSlot={
              <Checkbox
                checked={field.required}
                data-testid={`af-output-schema-required-${key}`}
                aria-label={t("agent_form.output_schema.col_required")}
                onChange={(e) => patchField({ required: e.target.checked })}
              />
            }
          />
        ) : (
          <ReadonlyNote path={path} />
        )}
        <Button
          type="text"
          danger
          size="small"
          data-testid={`af-output-schema-remove-${key}`}
          aria-label={t("agent_form.output_schema.remove_field")}
          onClick={remove}
        >
          {t("agent_form.output_schema.remove_field")}
        </Button>
      </div>
      {editable && ctx.strict && !field.required && <StrictWarning depth={path.length} pathKeyStr={key} />}
      <Children path={path} node={field.node} ctx={ctx} />
    </>
  );
}

// Strict mode + an optional field: a reminder under the row; never blocks saving.
function StrictWarning({ depth, pathKeyStr }: { depth: number; pathKeyStr: string }) {
  const { t } = useTranslation();
  return (
    <div style={{ margin: "-4px 0 6px", paddingLeft: (depth - 1) * INDENT + 20 }}>
      <Tooltip title={t("agent_form.output_schema.strict_optional_tip")}>
        <Text type="warning" style={{ fontSize: 12 }} data-testid={`af-output-schema-strict-warn-${pathKeyStr}`}>
          {t("agent_form.output_schema.strict_optional_warn")}
        </Text>
      </Tooltip>
    </div>
  );
}

export function FieldRows({ parentPath, fields, ctx }: { parentPath: SchemaPath; fields: SchemaField[]; ctx: TreeContext }) {
  return (
    <>
      {fields.map((_, i) => (
        <FieldRow key={`${pathKey(parentPath)}/${i}`} parentPath={parentPath} fields={fields} index={i} ctx={ctx} />
      ))}
    </>
  );
}
