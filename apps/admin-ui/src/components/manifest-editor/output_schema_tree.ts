// B-315 — the structured-output "field tree": a nested, editable view over
// ``spec.output_schema.json_schema`` (a JSON Schema). Pure functions only;
// form_model.ts adapts them to the manifest (read/set/strict).
//
// Core guarantee: ``writeSchemaNode(parseSchemaRoot(x))`` deep-equals ``x``
// for every input, key order included. How:
//  - a node the form cannot represent (oneOf/anyOf/allOf/$ref/$defs/const,
//    a type union, patternProperties, if/then/else, depth > MAX_DEPTH, or
//    any key outside the editable set below) becomes a ``readonly`` node
//    holding its original value, written back verbatim. Only THAT node is
//    readonly — siblings and ancestors stay editable.
//  - an editable node keeps its original object (``raw``). The writer walks
//    ``raw``'s keys in order: a key the form manages is re-emitted from the
//    node (or dropped when the node no longer has it); any other key
//    (``title``/``default``/``examples``/``$comment`` annotations) is copied
//    from ``raw`` untouched. Keys the form newly adds are appended after.

export type SchemaNodeType =
  | "string"
  | "number"
  | "integer"
  | "boolean"
  | "object"
  | "array";

export const SCHEMA_NODE_TYPES: readonly SchemaNodeType[] = [
  "string",
  "number",
  "integer",
  "boolean",
  "object",
  "array",
];

/** Fields of the root object are depth 1; deeper than this reads readonly. */
export const MAX_DEPTH = 5;

/** Same field-name rule the flat editor used. */
export const NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

export interface SchemaConstraints {
  minLength?: number;
  maxLength?: number;
  pattern?: string;
  minimum?: number;
  maximum?: number;
  minItems?: number;
  maxItems?: number;
  uniqueItems?: boolean;
}
export type ConstraintKey = keyof SchemaConstraints;

export interface EditableSchemaNode {
  kind: "editable";
  type: SchemaNodeType;
  /** Root only: the original had no ``type`` key (object implied). */
  typeImplicit?: boolean;
  description?: string;
  enum?: (string | number)[];
  constraints: SchemaConstraints;
  /** object: the properties, in order. */
  fields?: SchemaField[];
  /** object: original ``additionalProperties`` (absent = undefined). */
  additionalProperties?: unknown;
  /** object: write ``properties: {}`` even when there are no fields. */
  keepEmptyProperties?: boolean;
  /** object: write ``required: []`` even when nothing is required. */
  keepEmptyRequired?: boolean;
  /** array: the "each item" node. */
  items?: SchemaNode;
  /** The original object (key order + annotations); absent = brand-new. */
  raw?: Record<string, unknown>;
}

export interface ReadonlySchemaNode {
  kind: "readonly";
  raw: unknown;
}

export type SchemaNode = EditableSchemaNode | ReadonlySchemaNode;

export interface SchemaField {
  name: string;
  required: boolean;
  node: SchemaNode;
}

/** A node's position: field indexes, plus ``"items"`` to step into a list. */
export type SchemaPath = readonly (number | "items")[];

const CONSTRAINTS_BY_TYPE: Record<SchemaNodeType, readonly ConstraintKey[]> = {
  string: ["minLength", "maxLength", "pattern"],
  number: ["minimum", "maximum"],
  integer: ["minimum", "maximum"],
  boolean: [],
  object: [],
  array: ["minItems", "maxItems", "uniqueItems"],
};

export function constraintKeysFor(type: SchemaNodeType): readonly ConstraintKey[] {
  return CONSTRAINTS_BY_TYPE[type];
}

export function supportsEnum(type: SchemaNodeType): boolean {
  return type === "string" || type === "number" || type === "integer";
}

const ANNOTATION_KEYS = ["title", "default", "examples", "$comment"];
const STRUCTURE_KEYS: Record<SchemaNodeType, readonly string[]> = {
  string: [],
  number: [],
  integer: [],
  boolean: [],
  object: ["properties", "required", "additionalProperties"],
  array: ["items"],
};

// Order used for keys the form adds to a node that didn't have them.
const KEY_ORDER = [
  "type",
  "description",
  "enum",
  "minLength",
  "maxLength",
  "pattern",
  "minimum",
  "maximum",
  "minItems",
  "maxItems",
  "uniqueItems",
  "items",
  "properties",
  "required",
  "additionalProperties",
];

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

const isCount = (v: unknown): boolean => Number.isInteger(v) && (v as number) >= 0;
const isFiniteNumber = (v: unknown): boolean =>
  typeof v === "number" && Number.isFinite(v);

const CONSTRAINT_VALID: Record<ConstraintKey, (v: unknown) => boolean> = {
  minLength: isCount,
  maxLength: isCount,
  pattern: (v) => typeof v === "string",
  minimum: isFiniteNumber,
  maximum: isFiniteNumber,
  minItems: isCount,
  maxItems: isCount,
  uniqueItems: (v) => typeof v === "boolean",
};

export function enumValidFor(type: SchemaNodeType, values: unknown): boolean {
  if (!Array.isArray(values)) return false;
  if (type === "string") return values.every((v) => typeof v === "string");
  if (type === "number") return values.every(isFiniteNumber);
  if (type === "integer") return values.every((v) => Number.isInteger(v));
  return false;
}

// ---- read ----

const readonlyNode = (raw: unknown): ReadonlySchemaNode => ({ kind: "readonly", raw });

function parseFields(
  raw: Record<string, unknown>,
  depth: number,
): SchemaField[] | null {
  const props = "properties" in raw ? raw.properties : {};
  if (!isPlainObject(props)) return null;
  const required = "required" in raw ? raw.required : [];
  if (!Array.isArray(required) || !required.every((r) => typeof r === "string")) {
    return null;
  }
  const names = Object.keys(props);
  if (new Set(required).size !== required.length) return null;
  if (!required.every((r) => names.includes(r))) return null;
  return names.map((name) => ({
    name,
    required: required.includes(name),
    // A name the form's own rule would reject can't be re-typed safely, so
    // the whole field stays as-is (readonly) rather than dropping it.
    node: NAME_RE.test(name)
      ? parseSchemaNode(props[name], depth + 1)
      : readonlyNode(props[name]),
  }));
}

/** ``depth`` = this node's depth (root 0, the root's fields 1, …). */
export function parseSchemaNode(
  raw: unknown,
  depth: number,
  opts: { implicitObject?: boolean } = {},
): SchemaNode {
  if (depth > MAX_DEPTH || !isPlainObject(raw)) return readonlyNode(raw);
  const typeImplicit = !("type" in raw) && opts.implicitObject === true;
  const type = typeImplicit ? "object" : raw.type;
  if (typeof type !== "string" || !SCHEMA_NODE_TYPES.includes(type as SchemaNodeType)) {
    return readonlyNode(raw);
  }
  const t = type as SchemaNodeType;
  const allowed = new Set<string>([
    "type",
    "description",
    ...ANNOTATION_KEYS,
    ...CONSTRAINTS_BY_TYPE[t],
    ...STRUCTURE_KEYS[t],
    ...(supportsEnum(t) ? ["enum"] : []),
  ]);
  if (!Object.keys(raw).every((k) => allowed.has(k))) return readonlyNode(raw);
  if ("description" in raw && typeof raw.description !== "string") return readonlyNode(raw);
  if ("enum" in raw && !enumValidFor(t, raw.enum)) return readonlyNode(raw);

  const constraints: SchemaConstraints = {};
  for (const key of CONSTRAINTS_BY_TYPE[t]) {
    if (!(key in raw)) continue;
    if (!CONSTRAINT_VALID[key](raw[key])) return readonlyNode(raw);
    (constraints as Record<string, unknown>)[key] = raw[key];
  }

  const node: EditableSchemaNode = { kind: "editable", type: t, constraints, raw };
  if (typeImplicit) node.typeImplicit = true;
  if ("description" in raw) node.description = raw.description as string;
  if ("enum" in raw) node.enum = [...(raw.enum as (string | number)[])];
  if (t === "object") {
    const fields = parseFields(raw, depth);
    if (fields === null) return readonlyNode(raw);
    node.fields = fields;
    if ("additionalProperties" in raw) node.additionalProperties = raw.additionalProperties;
    node.keepEmptyProperties = "properties" in raw;
    node.keepEmptyRequired = "required" in raw;
  }
  if (t === "array") {
    if (!("items" in raw)) return readonlyNode(raw);
    node.items = parseSchemaNode(raw.items, depth + 1);
  }
  return node;
}

/** The ``json_schema`` value → the root node (always an object, or readonly). */
export function parseSchemaRoot(raw: unknown): SchemaNode {
  const node = parseSchemaNode(raw, 0, { implicitObject: true });
  return node.kind === "editable" && node.type !== "object" ? readonlyNode(raw) : node;
}

// ---- write ----

function requiredList(node: EditableSchemaNode): string[] {
  const fields = node.fields ?? [];
  const wanted = new Set(fields.filter((f) => f.required).map((f) => f.name));
  const original = Array.isArray(node.raw?.required)
    ? (node.raw.required as unknown[]).filter((r): r is string => typeof r === "string")
    : [];
  const kept = original.filter((n) => wanted.has(n));
  const added = fields
    .filter((f) => f.required && !original.includes(f.name))
    .map((f) => f.name);
  return [...kept, ...added];
}

// The value each managed key should have now; ``undefined`` = omit the key.
function managedValues(node: EditableSchemaNode): Record<string, unknown> {
  const out: Record<string, unknown> = {
    type: node.typeImplicit && node.type === "object" ? undefined : node.type,
    description: node.description,
    enum: supportsEnum(node.type) ? node.enum : undefined,
  };
  for (const key of CONSTRAINTS_BY_TYPE[node.type]) out[key] = node.constraints[key];
  if (node.type === "object") {
    const fields = node.fields ?? [];
    const properties: Record<string, unknown> = {};
    for (const f of fields) properties[f.name] = writeSchemaNode(f.node);
    const required = requiredList(node);
    out.properties =
      fields.length > 0 || node.keepEmptyProperties ? properties : undefined;
    out.required =
      required.length > 0 || node.keepEmptyRequired ? required : undefined;
    out.additionalProperties = node.additionalProperties;
  }
  if (node.type === "array" && node.items) out.items = writeSchemaNode(node.items);
  return out;
}

export function writeSchemaNode(node: SchemaNode): unknown {
  if (node.kind === "readonly") return node.raw;
  const managed = managedValues(node);
  const raw = node.raw ?? {};
  const out: Record<string, unknown> = {};
  for (const key of Object.keys(raw)) {
    if (key in managed || KEY_ORDER.includes(key)) {
      if (managed[key] !== undefined) out[key] = managed[key];
    } else {
      out[key] = raw[key];
    }
  }
  for (const key of KEY_ORDER) {
    if (!(key in raw) && managed[key] !== undefined) out[key] = managed[key];
  }
  return out;
}

// ---- build / edit (all immutable) ----

export function newSchemaNode(type: SchemaNodeType): EditableSchemaNode {
  if (type === "object") {
    return {
      kind: "editable",
      type,
      constraints: {},
      fields: [],
      additionalProperties: false,
      keepEmptyProperties: true,
    };
  }
  if (type === "array") {
    return { kind: "editable", type, constraints: {}, items: newSchemaNode("string") };
  }
  return { kind: "editable", type, constraints: {} };
}

export const newSchemaField = (): SchemaField => ({
  name: "",
  required: false,
  node: newSchemaNode("string"),
});

/** Switch a node's type: drops constraints/enum/children the new type can't use. */
export function changeNodeType(
  node: EditableSchemaNode,
  type: SchemaNodeType,
): EditableSchemaNode {
  if (node.type === type) return node;
  const fresh = newSchemaNode(type);
  const constraints: SchemaConstraints = {};
  for (const key of CONSTRAINTS_BY_TYPE[type]) {
    if (node.constraints[key] !== undefined) {
      (constraints as Record<string, unknown>)[key] = node.constraints[key];
    }
  }
  return {
    ...fresh,
    constraints,
    description: node.description,
    enum: node.enum !== undefined && enumValidFor(type, node.enum) ? node.enum : undefined,
    raw: node.raw,
  };
}

/** The node at ``path`` (``undefined`` when the path doesn't resolve). */
export function nodeAt(root: SchemaNode, path: SchemaPath): SchemaNode | undefined {
  let cur: SchemaNode | undefined = root;
  for (const seg of path) {
    if (cur?.kind !== "editable") return undefined;
    cur = seg === "items" ? cur.items : cur.fields?.[seg]?.node;
  }
  return cur;
}

/** Replace the editable node at ``path`` with ``fn(node)``; others untouched. */
export function updateNodeAt(
  root: SchemaNode,
  path: SchemaPath,
  fn: (node: EditableSchemaNode) => EditableSchemaNode,
): SchemaNode {
  if (root.kind !== "editable") return root;
  if (path.length === 0) return fn(root);
  const [seg, ...rest] = path;
  if (seg === "items") {
    return root.items ? { ...root, items: updateNodeAt(root.items, rest, fn) } : root;
  }
  const fields = root.fields;
  if (!fields?.[seg]) return root;
  return {
    ...root,
    fields: fields.map((f, i) =>
      i === seg ? { ...f, node: updateNodeAt(f.node, rest, fn) } : f,
    ),
  };
}

/** Replace the fields of the object node at ``parentPath``. */
export function updateFieldsAt(
  root: SchemaNode,
  parentPath: SchemaPath,
  fn: (fields: SchemaField[]) => SchemaField[],
): SchemaNode {
  return updateNodeAt(root, parentPath, (n) =>
    n.type === "object" ? { ...n, fields: fn(n.fields ?? []) } : n,
  );
}

// ---- field-name checks ----

export type FieldIssue = "invalid" | "duplicate" | null;

/**
 * ``"invalid"`` = fails NAME_RE; ``"duplicate"`` = an EARLIER sibling has
 * the same name (first one wins). Readonly fields keep their original name
 * and are never flagged.
 */
export function fieldIssue(fields: readonly SchemaField[], i: number): FieldIssue {
  const field = fields[i];
  if (field.node.kind === "readonly") return null;
  if (!NAME_RE.test(field.name)) return "invalid";
  for (let j = 0; j < i; j++) {
    if (fields[j].name === field.name) return "duplicate";
  }
  return null;
}

/** Drop every field (at any depth) whose name currently has an issue. */
export function pruneInvalidFields(node: SchemaNode): SchemaNode {
  if (node.kind !== "editable") return node;
  const next: EditableSchemaNode = { ...node };
  if (node.fields) {
    next.fields = node.fields
      .filter((_, i) => fieldIssue(node.fields as SchemaField[], i) === null)
      .map((f) => ({ ...f, node: pruneInvalidFields(f.node) }));
  }
  if (node.items) next.items = pruneInvalidFields(node.items);
  return next;
}
