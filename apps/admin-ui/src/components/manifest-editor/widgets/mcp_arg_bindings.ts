/**
 * McpToolPicker 的纯逻辑部分 —— 参数绑定的形状判断,不碰 React、不碰状态。
 *
 * 拆出来是因为 McpToolPicker.tsx 到了 800 行的上限:这三个函数自洽、可单独测,
 * 而它们各自带的那段「为什么是这个判据」的说明又必须留着(见下面两段)。
 * 有状态的那部分(setBinding / renderBindings)仍留在组件里,不在这次搬运范围内。
 */
import type { McpTool } from "../../../api/mcp-servers";
import type { ArgBindingFields } from "../form_model";

/** The "model fills this in" choice. Empty string, so it can never collide
 *  with a declared variable name (those are non-empty by construction). */
export const AUTO = "";

/** One row of the binding editor: a parameter of the tool's input schema. */
export interface ToolParam {
  name: string;
  required: boolean;
}

/**
 * Read a tool's parameters out of its JSON Schema.
 *
 * ``input_schema`` is whatever a THIRD-PARTY MCP server advertised — untrusted
 * input that only happens to be typed ``Record<string, unknown>``. So both
 * sides are whitelisted by shape, the same way the backend does it
 * (``tools/arg_bindings.py:_top_level_params``): a shape that is not on the
 * whitelist is treated as absent, never coerced and never thrown on.
 *
 * ``typeof [] === "object"`` is the one that bites: without the
 * ``Array.isArray`` guard a ``properties: [...]`` would hand back "0"/"1" as
 * parameter names and offer the operator a binding to a parameter that does
 * not exist.
 *
 * Where this deliberately differs from the backend: there, a bad shape on
 * EITHER side makes the whole schema undecidable, because both sides feed the
 * same strip-the-parameter decision. Here they feed different things —
 * ``properties`` decides whether anything can be bound at all, ``required``
 * only draws an asterisk — so a malformed ``required`` costs the asterisks and
 * nothing else, rather than hiding every parameter.
 */
export function paramsOf(tool: McpTool): ToolParam[] {
  const schema = tool.input_schema;
  if (schema === null || typeof schema !== "object") return [];
  const props = (schema as Record<string, unknown>).properties;
  if (props === null || typeof props !== "object" || Array.isArray(props)) {
    return [];
  }
  const rawRequired = (schema as Record<string, unknown>).required;
  const required = new Set(
    Array.isArray(rawRequired)
      ? rawRequired.filter((x): x is string => typeof x === "string")
      : [],
  );
  return Object.keys(props as Record<string, unknown>).map((name) => ({
    name,
    required: required.has(name),
  }));
}

/**
 * Whether ``server``/``tool`` is inside the agent's current MCP scope — one
 * place for both "may this tool be bound at all" and "is this existing binding
 * still legal".
 *
 * It is not a nicety: ``AgentSpecBody._check_arg_bindings`` REJECTS the save
 * outright (``raise ValueError``, not a warning) when a binding falls outside
 * the entry it lives in. The backend's two clauses, verbatim
 * (``agent_spec.py:1481`` / ``:1488``):
 *
 *     if entry.servers and binding.server not in entry.servers: ...
 *     if entry.allow_tools and binding.tool not in entry.allow_tools: ...
 *
 * so BOTH sides are symmetric: an empty list means "all", and that clause then
 * does not apply. The predicate below is deliberately stricter than that on
 * the ``servers`` side — and that is not a mirroring bug, it is the point:
 *
 *  - In this picker an empty ``servers`` never means "all servers". Selecting
 *    a server IS enabling MCP, so empty means MCP is being turned OFF, and
 *    ``setMcp`` drops the whole entry. Treating every binding as out of scope
 *    is therefore exactly right — it is what makes the drop-confirm fire when
 *    the last server is unchecked. Relaxing this to match the backend clause
 *    literally would silently delete that confirm.
 *  - The two sides never disagree about a state the backend actually sees: an
 *    entry the picker can produce always has a non-empty ``servers`` (empty ⇒
 *    no entry), and the tool sub-modal — the only way to reach a binding
 *    editor — opens only from a CHECKED server.
 *
 * A hand-written ``servers: []`` plus bindings is the one manifest where they
 * differ. Nothing here touches it: no server is checked, so there is no gear,
 * no sub-modal and no binding editor. The moment the operator checks a server,
 * ``servers`` goes non-empty, the backend clause switches on, and pruning is
 * then the correct reading of both.
 */
export const toolInScope = (
  server: string,
  tool: string,
  servers: string[],
  allowTools: string[],
): boolean =>
  servers.includes(server) &&
  (allowTools.length === 0 || allowTools.includes(tool));

export const bindingInScope = (
  binding: ArgBindingFields,
  servers: string[],
  allowTools: string[],
): boolean => toolInScope(binding.server, binding.tool, servers, allowTools);
