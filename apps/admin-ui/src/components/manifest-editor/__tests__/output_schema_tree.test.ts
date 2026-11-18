/**
 * output_schema_tree.test.ts — B-315 field-tree internals: constraint
 * read/write per type, the per-node readonly triggers, type changes dropping
 * what no longer applies, building new nodes, and field-name checks.
 */
import { describe, expect, it } from "vitest";

import {
  changeNodeType,
  fieldIssue,
  newSchemaField,
  newSchemaNode,
  nodeAt,
  parseSchemaNode,
  parseSchemaRoot,
  pruneInvalidFields,
  updateFieldsAt,
  updateNodeAt,
  writeSchemaNode,
  type EditableSchemaNode,
  type SchemaField,
  type SchemaNode,
} from "../output_schema_tree";

function editable(node: SchemaNode | undefined): EditableSchemaNode {
  if (node?.kind !== "editable") throw new Error(`expected editable, got ${node?.kind}`);
  return node;
}

const field = (name: string, node: SchemaNode = newSchemaNode("string")): SchemaField => ({
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
    ["unlisted key (format)", { type: "string", format: "email" }],
    ["constraint on the wrong type", { type: "integer", minLength: 1 }],
    ["enum on boolean", { type: "boolean", enum: [true] }],
    ["enum value of the wrong type", { type: "integer", enum: [1.5] }],
    ["bad constraint value", { type: "string", maxLength: -1 }],
    ["array without items", { type: "array" }],
    ["required names a missing property", { type: "object", properties: {}, required: ["x"] }],
    ["not an object", true],
  ];

  it.each(CASES)("%s", (_label, raw) => {
    const root = parseSchemaRoot({
      type: "object",
      properties: { odd: raw, plain: { type: "string" } },
    });
    expect(nodeAt(root, [0])).toEqual({ kind: "readonly", raw });
    expect(nodeAt(root, [1])?.kind).toBe("editable");
  });

  it("a property name the form would reject keeps that field readonly", () => {
    const root = parseSchemaRoot({ type: "object", properties: { "1bad": { type: "string" } } });
    expect(nodeAt(root, [0])?.kind).toBe("readonly");
  });
});

describe("changeNodeType", () => {
  it("string → integer drops string limits and string enum, keeps description/annotations", () => {
    const node = editable(
      parseSchemaNode({ type: "string", title: "T", description: "d", minLength: 1, pattern: "x", enum: ["a"] }, 1),
    );
    const next = changeNodeType(node, "integer");
    expect(next.constraints).toEqual({});
    expect(next.enum).toBeUndefined();
    expect(writeSchemaNode(next)).toEqual({ type: "integer", title: "T", description: "d" });
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

  it("newSchemaField is a blank, optional text field", () => {
    expect(newSchemaField()).toEqual({ name: "", required: false, node: newSchemaNode("string") });
  });
});

describe("field names", () => {
  it("flags blank / pattern-failing names and later duplicates only", () => {
    const fields = [field("title"), field(""), field("1x"), field("title")];
    expect(fields.map((_, i) => fieldIssue(fields, i))).toEqual([null, "invalid", "invalid", "duplicate"]);
  });

  it("never flags a readonly field", () => {
    const fields = [field("9odd", { kind: "readonly", raw: {} })];
    expect(fieldIssue(fields, 0)).toBeNull();
  });

  it("pruneInvalidFields drops bad names at every depth", () => {
    const inner = { ...newSchemaNode("object"), fields: [field("ok"), field("")] };
    const root = { ...newSchemaNode("object"), fields: [field("x", inner), field("x")] };
    expect(writeSchemaNode(pruneInvalidFields(root))).toEqual({
      type: "object",
      properties: {
        x: { type: "object", properties: { ok: { type: "string" } }, additionalProperties: false },
      },
      additionalProperties: false,
    });
  });
});
