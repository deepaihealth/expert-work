/**
 * form_model_output_schema.test.ts — B-315 structured-output field tree,
 * manifest adapters (``readOutputSchemaTree``/``setOutputSchemaTree``/
 * ``readOutputSchemaStrict``) over ``output_schema_tree.ts``.
 *
 * The linchpin invariant is the round-trip: read → write with no edits must
 * deep-equal the original ``output_schema`` block, KEY ORDER INCLUDED (each
 * case also compares ``JSON.stringify``, which is order-sensitive). Every
 * round-trip case also pins which nodes came out editable vs readonly — a
 * round-trip that passes only because the whole root fell back to readonly
 * would otherwise be a free pass.
 */
import { describe, expect, it } from "vitest";

import {
  readOutputSchemaStrict,
  readOutputSchemaTree,
  setOutputSchemaTree,
  type AgentManifest,
} from "../form_model";
import {
  newSchemaField,
  nodeAt,
  updateFieldsAt,
  updateNodeAt,
  type SchemaNode,
  type SchemaPath,
} from "../output_schema_tree";
import { parseYaml } from "../yaml";

const BLANK: AgentManifest = { spec: {} };

const HEALTH_MANAGER_YAML = `
json_schema:
  type: object
  additionalProperties: false
  required: [messages, media]
  properties:
    messages:
      type: array
      maxItems: 4
      items: {type: string, minLength: 1, maxLength: 300, pattern: "\\\\S"}
    media:
      type: array
      maxItems: 3
      uniqueItems: true
      items: {type: integer, minimum: 1}
`;

const healthManagerBlock = (): Record<string, unknown> =>
  parseYaml(HEALTH_MANAGER_YAML) as Record<string, unknown>;

const manifestWith = (block: unknown): AgentManifest =>
  ({ spec: { output_schema: block } }) as AgentManifest;

function rootOf(block: unknown): SchemaNode {
  const root = readOutputSchemaTree(manifestWith(block));
  if (root === undefined) throw new Error("expected a configured block");
  return root;
}

function kindAt(root: SchemaNode, path: SchemaPath): string | undefined {
  return nodeAt(root, path)?.kind;
}

// read → write with no edits; returns the written block.
function roundTrip(block: unknown): unknown {
  const m = manifestWith(block);
  return setOutputSchemaTree(m, rootOf(block)).spec?.output_schema;
}

function expectIdentical(actual: unknown, expected: unknown): void {
  expect(actual).toEqual(expected);
  expect(JSON.stringify(actual)).toBe(JSON.stringify(expected));
}

describe("round-trip: read → write unchanged is identical (key order too)", () => {
  it("health-manager schema", () => {
    const block = healthManagerBlock();
    const root = rootOf(block);
    expect(kindAt(root, [])).toBe("editable");
    expect(kindAt(root, [0])).toBe("editable");
    expect(kindAt(root, [0, "items"])).toBe("editable");
    expect(kindAt(root, [1, "items"])).toBe("editable");
    expectIdentical(roundTrip(block), block);
  });

  it("an object-list schema (list items are objects; required ≠ property order)", () => {
    const block = {
      name: "links_reply",
      strict: true,
      json_schema: {
        type: "object",
        properties: {
          summary: { type: "string", description: "one line" },
          links: {
            description: "related links",
            type: "array",
            minItems: 1,
            items: {
              type: "object",
              properties: {
                title: { type: "string" },
                url: { type: "string", pattern: "^https://" },
              },
              required: ["url", "title"],
              additionalProperties: false,
            },
          },
        },
        required: ["links", "summary"],
        additionalProperties: false,
      },
    };
    const root = rootOf(block);
    expect(kindAt(root, [1, "items"])).toBe("editable");
    expect(kindAt(root, [1, "items", 1])).toBe("editable");
    expectIdentical(roundTrip(block), block);
  });

  it("5 levels of nesting are all editable", () => {
    const block = {
      json_schema: {
        type: "object",
        properties: {
          l1: {
            type: "object",
            properties: {
              l2: {
                type: "array",
                items: {
                  type: "object",
                  properties: {
                    l4: {
                      type: "object",
                      properties: { l5: { type: "string", maxLength: 9 } },
                      required: ["l5"],
                    },
                  },
                },
              },
            },
          },
        },
      },
    };
    const root = rootOf(block);
    expect(kindAt(root, [0, 0, "items", 0, 0])).toBe("editable");
    expectIdentical(roundTrip(block), block);
  });

  it("a 6th level reads readonly, its level-5 parent stays editable", () => {
    const deep = { type: "string", minLength: 2 };
    const block = {
      json_schema: {
        type: "object",
        properties: {
          a: {
            type: "object",
            properties: {
              b: {
                type: "object",
                properties: {
                  c: {
                    type: "object",
                    properties: {
                      d: {
                        type: "object",
                        properties: { e: { type: "object", properties: { f: deep } } },
                      },
                    },
                  },
                },
              },
            },
          },
        },
      },
    };
    const root = rootOf(block);
    expect(kindAt(root, [0, 0, 0, 0, 0])).toBe("editable");
    expect(nodeAt(root, [0, 0, 0, 0, 0, 0])).toEqual({ kind: "readonly", raw: deep, reason: "depth" });
    expectIdentical(roundTrip(block), block);
  });

  it("a oneOf node is readonly, its siblings editable", () => {
    const choice = { oneOf: [{ type: "string" }, { type: "integer" }] };
    const block = {
      json_schema: {
        type: "object",
        properties: { kind: choice, note: { type: "string" } },
        required: ["kind"],
        additionalProperties: false,
      },
    };
    const root = rootOf(block);
    expect(nodeAt(root, [0])).toEqual({ kind: "readonly", raw: choice, reason: "unsupported" });
    expect(kindAt(root, [1])).toBe("editable");
    expectIdentical(roundTrip(block), block);
  });

  it("title/default/examples/$comment annotations survive", () => {
    const block = {
      json_schema: {
        title: "Reply",
        $comment: "v2",
        type: "object",
        properties: {
          level: {
            description: "severity",
            title: "Level",
            type: "string",
            enum: ["low", "high"],
            default: "low",
            examples: ["high"],
          },
          score: { type: "number", default: 0, minimum: 0, maximum: 1 },
        },
      },
    };
    expect(kindAt(rootOf(block), [0])).toBe("editable");
    expectIdentical(roundTrip(block), block);
  });

  it("the existing flat sample (no required key, no type on root)", () => {
    const block = {
      json_schema: {
        properties: {
          title: { type: "string", description: "the title" },
          score: { type: "number" },
          rank: { type: "integer" },
          ok: { type: "boolean" },
          tags: { type: "array", items: { type: "string" } },
          scores: { type: "array", items: { type: "number" } },
        },
      },
    };
    const root = rootOf(block);
    expect(kindAt(root, [])).toBe("editable");
    expect(kindAt(root, [4, "items"])).toBe("editable");
    expectIdentical(roundTrip(block), block);
  });

  it("one edit changes only that value (health manager messages.maxItems 4 → 3)", () => {
    const block = healthManagerBlock();
    const m = manifestWith(block);
    const edited = updateNodeAt(rootOf(block), [0], (n) => ({
      ...n,
      constraints: { ...n.constraints, maxItems: 3 },
    }));
    const expected = structuredClone(block) as {
      json_schema: { properties: { messages: { maxItems: number } } };
    };
    expected.json_schema.properties.messages.maxItems = 3;
    expectIdentical(setOutputSchemaTree(m, edited).spec?.output_schema, expected);
  });
});

describe("readOutputSchemaTree", () => {
  it("undefined when output_schema is absent or null", () => {
    expect(readOutputSchemaTree(BLANK)).toBeUndefined();
    expect(readOutputSchemaTree({ spec: { output_schema: null } })).toBeUndefined();
  });

  it("root readonly when json_schema is missing, not an object, or not type object", () => {
    expect(readOutputSchemaTree({ spec: { output_schema: {} } })?.kind).toBe("readonly");
    expect(rootOf({ json_schema: "nope" }).kind).toBe("readonly");
    expect(rootOf({ json_schema: { type: "array", items: {} } }).kind).toBe("readonly");
    expect(rootOf({ json_schema: { oneOf: [{ type: "object" }] } }).kind).toBe("readonly");
  });
});

describe("setOutputSchemaTree", () => {
  it("null deletes the whole output_schema block", () => {
    const next = setOutputSchemaTree(manifestWith(healthManagerBlock()), null);
    expect(next.spec?.output_schema).toBeUndefined();
  });

  it("keeps name/strict and other sibling keys untouched", () => {
    const block = { name: "custom", strict: false, extra: 1, json_schema: { type: "object" } };
    const next = setOutputSchemaTree(manifestWith(block), rootOf(block));
    expect(next.spec?.output_schema).toEqual(block);
  });

  it("a brand-new block writes no name/strict", () => {
    const next = setOutputSchemaTree(BLANK, rootOf({ json_schema: { type: "object" } }));
    expect(next.spec?.output_schema).toEqual({ json_schema: { type: "object" } });
  });
});

describe("root without a type key", () => {
  it("adding a field keeps the root type-less: { properties: { x: {type: string} } }", () => {
    // The backend allows this: OutputSchemaSpec._check_json_schema_shape
    // (packages/expert-work-protocol/.../agent_spec.py) only rejects an empty
    // json_schema or a top-level ``type`` other than "object"; omitted is OK.
    const m = manifestWith({ json_schema: {} });
    const root = updateFieldsAt(rootOf({ json_schema: {} }), [], (fs) => [
      ...fs,
      { ...newSchemaField(), name: "x" },
    ]);
    expectIdentical(setOutputSchemaTree(m, root).spec?.output_schema, {
      json_schema: { properties: { x: { type: "string" } } },
    });
  });
});

describe("readOutputSchemaStrict", () => {
  it("strict unless the block says strict: false", () => {
    expect(readOutputSchemaStrict(manifestWith({ json_schema: {} }))).toBe(true);
    expect(readOutputSchemaStrict(manifestWith({ strict: true, json_schema: {} }))).toBe(true);
    expect(readOutputSchemaStrict(manifestWith({ strict: false, json_schema: {} }))).toBe(false);
  });
});
