import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../../../i18n";

import * as catalog from "../../catalog";
import { MemorySection } from "../MemorySection";
import type { AgentManifest } from "../../form_model";
import type { ModelCatalog } from "../../../../api/model_catalog";

vi.spyOn(catalog, "loadModelCatalog").mockResolvedValue({ providers: [] });

// Memory on (a declared, possibly-empty ``long_term`` block) / off (an
// explicit ``null``) — same two fixtures the old FormView-embedding tests
// used, still the two states the whole section forks on.
const ON_SEED: AgentManifest = { spec: { memory: { long_term: {} } } };
const OFF_SEED: AgentManifest = { spec: { memory: { long_term: null } } };

const RETRIEVAL_FIELD_IDS = [
  "memory.long_term.verify_reads",
  "memory.long_term.write_min_importance",
  "memory.long_term.reconcile_writes",
  "memory.long_term.recall_mode",
  "memory.long_term.rewrite_reads",
  "memory.long_term.abstain_threshold",
];

function renderSection(
  formData: AgentManifest = ON_SEED,
  onChange: (d: unknown) => void = vi.fn(),
) {
  return render(<MemorySection formData={formData} onChange={onChange} />);
}

function rowFor(fieldId: string): HTMLElement | null {
  return document.querySelector(`[data-field-id="${fieldId}"]`);
}

// The retrieval tab's rows are mounted (``forceRender``) but "basic" is the
// default active tab, so retrieval's pane is ``aria-hidden`` until its nav
// tab is clicked — matches how a real user would reach it (mirrors this
// suite's own ``openPanel``-style helpers on other group sections).
async function openRetrievalTab(
  user: ReturnType<typeof userEvent.setup>,
): Promise<void> {
  await user.click(screen.getByRole("tab", { name: "Retrieval details" }));
}

describe("MemorySection", () => {
  it("renders three sub-tabs (basic/retrieval/budget), all mounted regardless of the active one", () => {
    renderSection();
    expect(screen.getByTestId("memory-tab-basic")).toBeInTheDocument();
    expect(screen.getByTestId("memory-tab-retrieval")).toBeInTheDocument();
    expect(screen.getByTestId("memory-tab-budget")).toBeInTheDocument();
  });

  it("disables the retrieval tab while memory is off", () => {
    const { container } = renderSection(OFF_SEED);
    expect(
      container.querySelector(".ant-tabs-tab-disabled"),
    ).toBeInTheDocument();
  });

  it("does not disable any tab while memory is on", () => {
    const { container } = renderSection(ON_SEED);
    expect(
      container.querySelector(".ant-tabs-tab-disabled"),
    ).not.toBeInTheDocument();
  });

  it("renders all 6 retrieval FieldRows once memory is on", () => {
    renderSection(ON_SEED);
    for (const id of RETRIEVAL_FIELD_IDS) {
      expect(rowFor(id)).toBeInTheDocument();
    }
  });

  it("does not render the top_k/write_back FieldRows while memory is off (they'd silently reactivate it)", () => {
    renderSection(OFF_SEED);
    expect(rowFor("memory.long_term.retrieve_top_k")).not.toBeInTheDocument();
    expect(rowFor("memory.long_term.write_back")).not.toBeInTheDocument();
  });

  it("budget tab: no injection FieldRows while memory is off, consolidation FieldRow still there", () => {
    renderSection(OFF_SEED);
    expect(
      rowFor("memory.long_term.injection_token_budget"),
    ).not.toBeInTheDocument();
    expect(
      rowFor("memory.long_term.correction_token_budget"),
    ).not.toBeInTheDocument();
    expect(
      rowFor("policies.memory_consolidation.enabled"),
    ).toBeInTheDocument();
  });

  it("budget tab: injection FieldRows render once memory is on", () => {
    renderSection(ON_SEED);
    expect(
      rowFor("memory.long_term.injection_token_budget"),
    ).toBeInTheDocument();
    expect(
      rowFor("memory.long_term.correction_token_budget"),
    ).toBeInTheDocument();
  });

  it("no longer renders the deleted reserved-fields note", () => {
    renderSection();
    expect(screen.queryByTestId("memory-reserved-note")).not.toBeInTheDocument();
  });

  it("B-168: memory-model row renders only while memory is on", () => {
    renderSection(OFF_SEED);
    expect(rowFor("memory.model")).toBeNull();
    renderSection(ON_SEED);
    expect(rowFor("memory.model")).not.toBeNull();
  });

  it("B-168: resetting the memory model drops the when=memory rule but keeps others", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const seed: AgentManifest = {
      spec: {
        memory: { long_term: {} },
        routing: {
          rules: [
            { when: "planning", model: { provider: "openai", name: "gpt-4o" } },
            { when: "memory", model: { provider: "glm", name: "glm-5.3-flash" } },
          ],
        },
      },
    };
    renderSection(seed, onChange);
    await user.click(screen.getByTestId("field-reset-memory.model"));
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.routing?.rules).toEqual([
      { when: "planning", model: { provider: "openai", name: "gpt-4o" } },
    ]);
  });

  it("editing top_k to 8 writes spec.memory.long_term.retrieve_top_k", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection(ON_SEED, onChange);
    const input = within(
      rowFor("memory.long_term.retrieve_top_k") as HTMLElement,
    ).getByRole("spinbutton");
    await user.clear(input);
    await user.type(input, "8");

    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.memory?.long_term?.retrieve_top_k).toBe(8);
  });

  it("turning the long-term-memory switch off writes memory.long_term = null", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection(ON_SEED, onChange);
    const switchEl = within(
      rowFor("memory.long_term") as HTMLElement,
    ).getByRole("switch");
    await user.click(switchEl);

    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.memory?.long_term).toBeNull();
  });

  it("turning verify_reads off writes memory.long_term.verify_reads = false", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection(ON_SEED, onChange);
    await openRetrievalTab(user);
    const switchEl = within(
      rowFor("memory.long_term.verify_reads") as HTMLElement,
    ).getByRole("switch");
    await user.click(switchEl);

    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.memory?.long_term?.verify_reads).toBe(false);
  });
});

describe("MemorySection — B-168 memory model picker", () => {
  const MEMORY_CATALOG: ModelCatalog = {
    providers: [
      {
        provider: "glm",
        models: [
          { name: "glm-5.3", vision: false, embeddings: false, context_window: 1000000, deprecated: false,
            thinking: "effort", thinking_default: true },
          { name: "glm-5.3-flash", vision: true, embeddings: false, context_window: 1000000, deprecated: false,
            thinking: "effort", thinking_default: true, always_thinking: true },
        ],
      },
      {
        provider: "deepseek",
        models: [
          { name: "deepseek-v4-pro", vision: false, embeddings: false, context_window: 1000000, deprecated: false,
            thinking: "effort", thinking_default: true },
        ],
      },
    ],
  };
  const seedWith = (spec: Record<string, unknown>): AgentManifest =>
    ({ spec: { memory: { long_term: {} }, ...spec } }) as AgentManifest;
  const withCatalog = (): void => {
    vi.mocked(catalog.loadModelCatalog).mockResolvedValueOnce(MEMORY_CATALOG);
  };
  const modeRadio = (mode: "default" | "custom"): HTMLInputElement =>
    screen.getByRole("radio", {
      name: mode === "default" ? "Platform default (recommended)" : "Choose a model",
    }) as HTMLInputElement;

  it("no rule → platform default is selected, no picker, and the effective model is named (always-thinking floors)", async () => {
    withCatalog();
    renderSection(seedWith({ model: { provider: "glm", name: "glm-5.3" } }));
    expect(modeRadio("default").checked).toBe(true);
    expect(screen.queryByTestId("model-select-field")).toBeNull();
    expect(await screen.findByText("Uses glm-5.3-flash, thinking at its lowest level")).toBeInTheDocument();
  });

  it("a main model outside the cheap-sibling map is used as-is, thinking off", async () => {
    withCatalog();
    renderSection(seedWith({ model: { provider: "deepseek", name: "deepseek-v4-pro" } }));
    expect(await screen.findByText("Uses deepseek-v4-pro, thinking off")).toBeInTheDocument();
  });

  it("no main model yet → a neutral line instead of a model name", () => {
    renderSection(seedWith({}));
    expect(screen.getByTestId("memory-model-effective")).toHaveTextContent(
      "A cheaper model from the main model's vendor, thinking off",
    );
  });

  it("choosing 'choose a model' with no rule shows an empty compact picker and writes nothing yet", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection(seedWith({ model: { provider: "glm", name: "glm-5.3" } }), onChange);
    await user.click(modeRadio("custom"));
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByTestId("model-select-field")).toBeInTheDocument();
    expect(screen.queryByTestId("model-select-temperature")).toBeNull();
    expect(screen.queryByTestId("model-select-advanced")).toBeNull();
    expect(screen.queryByTestId("memory-model-effective")).toBeNull();
  });

  it("an existing rule → 'choose a model' is selected with the thinking note beside the switch", async () => {
    withCatalog();
    renderSection(
      seedWith({ routing: { rules: [{ when: "memory", model: { provider: "glm", name: "glm-5.3-flash" } }] } }),
    );
    expect(modeRadio("custom").checked).toBe(true);
    const thinking = await screen.findByTestId("model-select-thinking");
    expect(
      within(thinking).getByText("Memory calls are short tasks — thinking makes them several times slower and costlier"),
    ).toBeInTheDocument();
  });

  it("switching back to platform default removes the when=memory rule", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection(
      seedWith({
        routing: {
          rules: [
            { when: "planning", model: { provider: "openai", name: "gpt-4o" } },
            { when: "memory", model: { provider: "glm", name: "glm-5.3-flash" } },
          ],
        },
      }),
      onChange,
    );
    await user.click(modeRadio("default"));
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.routing?.rules).toEqual([{ when: "planning", model: { provider: "openai", name: "gpt-4o" } }]);
  });

  it("the memory-model FieldRow top-aligns so the label stays beside the first line of the control", () => {
    renderSection(seedWith({}));
    expect((rowFor("memory.model") as HTMLElement).style.alignItems).toBe("flex-start");
  });
});
