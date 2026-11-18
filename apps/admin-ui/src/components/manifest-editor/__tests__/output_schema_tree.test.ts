/**
 * output_schema_tree.test.ts — B-315 field-tree internals: constraint
 * read/write per type, the per-node readonly triggers, type changes dropping
 * what no longer applies, building new nodes, and field-name checks.
 */
import { describe, expect, it } from "vitest";

import {
  changeNodeType,
  fieldIssue,
  isValidFieldName,
  makeFieldUid,
  matchFieldUids,
  newSchemaField,
  newSchemaNode,
  parseSchemaNode,
  parseSchemaRoot,
  hasNameIssues,
  updateFieldsAt,
  updateNodeAt,
  writeSchemaNode,
  type EditableSchemaNode,
  type SchemaField,
  type SchemaNode,
} from "../output_schema_tree";
import { nodeAt } from "./schema_tree_test_utils";

function editable(node: SchemaNode | undefined): EditableSchemaNode {
  if (node?.kind !== "editable") throw new Error(`expected editable, got ${node?.kind}`);
  return node;
}

const field = (name: string, node: SchemaNode = newSchemaNode("string")): SchemaField => ({
  uid: makeFieldUid(),
  name,
  required: false,
  node,
});

describe("constraints: read and write per type", () => {
  const CASES: [string, Record<string, unknown>, Record<string, unknown>][] = [
    ["string minLength/maxLength/pattern", { type: "string", minLength: 1, maxLength: 300, pattern: "\\S" }, { minLength: 1, maxLength: 300, pattern: "\\S" }],
    ["number minimum/maximum", { type: "number", minimum: -1.5, maximum: 2 }, { minimum: -1.5, maximum: 2 }],
    ["integer minimum/maximum", { type: "integer", minimum: 1, maximum: 9 }, { minimum: 1, maximum: 9 }],
    ["array minItems/maxItems/uniqueItems", { type: "array", minItems: 1, maxItems: 3, uniqueItems: true, items: { type: "string" } }, { minItems: 1, maxItems: 3, uniqueItems: true }],
  ];

  it.each(CASES)("reads %s", (_label, raw, constraints) => {
    expect(editable(parseSchemaNode(raw, 1)).constraints).toEqual(constraints);
  });

  it.each(CASES)("writes %s onto a new node", (_label, raw, constraints) => {
    const type = raw.type as EditableSchemaNode["type"];
    const node = { ...newSchemaNode(type), constraints };
    expect(writeSchemaNode(node)).toEqual(raw);
  });

  it("enum reads and writes for string / number / integer", () => {
    for (const [type, values] of [
      ["string", ["a", "b"]],
      ["number", [0.5, 2]],
      ["integer", [1, 2]],
    ] as const) {
      const raw = { type, enum: [...values] };
      const node = editable(parseSchemaNode(raw, 1));
      expect(node.enum).toEqual(values);
      expect(writeSchemaNode({ ...node, enum: [values[0]] })).toEqual({ type, enum: [values[0]] });
    }
  });

  it("clearing a constraint removes the key", () => {
    const node = editable(parseSchemaNode({ type: "string", maxLength: 5, title: "T" }, 1));
    expect(writeSchemaNode({ ...node, constraints: {} })).toEqual({ type: "string", title: "T" });
  });
});

describe("readonly triggers (only that node)", () => {
  const CASES: [string, unknown][] = [
    ["oneOf", { oneOf: [{ type: "string" }] }],
    ["anyOf", { anyOf: [{ type: "string" }] }],
    ["allOf", { allOf: [{ type: "string" }] }],
    ["$ref", { $ref: "#/$defs/X" }],
    ["$defs", { type: "object", $defs: { X: { type: "string" } }, properties: {} }],
    ["const", { type: "string", const: "x" }],
    ["type union", { type: ["string", "null"] }],
    ["patternProperties", { type: "object", patternProperties: { "^x": { type: "string" } } }],
    ["if/then/else", { type: "object", if: {}, then: {}, else: {} }],
    ["unlisted key (minProperties)", { type: "object", properties: {}, minProperties: 1 }],
    ["unlisted key (contentEncoding)", { type: "string", contentEncoding: "base64" }],
    ["$schema below the root", { type: "string", $schema: "x" }],
    ["constraint on the wrong type", { type: "integer", minLength: 1 }],
    ["enum on boolean", { type: "boolean", enum: [true] }],
    ["enum value of the wrong type", { type: "integer", enum: [1.5] }],
    ["bad constraint value", { type: "string", maxLength: -1 }],
    ["array without items", { type: "array" }],
    ["required names a missing property", { type: "object", properties: {}, required: ["x"] }],
    ["required lists a name twice", { type: "object", properties: { x: { type: "string" } }, required: ["x", "x"] }],
    ["not an object", true],
  ];

  it.each(CASES)("%s", (_label, raw) => {
    const root = parseSchemaRoot({
      type: "object",
      properties: { odd: raw, plain: { type: "string" } },
    });
    expect(nodeAt(root, [0])).toEqual({ kind: "readonly", raw, reason: "unsupported" });
    expect(nodeAt(root, [1])?.kind).toBe("editable");
  });

  it("a property name the form would reject only locks the name; the node stays editable", () => {
    const raw = { type: "object", properties: { "1bad": { type: "string" }, "客户-名字": { type: "string", maxLength: 9 } } };
    const root = editable(parseSchemaRoot(raw));
    expect(root.fields?.map((f) => f.nameLocked)).toEqual([true, true]);
    expect(nodeAt(root, [1])?.kind).toBe("editable");
    expect(writeSchemaNode(updateNodeAt(root, [1], (n) => ({ ...n, description: "d" })))).toEqual({
      type: "object",
      properties: { "1bad": { type: "string" }, "客户-名字": { type: "string", maxLength: 9, description: "d" } },
    });
  });

  it("tuple-style items (an array) keep the list editable, the item row readonly", () => {
    const tuple = [{ type: "string" }, { type: "integer" }];
    const raw = { type: "object", properties: { pair: { type: "array", maxItems: 2, items: tuple } } };
    const root = parseSchemaRoot(raw);
    expect(nodeAt(root, [0])?.kind).toBe("editable");
    expect(nodeAt(root, [0, "items"])).toEqual({ kind: "readonly", raw: tuple, reason: "unsupported" });
    expect(JSON.stringify(writeSchemaNode(root))).toBe(JSON.stringify(raw));
  });
});

describe("pass-through keywords and root annotations", () => {
  it("root $schema/$id, format, multipleOf/exclusive bounds are kept and the nodes stay editable", () => {
    const raw = {
      $schema: "https://json-schema.org/draft/2020-12/schema",
      $id: "reply",
      type: "object",
      properties: {
        email: { type: "string", format: "email", maxLength: 80 },
        price: { type: "number", multipleOf: 0.01, exclusiveMinimum: 0, exclusiveMaximum: 1000 },
        count: { type: "integer", multipleOf: 2 },
      },
    };
    const root = parseSchemaRoot(raw);
    expect(root.kind).toBe("editable");
    expect([0, 1, 2].map((i) => nodeAt(root, [i])?.kind)).toEqual(["editable", "editable", "editable"]);
    expect(JSON.stringify(writeSchemaNode(root))).toBe(JSON.stringify(raw));
  });

  it("a type change drops format / multipleOf / exclusive bounds", () => {
    const email = editable(parseSchemaNode({ type: "string", format: "email" }, 1));
    expect(writeSchemaNode(changeNodeType(email, "integer"))).toEqual({ type: "integer" });
    const price = editable(parseSchemaNode({ type: "number", multipleOf: 0.5, exclusiveMinimum: 0 }, 1));
    expect(writeSchemaNode(changeNodeType(price, "string"))).toEqual({ type: "string" });
  });
});

describe("other values kept as written", () => {
  it("uniqueItems: false, required: [], and a schema-valued additionalProperties", () => {
    const raw = {
      type: "object",
      properties: {
        tags: { type: "array", uniqueItems: false, items: { type: "string" } },
        extra: { type: "object", properties: {}, required: [], additionalProperties: { type: "string" } },
      },
      required: [],
    };
    const root = parseSchemaRoot(raw);
    expect(nodeAt(root, [1])?.kind).toBe("editable");
    expect(JSON.stringify(writeSchemaNode(root))).toBe(JSON.stringify(raw));
  });
});

describe("changeNodeType", () => {
  it("string → integer drops string limits, enum, default and examples; keeps title/description/$comment", () => {
    const node = editable(
      parseSchemaNode(
        {
          type: "string",
          title: "T",
          description: "d",
          $comment: "c",
          default: "a",
          examples: ["a"],
          minLength: 1,
          pattern: "x",
          enum: ["a"],
        },
        1,
      ),
    );
    const next = changeNodeType(node, "integer");
    expect(next.constraints).toEqual({});
    expect(next.enum).toBeUndefined();
    expect(writeSchemaNode(next)).toEqual({ type: "integer", title: "T", description: "d", $comment: "c" });
  });

  it("number → integer rounds minimum up and maximum down", () => {
    const node = editable(parseSchemaNode({ type: "number", minimum: 0.5, maximum: 2.5 }, 1));
    expect(writeSchemaNode(changeNodeType(node, "integer"))).toEqual({ type: "integer", minimum: 1, maximum: 2 });
  });

  it("integer → number keeps minimum/maximum and a still-valid enum", () => {
    const node = editable(parseSchemaNode({ type: "integer", minimum: 1, enum: [1, 2] }, 1));
    expect(writeSchemaNode(changeNodeType(node, "number"))).toEqual({ type: "number", minimum: 1, enum: [1, 2] });
  });

  it("array → string drops items and list limits", () => {
    const node = editable(parseSchemaNode({ type: "array", maxItems: 4, items: { type: "string" } }, 1));
    expect(writeSchemaNode(changeNodeType(node, "string"))).toEqual({ type: "string" });
  });

  it("object → boolean drops properties/required/additionalProperties", () => {
    const node = editable(
      parseSchemaNode(
        { type: "object", properties: { a: { type: "string" } }, required: ["a"], additionalProperties: false },
        1,
      ),
    );
    expect(writeSchemaNode(changeNodeType(node, "boolean"))).toEqual({ type: "boolean" });
  });

  it("string → object / array start with an empty closed object / a text item", () => {
    const node = newSchemaNode("string");
    expect(writeSchemaNode(changeNodeType(node, "object"))).toEqual({
      type: "object",
      properties: {},
      additionalProperties: false,
    });
    expect(writeSchemaNode(changeNodeType(node, "array"))).toEqual({
      type: "array",
      items: { type: "string" },
    });
  });
});

describe("building and editing", () => {
  it("adding a required child keeps the existing required order and appends", () => {
    const root = parseSchemaRoot({
      type: "object",
      properties: { a: { type: "string" }, b: { type: "string" } },
      required: ["b", "a"],
    });
    const next = updateFieldsAt(root, [], (fs) => [...fs, { ...field("c"), required: true }]);
    expect(writeSchemaNode(next)).toEqual({
      type: "object",
      properties: { a: { type: "string" }, b: { type: "string" }, c: { type: "string" } },
      required: ["b", "a", "c"],
    });
  });

  it("adds a nested child under an object inside a list", () => {
    const root = parseSchemaRoot({
      type: "object",
      properties: { links: { type: "array", items: { type: "object", properties: {} } } },
    });
    const next = updateFieldsAt(root, [0, "items"], (fs) => [...fs, field("url")]);
    expect(writeSchemaNode(nodeAt(next, [0, "items"]) as SchemaNode)).toEqual({
      type: "object",
      properties: { url: { type: "string" } },
    });
  });

  it("updateNodeAt leaves the original tree untouched", () => {
    const root = parseSchemaRoot({ type: "object", properties: { a: { type: "string" } } });
    const before = JSON.stringify(writeSchemaNode(root));
    updateNodeAt(root, [0], (n) => ({ ...n, description: "x" }));
    expect(JSON.stringify(writeSchemaNode(root))).toBe(before);
  });

  it("removing a required field drops it from required", () => {
    const root = parseSchemaRoot({
      type: "object",
      properties: { a: { type: "string" }, b: { type: "string" } },
      required: ["a", "b"],
    });
    const next = updateFieldsAt(root, [], (fs) => fs.filter((_, i) => i !== 0));
    expect(writeSchemaNode(next)).toEqual({
      type: "object",
      properties: { b: { type: "string" } },
      required: ["b"],
    });
  });

  it("newSchemaField is a blank, optional text field with its own uid", () => {
    const a = newSchemaField();
    expect(a).toEqual({ uid: expect.any(String), name: "", required: false, node: newSchemaNode("string") });
    expect(newSchemaField().uid).not.toBe(a.uid);
  });

  it("writing a __proto__ field defines an own property and leaves the prototype alone", () => {
    const raw = JSON.parse('{"type":"object","properties":{"__proto__":{"type":"string"}}}') as unknown;
    const root = editable(parseSchemaRoot(raw));
    expect(root.fields?.[0].nameLocked).toBe(true);
    const out = writeSchemaNode(root) as { properties: Record<string, unknown> };
    expect(Object.keys(out.properties)).toEqual(["__proto__"]);
    expect(Object.getPrototypeOf(out.properties)).toBe(Object.prototype);
  });
});

describe("field names: any problem is an error, nothing is picked or dropped", () => {
  const RO = { kind: "readonly", raw: { oneOf: [] }, reason: "unsupported" } as const;

  it("flags blank and pattern-failing names, and BOTH rows of a duplicate", () => {
    const fields = [field("title"), field(""), field("1x"), field("title")];
    expect(fields.map((_, i) => fieldIssue(fields, i))).toEqual(["duplicate", "invalid", "invalid", "duplicate"]);
  });

  it("a readonly or name-locked field is never 'invalid' but is flagged on a clash", () => {
    const locked = { ...field("9odd"), nameLocked: true };
    expect(fieldIssue([field("9odd", RO)], 0)).toBeNull();
    expect(fieldIssue([locked], 0)).toBeNull();
    const clash = [field("b"), field("b", RO)];
    expect(clash.map((_, i) => fieldIssue(clash, i))).toEqual(["duplicate", "duplicate"]);
  });

  it("__proto__ is not a name the user may type", () => {
    expect(isValidFieldName("__proto__")).toBe(false);
    expect(fieldIssue([field("__proto__")], 0)).toBe("invalid");
  });

  it("hasNameIssues looks at every depth, list items included", () => {
    const deep = (inner: SchemaField[]) => ({
      ...newSchemaNode("object"),
      fields: [field("list", { ...newSchemaNode("array"), items: { ...newSchemaNode("object"), fields: inner } })],
    });
    expect(hasNameIssues(deep([field("a"), field("b")]))).toBe(false);
    expect(hasNameIssues(deep([field("a"), field("a")]))).toBe(true);
    expect(hasNameIssues(deep([field("")]))).toBe(true);
  });
});

describe("field uids", () => {
  const raw = {
    type: "object",
    properties: { a: { type: "array", items: { type: "object", properties: { b: { type: "string" } } } } },
  };

  it("every parse and every new field gets fresh uids", () => {
    const uids = (n: SchemaNode): string[] =>
      n.kind !== "editable"
        ? []
        : [...(n.fields ?? []).flatMap((f) => [f.uid, ...uids(f.node)]), ...(n.items ? uids(n.items) : [])];
    const all = [...uids(parseSchemaRoot(raw)), ...uids(parseSchemaRoot(raw)), newSchemaField().uid];
    expect(new Set(all).size).toBe(all.length);
  });

  it("matchFieldUids skips a field whose name currently has an issue (and its subtree)", () => {
    const before = editable(parseSchemaRoot(raw));
    const renamed = updateFieldsAt(before, [], (fs) => [...fs, { ...field("a") }]);
    expect(matchFieldUids(renamed, parseSchemaRoot(raw)).size).toBe(0);
  });

  it("matchFieldUids pairs fields by name path across two reads", () => {
    const before = editable(parseSchemaRoot(raw));
    const after = editable(
      parseSchemaRoot({ type: "object", properties: { z: { type: "string" }, ...raw.properties } }),
    );
    const map = matchFieldUids(before, after);
    const beforeA = before.fields?.[0];
    const afterA = after.fields?.[1];
    const inner = (n: SchemaNode | undefined) => editable(editable(n).items).fields?.[0].uid;
    expect(map.get(beforeA?.uid ?? "")).toBe(afterA?.uid);
    expect(map.get(inner(beforeA?.node) ?? "")).toBe(inner(afterA?.node));
    expect(map.size).toBe(2);
  });
});
