import { describe, expect, it } from "vitest";
import { buildDefaultManifest, BASE_MANIFEST_YAML } from "../defaults";
import { setBuiltinTool, setTool } from "../form_model";
import { parseYaml } from "../yaml";

type Manifest = {
  spec: {
    model: { provider: string; name: string; supports_vision: boolean };
    memory?: {
      long_term?: {
        retrieve_top_k: number;
        write_back: boolean;
        recall_mode: string;
      };
    };
  };
};

describe("buildDefaultManifest", () => {
  it("picks the first configured provider's first chat model and its vision flag", () => {
    const catalog = {
      providers: [
        {
          provider: "openai",
          models: [
            {
              name: "text-embedding-3-large",
              vision: false,
              embeddings: true,
              context_window: null,
              deprecated: false,
            },
            {
              name: "gpt-5.5",
              vision: true,
              embeddings: false,
              context_window: 128000,
              deprecated: false,
            },
          ],
        },
      ],
    };
    const m = buildDefaultManifest(catalog) as Manifest;
    expect(m.spec.model.provider).toBe("openai");
    expect(m.spec.model.name).toBe("gpt-5.5");
    expect(m.spec.model.supports_vision).toBe(true);
  });

  it("falls back to the base template when no provider is configured", () => {
    const m = buildDefaultManifest({ providers: [] }) as Manifest;
    expect(m.spec.model.provider).toBeTruthy();
    expect(m).toHaveProperty("spec.memory.long_term");
    expect(m.spec.memory?.long_term).toMatchObject({
      retrieve_top_k: 5,
      write_back: true,
      recall_mode: "per_session",
    });
  });
});

describe("BASE_MANIFEST_YAML seed tools", () => {
  it("seeds a default-all-on profile: 8 essentials + exec_python/bash + web_search/http + opt-in-7 (19 total; ask_for_approval deliberately absent)", () => {
    const m = parseYaml(BASE_MANIFEST_YAML) as {
      spec: {
        tools: { type: string; name?: string; config?: unknown }[];
        policies: { max_no_progress: number };
      };
    };
    const names = m.spec.tools.map((t) => t.name ?? t.type);
    expect(new Set(names)).toEqual(
      new Set([
        "read_file",
        "write_file",
        "edit_file",
        "list_dir",
        "read_document",
        "save_artifact",
        "list_artifacts",
        "remember",
        "exec_python",
        "bash",
        "web_search",
        "http",
        "manage_task",
        "author_skill",
        "refine_skill",
        "fork_skill",
        "propose_skill_to_tenant",
        "note_behavior_patch",
        "clarify_tool_usage",
      ]),
    );
    expect(m.spec.tools).toHaveLength(19);

    // web_search/http mirror form_model.ts's ``setTool`` write shape exactly
    // — so toggling either off then back on via the form round-trips
    // byte-identical (no spurious YAML diff on an untouched checkbox).
    expect(m.spec.tools).toContainEqual({
      type: "builtin",
      name: "web_search",
      config: {},
    });
    expect(m.spec.tools).toContainEqual({ type: "http" });
  });

  it("seeds policies.max_no_progress = 4 (T6 dependency)", () => {
    const m = parseYaml(BASE_MANIFEST_YAML) as {
      spec: { policies: { max_no_progress: number } };
    };
    expect(m.spec.policies.max_no_progress).toBe(4);
  });
});

// Linchpin regression (T5 review Important): un-checking ONE tool from the
// full 19-tool seed must drop exactly that entry and leave every sibling —
// including the hidden essentials and their exact shapes — untouched.
// Guards the setTool/setBuiltinTool name-predicate filters against future
// edits (e.g. accidentally filtering by index or by type alone).
describe("19-seed uncheck round-trip", () => {
  type SeededManifest = {
    spec: { tools: { type: string; name?: string; config?: unknown }[] };
  };
  const seed = (): SeededManifest =>
    parseYaml(BASE_MANIFEST_YAML) as SeededManifest;

  it("un-checking web_search drops only that entry; the 18 siblings stay byte-identical", () => {
    const base = seed();
    const next = setTool(base, "webSearch", false) as SeededManifest;
    expect(next.spec.tools).toHaveLength(18);
    const survivors = base.spec.tools.filter(
      (t) => !(t.type === "builtin" && t.name === "web_search"),
    );
    expect(next.spec.tools).toEqual(survivors);
  });

  it("un-checking manage_task (setBuiltinTool) drops only that entry; essential shapes survive", () => {
    const base = seed();
    const next = setBuiltinTool(base, "manage_task", false) as SeededManifest;
    expect(next.spec.tools).toHaveLength(18);
    const survivors = base.spec.tools.filter(
      (t) => !(t.type === "builtin" && t.name === "manage_task"),
    );
    expect(next.spec.tools).toEqual(survivors);
    // the hidden essentials are still there in their exact seeded shape
    expect(next.spec.tools).toContainEqual({
      type: "builtin",
      name: "read_file",
    });
    expect(next.spec.tools).toContainEqual({
      type: "builtin",
      name: "remember",
    });
  });

  it("un-checking http drops only the http entry", () => {
    const base = seed();
    const next = setTool(base, "http", false) as SeededManifest;
    expect(next.spec.tools).toHaveLength(18);
    expect(next.spec.tools).not.toContainEqual({ type: "http" });
    expect(next.spec.tools).toEqual(
      base.spec.tools.filter((t) => t.type !== "http"),
    );
  });

  it("uncheck-then-recheck web_search round-trips back to the full 19-name set", () => {
    const base = seed();
    const next = setTool(
      setTool(base, "webSearch", false),
      "webSearch",
      true,
    ) as SeededManifest;
    expect(next.spec.tools).toHaveLength(19);
    expect(next.spec.tools).toContainEqual({
      type: "builtin",
      name: "web_search",
      config: {},
    });
    expect(new Set(next.spec.tools.map((t) => t.name ?? t.type))).toEqual(
      new Set(base.spec.tools.map((t) => t.name ?? t.type)),
    );
  });
});
