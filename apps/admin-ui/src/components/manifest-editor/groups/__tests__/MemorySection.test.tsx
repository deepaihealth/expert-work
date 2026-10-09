import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../../../i18n";

import * as catalog from "../../catalog";
import { MemorySection } from "../MemorySection";
import type { AgentManifest } from "../../form_model";

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
