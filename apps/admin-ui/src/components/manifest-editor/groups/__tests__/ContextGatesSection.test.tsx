import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../../../i18n";

import { ContextGatesSection } from "../ContextGatesSection";
import type { AgentManifest } from "../../form_model";

// The three tabbed PolicySpec sub-blocks (config-page redesign v2 Task 3) —
// mirrors ``ContextGatesFields`` in form_model.ts. tool_output_budget (the
// fourth sub-block) is NOT tabbed — it's a single master-switch row that
// lives outside the Tabs, at the top of the section.
const PRUNE_FIELD_IDS = [
  "policies.tool_result_prune.enabled",
  "policies.tool_result_prune.threshold_pct",
  "policies.tool_result_prune.recent_tool_results_kept",
];
const WINDOW_FIELD_IDS = [
  "policies.working_memory.enabled",
  "policies.working_memory.threshold_pct",
  "policies.working_memory.max_recent_turns",
  "policies.working_memory.keep_first_turn",
];
const COMPRESS_FIELD_IDS = [
  "policies.context_compression.enabled",
  "policies.context_compression.threshold_pct",
  "policies.context_compression.head_keep",
  "policies.context_compression.tail_keep",
  "policies.context_compression.flush_before_compaction",
  "policies.context_compression.max_passes",
  "policies.context_compression.pressure_feedback",
  "policies.context_compression.pressure_warn_pct",
];
const BUDGET_FIELD_ID = "policies.tool_output_budget.enabled";

// Mirrors the (en locale) ``context_gates.panel_*`` values — the step
// sequence is the whole point of this section, so the labels (and the
// tabs' left-to-right order) must carry it (#4).
const TAB_LABELS = {
  prune: "Step 1 · Tool-result prune",
  window: "Step 2 · Sliding window",
  compress: "Step 3 · Context compression",
};

function renderSection(
  formData: AgentManifest = {},
  onChange: (d: unknown) => void = vi.fn(),
) {
  return render(<ContextGatesSection formData={formData} onChange={onChange} />);
}

function rowFor(fieldId: string): HTMLElement | null {
  return document.querySelector(`[data-field-id="${fieldId}"]`);
}

async function openTab(
  user: ReturnType<typeof userEvent.setup>,
  label: string,
): Promise<void> {
  await user.click(screen.getByRole("tab", { name: label }));
}

describe("ContextGatesSection", () => {
  it("renders with no Collapse anywhere", () => {
    renderSection();
    expect(document.querySelector(".ant-collapse")).not.toBeInTheDocument();
  });

  it("renders a one-line intro above everything else", () => {
    renderSection();
    expect(
      screen.getByText(/three steps below in order/i),
    ).toBeVisible();
  });

  it("each tab opens with its own step-intro first line (#4)", async () => {
    const user = userEvent.setup();
    renderSection();
    expect(screen.getByText(/^Step 1:/)).toBeVisible();
    await openTab(user, TAB_LABELS.window);
    expect(screen.getByText(/^Step 2:/)).toBeVisible();
    await openTab(user, TAB_LABELS.compress);
    expect(screen.getByText(/^Step 3:/)).toBeVisible();
  });

  it("renders the tool-output-budget master switch outside the Tabs, at the very top", () => {
    renderSection();
    const row = rowFor(BUDGET_FIELD_ID);
    expect(row).toBeInTheDocument();
    expect(row).toBeVisible();
    expect(row?.closest(".ant-tabs")).toBeNull();
  });

  it("renders three step-labelled tabs in order, prune (step 1) active by default", () => {
    renderSection();
    const tabs = screen.getAllByRole("tab").map((el) => el.textContent);
    expect(tabs).toEqual([
      TAB_LABELS.prune,
      TAB_LABELS.window,
      TAB_LABELS.compress,
    ]);
    expect(
      screen.getByRole("tab", { name: TAB_LABELS.prune }),
    ).toHaveAttribute("aria-selected", "true");
  });

  it("shows the prune tab's 3 fields by default, with no click needed", () => {
    renderSection();
    for (const id of PRUNE_FIELD_IDS) {
      expect(rowFor(id)).toBeVisible();
    }
  });

  it("switching to the sliding-window (step 2) tab shows its 4 fields", async () => {
    const user = userEvent.setup();
    renderSection();
    await openTab(user, TAB_LABELS.window);
    for (const id of WINDOW_FIELD_IDS) {
      expect(rowFor(id)).toBeVisible();
    }
  });

  it("switching to the context-compression (step 3) tab shows its 10 fields", async () => {
    const user = userEvent.setup();
    renderSection();
    await openTab(user, TAB_LABELS.compress);
    for (const id of COMPRESS_FIELD_IDS) {
      expect(rowFor(id)).toBeVisible();
    }
  });

  it("changing the tool-result-prune threshold writes policies.tool_result_prune.threshold_pct", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection({}, onChange);

    const row = rowFor("policies.tool_result_prune.threshold_pct") as HTMLElement;
    const input = within(row).getByRole("spinbutton");
    await user.clear(input);
    await user.type(input, "0.4");

    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.policies?.tool_result_prune?.threshold_pct).toBe(0.4);
  });

  it("switching tool_result_prune.enabled off writes false", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection({}, onChange);

    const row = rowFor("policies.tool_result_prune.enabled") as HTMLElement;
    await user.click(within(row).getByRole("switch"));

    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.policies?.tool_result_prune?.enabled).toBe(false);
  });

  it("switching tool_result_prune.enabled back to true (=default) deletes the key", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const seed: AgentManifest = {
      spec: { policies: { tool_result_prune: { enabled: false } } },
    };
    renderSection(seed, onChange);

    const row = rowFor("policies.tool_result_prune.enabled") as HTMLElement;
    await user.click(within(row).getByRole("switch"));

    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.policies?.tool_result_prune).toBeUndefined();
  });

  it("changing working_memory.max_recent_turns writes policies.working_memory.max_recent_turns", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection({}, onChange);
    await openTab(user, TAB_LABELS.window);

    const row = rowFor("policies.working_memory.max_recent_turns") as HTMLElement;
    const input = within(row).getByRole("spinbutton");
    await user.clear(input);
    await user.type(input, "12");

    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.policies?.working_memory?.max_recent_turns).toBe(12);
  });

  it("changing context_compression.head_keep writes policies.context_compression.head_keep", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection({}, onChange);
    await openTab(user, TAB_LABELS.compress);

    const row = rowFor("policies.context_compression.head_keep") as HTMLElement;
    const input = within(row).getByRole("spinbutton");
    await user.clear(input);
    await user.type(input, "2");

    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.policies?.context_compression?.head_keep).toBe(2);
  });

  it("legacy hard-truncation knobs (max_turns / max_tokens) are NOT rendered — YAML-only (#13)", async () => {
    const user = userEvent.setup();
    renderSection({}, vi.fn());
    await openTab(user, TAB_LABELS.compress);

    expect(rowFor("policies.context_compression.max_turns")).toBeNull();
    expect(rowFor("policies.context_compression.max_tokens")).toBeNull();
  });

  it("switching tool_output_budget.enabled off writes policies.tool_output_budget.enabled = false", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection({}, onChange);

    const row = rowFor(BUDGET_FIELD_ID) as HTMLElement;
    await user.click(within(row).getByRole("switch"));

    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.policies?.tool_output_budget?.enabled).toBe(false);
  });

  it("patching one sub-block preserves sibling sub-blocks (including ones on other tabs)", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const seed: AgentManifest = {
      spec: {
        policies: {
          working_memory: { max_recent_turns: 30 },
          tool_output_budget: { enabled: false },
        },
      },
    };
    renderSection(seed, onChange);

    const row = rowFor("policies.tool_result_prune.threshold_pct") as HTMLElement;
    const input = within(row).getByRole("spinbutton");
    await user.clear(input);
    await user.type(input, "0.5");

    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.policies?.tool_result_prune?.threshold_pct).toBe(0.5);
    expect(last.spec?.policies?.working_memory?.max_recent_turns).toBe(30);
    expect(last.spec?.policies?.tool_output_budget?.enabled).toBe(false);
  });

  it("shows no '已自定义' tag for tool_result_prune.threshold_pct when unset (at default)", () => {
    renderSection({});
    expect(
      screen.queryByTestId(
        "field-customized-policies.tool_result_prune.threshold_pct",
      ),
    ).not.toBeInTheDocument();
  });

  it("shows the '已自定义' tag and a reset button for tool_result_prune.threshold_pct once diverged", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const seed: AgentManifest = {
      spec: { policies: { tool_result_prune: { threshold_pct: 0.4 } } },
    };
    renderSection(seed, onChange);

    expect(
      screen.getByTestId(
        "field-customized-policies.tool_result_prune.threshold_pct",
      ),
    ).toBeInTheDocument();

    await user.click(
      screen.getByTestId(
        "field-reset-policies.tool_result_prune.threshold_pct",
      ),
    );

    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.policies?.tool_result_prune?.threshold_pct).toBeUndefined();
  });
});
