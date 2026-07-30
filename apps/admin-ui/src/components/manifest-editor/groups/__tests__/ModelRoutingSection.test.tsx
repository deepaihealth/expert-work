import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../../../i18n";

import * as catalog from "../../catalog";
import { ModelRoutingSection } from "../ModelRoutingSection";
import type { AgentManifest } from "../../form_model";

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

// A primary model already picked so the embedded FormView's "model" section
// also renders its af-fallback sub-section (E.11) — spot-checked alongside
// af-model below.
const SEED: AgentManifest = {
  spec: { model: { provider: "openai", name: "gpt-4o" } },
};

// Reflection on (a declared, empty ``reflection`` block) — required for the
// two tuning FieldRows to render at all (see the render-guard tests below
// for the off cases).
const REFLECTION_ON_SEED: AgentManifest = {
  ...SEED,
  spec: { ...SEED.spec, reflection: {} },
};

function renderSection(
  formData: AgentManifest = SEED,
  onChange: (d: unknown) => void = vi.fn(),
) {
  return render(
    <ModelRoutingSection formData={formData} onChange={onChange} />,
  );
}

function rowFor(fieldId: string): HTMLElement | null {
  return document.querySelector(`[data-field-id="${fieldId}"]`);
}

describe("ModelRoutingSection", () => {
  it("embeds the existing 'model' FormView section (model-select-provider always visible, af-fallback once a primary model is picked)", async () => {
    renderSection();
    expect(screen.getByTestId("af-model")).toBeInTheDocument();
    // FormView's "model" section renders one ModelSelect per sub-section
    // (main model / reflection evaluator / vision fallback) sharing the same
    // internal testids, so scope the query to af-model's own instance.
    expect(
      within(screen.getByTestId("af-model")).getByTestId(
        "model-select-provider",
      ),
    ).toBeInTheDocument();
    expect(screen.getByTestId("af-fallback")).toBeInTheDocument();
  });

  it("renders the reflection section as a plain heading + always-visible switch (no collapse)", () => {
    renderSection();
    // The heading and the FieldRow's own label both read "Reflection
    // self-assessment" in English — both present (no collapse hiding the
    // FieldRow) means exactly two matches.
    expect(screen.getAllByText("Reflection self-assessment")).toHaveLength(2);
    // The reflection row isn't nested inside a collapse panel (unlike the
    // embedded FormView's own unrelated "Advanced" ModelSelect collapse) —
    // the switch is directly clickable, no panel header to open first.
    expect(rowFor("reflection")?.closest(".ant-collapse")).toBeNull();
    expect(
      within(rowFor("reflection") as HTMLElement).getByRole("switch"),
    ).toBeVisible();
  });

  it("does not render the tuning FieldRows while reflection is off (absent block)", () => {
    renderSection(SEED);

    expect(rowFor("reflection")).toBeInTheDocument();
    expect(rowFor("reflection.budget")).not.toBeInTheDocument();
    expect(rowFor("reflection.deadline_s")).not.toBeInTheDocument();
  });

  it("does not render the tuning FieldRows while reflection is off (explicit reflection: null)", () => {
    const offSeed: AgentManifest = {
      ...SEED,
      spec: { ...SEED.spec, reflection: null },
    };
    renderSection(offSeed);

    expect(rowFor("reflection")).toBeInTheDocument();
    expect(rowFor("reflection.budget")).not.toBeInTheDocument();
    expect(rowFor("reflection.deadline_s")).not.toBeInTheDocument();
  });

  it("turning the reflection switch on writes spec.reflection = {} and reveals the tuning FieldRows", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const { rerender } = renderSection(SEED, onChange);

    const switchEl = within(rowFor("reflection") as HTMLElement).getByRole(
      "switch",
    );
    await user.click(switchEl);

    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.reflection).toEqual({});

    rerender(<ModelRoutingSection formData={last} onChange={onChange} />);
    expect(rowFor("reflection.budget")).toBeInTheDocument();
    expect(rowFor("reflection.deadline_s")).toBeInTheDocument();
  });

  it("editing the reflection budget to 5 writes reflection.budget = 5", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection(REFLECTION_ON_SEED, onChange);

    const input = within(
      rowFor("reflection.budget") as HTMLElement,
    ).getByRole("spinbutton");
    await user.clear(input);
    await user.type(input, "5");

    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.reflection?.budget).toBe(5);
  });

  it("turning the reflection switch off deletes spec.reflection and hides the tuning FieldRows", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const { rerender } = renderSection(REFLECTION_ON_SEED, onChange);
    expect(rowFor("reflection.budget")).toBeInTheDocument();

    const switchEl = within(rowFor("reflection") as HTMLElement).getByRole(
      "switch",
    );
    await user.click(switchEl);

    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.reflection).toBeUndefined();

    rerender(<ModelRoutingSection formData={last} onChange={onChange} />);
    expect(rowFor("reflection.budget")).not.toBeInTheDocument();
    expect(rowFor("reflection.deadline_s")).not.toBeInTheDocument();
  });

  // Reflection-evaluator model picker — moved into the reflection panel
  // (UX feedback merge): rendered only while reflection is on, same guard
  // as the tuning FieldRows.
  it("does not render the reflection-evaluator picker while reflection is off", () => {
    renderSection(SEED);
    expect(
      screen.queryByTestId("af-reflection-evaluator"),
    ).not.toBeInTheDocument();
  });

  it("renders the reflection-evaluator picker once reflection is on (no clear link while unset)", () => {
    renderSection(REFLECTION_ON_SEED);
    expect(screen.getByTestId("af-reflection-evaluator")).toBeInTheDocument();
    // Empty by default — reflection reuses the agent's own model, no clear link.
    expect(
      screen.queryByTestId("af-reflection-evaluator-clear"),
    ).not.toBeInTheDocument();
  });

  it("shows the clear link and removes the routing rule when cleared", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const withEvaluator: AgentManifest = {
      ...REFLECTION_ON_SEED,
      spec: {
        ...REFLECTION_ON_SEED.spec,
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
    renderSection(withEvaluator, onChange);
    const clear = screen.getByTestId("af-reflection-evaluator-clear");
    expect(clear).toBeInTheDocument();
    await user.click(clear);
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.routing).toBeUndefined();
  });

  it("renders the closing YAML-guidance note", () => {
    renderSection();
    expect(screen.getByTestId("model-yaml-note")).toHaveTextContent(
      "routing.rules",
    );
    expect(screen.getByTestId("model-yaml-note")).toHaveTextContent(
      "api_key_ref is deprecated",
    );
  });
});
