import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../../i18n";

import * as catalog from "../catalog";
import { BASE_MANIFEST_YAML } from "../defaults";
import { FormView, type FormSection } from "../FormView";
import type { AgentManifest } from "../form_model";
import { parseYaml } from "../yaml";

// The MCP tab mounts McpToolPicker, which loads servers on mount.
vi.mock("../../../api/mcp-servers", () => ({
  listAvailableMcpServers: vi.fn().mockResolvedValue([]),
  listMcpServerTools: vi.fn().mockResolvedValue([]),
}));
vi.mock("../../../api/mcp-catalog", () => ({
  listPlatformCatalog: vi.fn().mockResolvedValue([]),
  listCatalogTools: vi.fn().mockResolvedValue({ status: "ok", tools: [] }),
}));

vi.spyOn(catalog, "loadModelCatalog").mockResolvedValue({
  providers: [
    {
      provider: "openai",
      models: [
        {
          name: "gpt-4o",
          vision: true,
          embeddings: false,
          context_window: 128000,
          deprecated: false,
        },
      ],
    },
  ],
});

const SEED: AgentManifest = {
  apiVersion: "expert_work/v1",
  kind: "Agent",
  metadata: { name: "bot" },
  spec: {
    model: { provider: "openai", name: "gpt-4o" },
    system_prompt: { template: "hi" },
    memory: {
      long_term: {
        retrieve_top_k: 5,
        write_back: true,
        recall_mode: "per_session",
      },
    },
    sandbox: { kind: "none" },
  },
};

function renderSection(
  section: FormSection,
  formData: AgentManifest = SEED,
  onChange: (d: unknown) => void = vi.fn(),
) {
  return render(
    <FormView formData={formData} onChange={onChange} section={section} />,
  );
}

describe("FormView", () => {
  it("renders each section's testids under its tab", () => {
    renderSection("basic");
    expect(screen.getByTestId("af-basic")).toBeInTheDocument();

    renderSection("model");
    expect(screen.getByTestId("af-model")).toBeInTheDocument();
    expect(screen.getByTestId("af-reflection-evaluator")).toBeInTheDocument();
    // E.11 — the fallback-chain section shows once a primary model is picked.
    expect(screen.getByTestId("af-fallback")).toBeInTheDocument();

    renderSection("prompt");
    expect(screen.getByTestId("af-prompt")).toBeInTheDocument();
    // RT-1 (RT-ADR-4) — the structured-output status block rides the tab.
    expect(screen.getByTestId("af-output-schema")).toBeInTheDocument();

    renderSection("tools");
    expect(screen.getByTestId("af-tools")).toBeInTheDocument();
    // MCP is its own section now, not under tools — and there's no longer a
    // separate "MCP 工具" enable checkbox (selecting a server enables MCP).
    expect(screen.queryByTestId("af-tool-mcp")).not.toBeInTheDocument();

    renderSection("mcp");
    expect(screen.getByTestId("af-mcp")).toBeInTheDocument();
    expect(screen.queryByTestId("af-tool-mcp")).not.toBeInTheDocument();

    renderSection("knowledge");
    expect(screen.getByTestId("af-knowledge")).toBeInTheDocument();

    renderSection("skills");
    expect(screen.getByTestId("af-skills")).toBeInTheDocument();

    renderSection("subagents");
    expect(screen.getByTestId("af-subagents")).toBeInTheDocument();
  });

  it("keeps the section's own heading on the singular section= path", () => {
    renderSection("model");
    // ``manifest_editor.tab_model`` and ``agent_form.section_model`` both
    // translate to "Model" — on the singular path there's no data-section-id
    // subtitle at all, so this is the only place "Model" can come from.
    expect(screen.getByText("Model")).toBeInTheDocument();
    expect(document.querySelector("[data-section-id]")).not.toBeInTheDocument();
  });

  it("suppresses only the section's own duplicate heading in the stacked path", () => {
    render(
      <FormView formData={SEED} onChange={vi.fn()} sections={["model"]} />,
    );
    // Before the fix this rendered twice: the data-section-id subtitle
    // (manifest_editor.tab_model) and the "model" section's own <h3>
    // (agent_form.section_model) — both translate to "Model".
    expect(screen.getAllByText("Model")).toHaveLength(1);
    expect(
      document.querySelector('[data-section-id="model"]'),
    ).toBeInTheDocument();
    // Sub-parts bundled into the same "model" tab keep their OWN
    // (non-duplicate) labels — suppressing those would remove the only
    // label distinguishing them from one another. (Fallback is a nested
    // sub-area of af-model with a small label, not a sibling heading.)
    expect(screen.getByTestId("af-fallback")).toHaveTextContent(
      "Fallback models",
    );
    expect(screen.getByTestId("af-reflection-evaluator")).toHaveTextContent(
      "Reflection evaluator (optional)",
    );
  });

  it("folds the fallback area into the model block with model-wording copy (no empty-state illustration)", () => {
    renderSection("model");
    const model = screen.getByTestId("af-model");
    // Nested inside the main-model block, not a sibling section.
    expect(within(model).getByTestId("af-fallback")).toBeInTheDocument();
    // Empty chain = one-line hint + inline add button — no big Empty block.
    expect(document.querySelector(".ant-empty")).not.toBeInTheDocument();
    expect(screen.getByTestId("af-fallback")).toHaveTextContent(
      "No fallback models yet.",
    );
    expect(screen.getByTestId("af-fallback-add")).toHaveTextContent(
      "Add fallback model",
    );
  });

  it("hides the fallback section until a primary model is picked", () => {
    const noPrimary: AgentManifest = {
      ...SEED,
      spec: { ...SEED.spec, model: {} },
    };
    renderSection("model", noPrimary);
    expect(screen.queryByTestId("af-fallback")).not.toBeInTheDocument();
  });

  it("adding a fallback provider writes spec.model.fallback", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection("model", SEED, onChange);
    await user.click(screen.getByTestId("af-fallback-add"));
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.model?.fallback).toHaveLength(1);
    // The primary model is untouched by adding a fallback.
    expect(last.spec?.model?.provider).toBe("openai");
  });

  it("removing the last fallback entry drops the chain key", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const withFb: AgentManifest = {
      ...SEED,
      spec: {
        ...SEED.spec,
        model: {
          provider: "openai",
          name: "gpt-4o",
          fallback: [{ provider: "openai", name: "gpt-4o" }],
        },
      },
    };
    renderSection("model", withFb, onChange);
    await user.click(screen.getByTestId("af-fallback-remove-0"));
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.model?.fallback).toBeUndefined();
  });

  it("shows the vision (VL fallback) section when the main model is text-only", () => {
    // SEED's model has no supports_vision → Path B section appears.
    renderSection("model");
    expect(screen.getByTestId("af-vision")).toBeInTheDocument();
  });

  it("hides the vision section when the main model is vision-capable", () => {
    const visionModel: AgentManifest = {
      ...SEED,
      spec: {
        ...SEED.spec,
        model: { provider: "openai", name: "gpt-4o", supports_vision: true },
      },
    };
    renderSection("model", visionModel);
    expect(screen.queryByTestId("af-vision")).not.toBeInTheDocument();
  });

  it("hides the evaluator clear button until an independent evaluator is set", () => {
    renderSection("model");
    expect(
      screen.queryByTestId("af-reflection-evaluator-clear"),
    ).not.toBeInTheDocument();
  });

  it("shows the clear button and removes the routing rule when cleared", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const withEvaluator: AgentManifest = {
      ...SEED,
      spec: {
        ...SEED.spec,
        routing: {
          rules: [
            {
              when: "reflection",
              model: { provider: "openai", name: "gpt-4o" },
            },
          ],
        },
      },
    };
    renderSection("model", withEvaluator, onChange);
    const clear = screen.getByTestId("af-reflection-evaluator-clear");
    expect(clear).toBeInTheDocument();
    await user.click(clear);
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.routing).toBeUndefined();
  });

  it("typing the name emits a merged manifest preserving non-curated fields", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection("basic", SEED, onChange);
    const input = screen
      .getByTestId("af-name")
      .querySelector("input") as HTMLInputElement;
    await user.type(input, "X");
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.metadata?.name).toBe("botX");
    expect(last.apiVersion).toBe("expert_work/v1");
    expect(last.spec?.sandbox).toEqual({ kind: "none" });
  });

  it("checking web search adds a builtin web_search tool entry", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection("tools", SEED, onChange);
    await user.click(screen.getByTestId("af-tool-web_search"));
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.tools).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ type: "builtin", name: "web_search" }),
      ]),
    );
  });

  // Task 1 — builtin tool toggles (exec_python/bash default-on + opt-in-7).
  it("renders the exec_python/bash/opt-in builtin tool toggles", () => {
    renderSection("tools");
    expect(screen.getByTestId("af-tool-exec_python")).toBeInTheDocument();
    expect(screen.getByTestId("af-tool-bash")).toBeInTheDocument();
    expect(screen.getByTestId("af-tool-manage_task")).toBeInTheDocument();
    expect(screen.getByTestId("af-tool-author_skill")).toBeInTheDocument();
    expect(screen.getByTestId("af-tool-refine_skill")).toBeInTheDocument();
    expect(screen.getByTestId("af-tool-fork_skill")).toBeInTheDocument();
    expect(
      screen.getByTestId("af-tool-propose_skill_to_tenant"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("af-tool-note_behavior_patch"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("af-tool-clarify_tool_usage"),
    ).toBeInTheDocument();
  });

  it("checking manage_task (opt-in) adds a builtin manage_task tool entry", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection("tools", SEED, onChange);
    await user.click(screen.getByTestId("af-tool-manage_task"));
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.tools).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ type: "builtin", name: "manage_task" }),
      ]),
    );
  });

  it("new-agent default seed: every toggleable tool (incl. web_search/http/opt-in-7) checked on", () => {
    const seeded = parseYaml(BASE_MANIFEST_YAML) as AgentManifest;
    renderSection("tools", seeded);
    expect(screen.getByTestId("af-tool-web_search")).toBeChecked();
    expect(screen.getByTestId("af-tool-http")).toBeChecked();
    expect(screen.getByTestId("af-tool-exec_python")).toBeChecked();
    expect(screen.getByTestId("af-tool-bash")).toBeChecked();
    expect(screen.getByTestId("af-tool-manage_task")).toBeChecked();
    expect(screen.getByTestId("af-tool-author_skill")).toBeChecked();
    expect(screen.getByTestId("af-tool-refine_skill")).toBeChecked();
    expect(screen.getByTestId("af-tool-fork_skill")).toBeChecked();
    expect(
      screen.getByTestId("af-tool-propose_skill_to_tenant"),
    ).toBeChecked();
    expect(screen.getByTestId("af-tool-note_behavior_patch")).toBeChecked();
    expect(screen.getByTestId("af-tool-clarify_tool_usage")).toBeChecked();
  });

  it("editing the prompt updates spec.system_prompt.template", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection("prompt", SEED, onChange);
    const ta = screen
      .getByTestId("af-prompt-input")
      .querySelector("textarea") as HTMLTextAreaElement;
    await user.type(ta, "!");
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.system_prompt?.template).toBe("hi!");
  });

  it("loads the model catalog", async () => {
    renderSection("model");
    await waitFor(() => expect(catalog.loadModelCatalog).toHaveBeenCalled());
  });

  // RT-1 (RT-ADR-4) / config-page redesign v2 Task 7 — the structured-output
  // block is a flat field-list editor (OutputSchemaEditor) riding the prompt
  // tab; its own test suite (widgets/__tests__/OutputSchemaEditor.test.tsx)
  // covers the editor's behavior in depth — these two just confirm it's
  // wired to the right formData/onChange here.
  it("shows the output-schema switch off by default", () => {
    renderSection("prompt");
    const block = screen.getByTestId("af-output-schema");
    expect(within(block).getByTestId("af-output-schema-switch")).not.toBeChecked();
  });

  it("shows the configured schema's fields when output_schema is set", () => {
    const seeded: AgentManifest = {
      ...SEED,
      spec: {
        ...SEED.spec,
        output_schema: {
          name: "review_verdict",
          json_schema: {
            type: "object",
            properties: { verdict: { type: "string" } },
            additionalProperties: false,
          },
        },
      },
    };
    renderSection("prompt", seeded);
    const block = screen.getByTestId("af-output-schema");
    expect(within(block).getByTestId("af-output-schema-switch")).toBeChecked();
    expect(within(block).getByTestId("af-output-schema-name-0")).toHaveValue(
      "verdict",
    );
  });

  // inject_current_date (DynamicContextSpec) — default-on switch at the tail
  // of the prompt tab, plus a static note about custom_reminders being
  // YAML-only.
  it("the inject-current-date switch defaults to on (value absent)", () => {
    renderSection("prompt");
    expect(screen.getByTestId("af-inject-current-date")).toBeChecked();
  });

  it("toggling inject-current-date off writes spec.dynamic_context.inject_current_date=false", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection("prompt", SEED, onChange);
    await user.click(screen.getByTestId("af-inject-current-date"));
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.dynamic_context?.inject_current_date).toBe(false);
  });

  it("renders the dynamic-context note about custom_reminders", () => {
    renderSection("prompt");
    expect(screen.getByTestId("af-dynamic-context-note")).toBeInTheDocument();
  });

  it("renders the basic-section note about extends and tenant_config reserved fields", () => {
    renderSection("basic");
    expect(screen.getByTestId("af-basic-yaml-note")).toBeInTheDocument();
  });

  it("renders the tools-section note about per-tool YAML config", () => {
    renderSection("tools");
    expect(screen.getByTestId("af-tools-config-note")).toBeInTheDocument();
  });
});
