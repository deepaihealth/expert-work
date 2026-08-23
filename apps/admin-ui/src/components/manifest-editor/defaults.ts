/**
 * Default manifest template + capability-adaptive seeding (Mini-ADR S-5).
 *
 * ``BASE_MANIFEST_YAML`` is the blank-canvas manifest. ``buildDefaultManifest``
 * pre-selects the first *configured* provider's first chat (non-embedding)
 * model and copies its vision capability, so a new agent starts on a model the
 * platform can actually build. Long-term memory is ON by default
 * (Stream T): a memory-less agent has little product value, so new agents seed
 * with ``spec.memory.long_term``. This requires a platform embedding config —
 * CreateAgentModal blocks+guides when none is set (the build-time embedder
 * gate is the backstop).
 *
 * config-page redesign v2 Task 5 — ``tools`` seeds a "default all-on"
 * profile: the 8 hidden essentials + exec_python/bash (already the case) PLUS
 * web_search/http and every opt-in-7 builtin (manage_task/author_skill/
 * refine_skill/fork_skill/propose_skill_to_tenant/note_behavior_patch/
 * clarify_tool_usage), 19 tools total. ``ask_for_approval`` is deliberately
 * NOT seeded (default OFF since 2026-08-23): in a live conversation the agent
 * should ask in chat; the tool is for unattended runs / audited sign-offs and
 * has its own FormView toggle. This only changes what a brand-new
 * template starts with — the ``tools`` FormView checkboxes turn any of them
 * back off, and an EXISTING agent's manifest is never touched by this file.
 * The web_search/http entry shapes exactly mirror what ``form_model.ts``'s
 * ``setTool`` writes when a user checks the box (``{type: builtin, name:
 * web_search, config: {}}`` / ``{type: http}``) so toggling either off then
 * back on round-trips byte-identical; the opt-in-7 entries mirror
 * ``setBuiltinTool``'s plain ``{type: builtin, name}`` shape the same way.
 * ``policies.max_no_progress: 4`` is also seeded here (T6 depends on this
 * default existing on new agents).
 */
import { parseYaml } from "./yaml";
import type { CatalogModel, ModelCatalog } from "../../api/model_catalog";

export const BASE_MANIFEST_YAML = `apiVersion: expert_work.io/v1
kind: Agent
metadata:
  name: my-agent
  version: "1.0.0"
  tenant: my-tenant
spec:
  tenant_config: {}
  model:
    provider: anthropic
    name: claude-sonnet-4-6
  system_prompt:
    template: "You are a helpful assistant."
  memory:
    long_term:
      retrieve_top_k: 5
      write_back: true
      recall_mode: per_session
  tools:
    - { type: builtin, name: read_file }
    - { type: builtin, name: write_file }
    - { type: builtin, name: edit_file }
    - { type: builtin, name: list_dir }
    - { type: builtin, name: read_document }
    - { type: builtin, name: save_artifact }
    - { type: builtin, name: list_artifacts }
    - { type: builtin, name: remember }
    - { type: builtin, name: exec_python }
    - { type: builtin, name: bash }
    - { type: builtin, name: web_search, config: {} }
    - { type: http }
    - { type: builtin, name: manage_task }
    - { type: builtin, name: author_skill }
    - { type: builtin, name: refine_skill }
    - { type: builtin, name: fork_skill }
    - { type: builtin, name: propose_skill_to_tenant }
    - { type: builtin, name: note_behavior_patch }
    - { type: builtin, name: clarify_tool_usage }
  policies:
    max_no_progress: 4
  sandbox:
    resources: { cpu: "1.0", memory: "1Gi" }
    network:
      egress: proxy
      allowlist: []  # empty = allow all public hosts (SSRF blocked, audited)
      denylist: []   # block these hosts even under allow-all (takes precedence)
    filesystem:
      readonly_root: true
      writable: ["/workspace"]
`;

interface FirstChat {
  provider: string;
  model: CatalogModel;
}

function firstChatModel(catalog: ModelCatalog): FirstChat | null {
  for (const p of catalog.providers) {
    const chat = p.models.find((m) => !m.embeddings && !m.deprecated);
    if (chat) return { provider: p.provider, model: chat };
  }
  return null;
}

export function buildDefaultManifest(catalog: ModelCatalog): unknown {
  const base = parseYaml(BASE_MANIFEST_YAML) as Record<string, unknown>;
  const pick = firstChatModel(catalog);
  if (!pick) return base;
  const spec = base.spec as Record<string, unknown>;
  return {
    ...base,
    spec: {
      ...spec,
      model: {
        provider: pick.provider,
        name: pick.model.name,
        supports_vision: pick.model.vision,
      },
    },
  };
}
