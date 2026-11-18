// Test-only: walk a field tree by path. Lets round-trip tests assert which
// nodes came out editable vs readonly (a round-trip that passes only
// because everything fell back to readonly would otherwise be a free pass).
import type { SchemaNode, SchemaPath } from "../output_schema_tree";

export function nodeAt(root: SchemaNode, path: SchemaPath): SchemaNode | undefined {
  let cur: SchemaNode | undefined = root;
  for (const seg of path) {
    if (cur?.kind !== "editable") return undefined;
    cur = seg === "items" ? cur.items : cur.fields?.[seg]?.node;
  }
  return cur;
}
