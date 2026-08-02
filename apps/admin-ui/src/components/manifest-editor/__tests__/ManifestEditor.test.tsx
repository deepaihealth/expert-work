import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../../i18n";

vi.mock("@monaco-editor/react", () => {
  const Editor = ({
    value,
    onChange,
  }: {
    value?: string;
    onChange?: (v: string | undefined) => void;
  }) => (
    <textarea
      data-testid="monaco-stub"
      value={value}
      onChange={(e) => onChange?.(e.target.value)}
    />
  );
  return { default: Editor };
});

// The "capabilities" group mounts McpToolPicker, which loads servers on mount.
vi.mock("../../../api/mcp-servers", () => ({
  listAvailableMcpServers: vi.fn().mockResolvedValue([]),
  listMcpServerTools: vi.fn().mockResolvedValue([]),
}));
vi.mock("../../../api/mcp-catalog", () => ({
  listPlatformCatalog: vi.fn().mockResolvedValue([]),
  listCatalogTools: vi.fn().mockResolvedValue({ status: "ok", tools: [] }),
}));

// Cross-tenant W3 — the pickers read the ambient tenant scope; these tests
// don't mount a TenantScopeProvider, so mock it (home state: no scope).
vi.mock("../../../tenant/TenantScopeContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../tenant/TenantScopeContext")>()),
  useTenantScope: () => ({
    scope: "home",
    setScope: () => {},
    apiTenantScope: undefined,
  }),
}));

import * as schemaSdk from "../../../api/manifest_schema";
import { __resetSchemaCacheForTest } from "../schema";
import { ManifestEditor } from "../ManifestEditor";
import en from "../../../i18n/locales/en";

const SCHEMA = {
  type: "object",
  required: ["metadata"],
  properties: {
    metadata: {
      type: "object",
      required: ["name"],
      properties: { name: { type: "string", title: "Name" } },
    },
  },
};

const SEED = "metadata:\n  name: bot\n";

beforeEach(() => {
  __resetSchemaCacheForTest();
  vi.spyOn(schemaSdk, "fetchAgentSchema").mockResolvedValue(SCHEMA);
});
afterEach(() => vi.restoreAllMocks());

describe("ManifestEditor", () => {
  it("renders the group-nav tree + detail pane and shows the 'basic' group by default", async () => {
    render(
      <ManifestEditor mode="create" initialYaml={SEED} onChange={vi.fn()} />,
    );
    await waitFor(() =>
      expect(screen.getByTestId("af-basic")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("cfg-nav")).toBeInTheDocument();
    expect(screen.getByTestId("cfg-pane")).toBeInTheDocument();
  });

  it("selecting the capabilities group renders CapabilitiesSection's five sub-tabs (tools/mcp/knowledge/skills/subagents)", async () => {
    const user = userEvent.setup();
    render(
      <ManifestEditor mode="create" initialYaml={SEED} onChange={vi.fn()} />,
    );
    await screen.findByTestId("af-basic");
    await user.click(screen.getByTestId("cfg-nav-capabilities"));
    expect(screen.getByTestId("capabilities-section")).toBeInTheDocument();
    // "tools" is the default-active sub-tab.
    expect(screen.getByTestId("af-tools")).toBeInTheDocument();
    // "basic" is no longer shown once another group is active.
    expect(screen.queryByTestId("af-basic")).not.toBeInTheDocument();

    // Each sub-tab lazily mounts its own FormView section on click.
    await user.click(
      screen.getByRole("tab", { name: en.manifest_editor.tab_mcp }),
    );
    expect(screen.getByTestId("af-mcp")).toBeInTheDocument();

    await user.click(
      screen.getByRole("tab", { name: en.manifest_editor.tab_knowledge }),
    );
    expect(screen.getByTestId("af-knowledge")).toBeInTheDocument();

    await user.click(
      screen.getByRole("tab", { name: en.manifest_editor.tab_skills }),
    );
    expect(screen.getByTestId("af-skills")).toBeInTheDocument();

    await user.click(
      screen.getByRole("tab", { name: en.manifest_editor.tab_subagents }),
    );
    expect(screen.getByTestId("af-subagents")).toBeInTheDocument();
  });

  it("the 'budget' group renders RunBudgetSection instead of the pending hint", async () => {
    const user = userEvent.setup();
    render(
      <ManifestEditor mode="create" initialYaml={SEED} onChange={vi.fn()} />,
    );
    await screen.findByTestId("af-basic");
    await user.click(screen.getByTestId("cfg-nav-budget"));
    expect(screen.getByTestId("run-budget-section")).toBeInTheDocument();
    expect(screen.queryByTestId("cfg-pane-pending")).not.toBeInTheDocument();
  });

  it("the 'context' group renders ContextGatesSection instead of the pending hint", async () => {
    const user = userEvent.setup();
    render(
      <ManifestEditor mode="create" initialYaml={SEED} onChange={vi.fn()} />,
    );
    await screen.findByTestId("af-basic");
    await user.click(screen.getByTestId("cfg-nav-context"));
    expect(screen.getByTestId("context-gates-section")).toBeInTheDocument();
    expect(screen.queryByTestId("cfg-pane-pending")).not.toBeInTheDocument();
  });

  it("the 'security' group renders SecuritySection instead of the pending hint", async () => {
    const user = userEvent.setup();
    render(
      <ManifestEditor mode="create" initialYaml={SEED} onChange={vi.fn()} />,
    );
    await screen.findByTestId("af-basic");
    await user.click(screen.getByTestId("cfg-nav-security"));
    expect(screen.getByTestId("security-section")).toBeInTheDocument();
    expect(screen.getByTestId("security-gates-dict-note")).toBeInTheDocument();
    // The defenses tab (its default-active sub-tab) renders underneath.
    expect(screen.getByTestId("security-tab-defenses")).toBeInTheDocument();
    expect(screen.queryByTestId("cfg-pane-pending")).not.toBeInTheDocument();
  });

  it("the 'memory' group renders MemorySection instead of the pending hint", async () => {
    const user = userEvent.setup();
    render(
      <ManifestEditor mode="create" initialYaml={SEED} onChange={vi.fn()} />,
    );
    await screen.findByTestId("af-basic");
    await user.click(screen.getByTestId("cfg-nav-memory"));
    expect(screen.getByTestId("memory-section")).toBeInTheDocument();
    expect(screen.getByTestId("memory-tab-basic")).toBeInTheDocument();
    expect(screen.queryByTestId("cfg-pane-pending")).not.toBeInTheDocument();
  });

  it("remembers a group's active sub-tab across group switches (#2)", async () => {
    const user = userEvent.setup();
    render(
      <ManifestEditor mode="create" initialYaml={SEED} onChange={vi.fn()} />,
    );
    await screen.findByTestId("af-basic");

    // security → open the (non-default) approval sub-tab.
    await user.click(screen.getByTestId("cfg-nav-security"));
    await screen.findByTestId("security-tab-defenses");
    await user.click(
      screen.getByRole("tab", { name: en.security_gates.tab_approval }),
    );
    await screen.findByTestId("security-tab-approval");

    // Leave for another group (unmounts SecuritySection), then come back —
    // the sub-tab selection is held by ManifestEditor, so it survives.
    await user.click(screen.getByTestId("cfg-nav-memory"));
    await screen.findByTestId("memory-tab-basic");
    await user.click(screen.getByTestId("cfg-nav-security"));

    expect(screen.getByTestId("security-tab-approval")).toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: en.security_gates.tab_approval }),
    ).toHaveAttribute("aria-selected", "true");
  });

  it("the 'model' group renders ModelRoutingSection instead of a plain stacked FormView", async () => {
    const user = userEvent.setup();
    render(
      <ManifestEditor mode="create" initialYaml={SEED} onChange={vi.fn()} />,
    );
    await screen.findByTestId("af-basic");
    await user.click(screen.getByTestId("cfg-nav-model"));
    // "model" still lists a real ``sections`` entry in CONFIG_GROUPS, so the
    // distinguishing signal is the curated pane's own wrapper + its
    // YAML-guidance note, which a plain stacked FormView never renders.
    expect(screen.getByTestId("model-routing-section")).toBeInTheDocument();
    expect(screen.getByTestId("model-yaml-note")).toBeInTheDocument();
    // The embedded "model" FormView section (af-model controls) still
    // renders underneath.
    expect(screen.getByTestId("af-model")).toBeInTheDocument();
  });

  it("the 'sandbox' group renders SandboxSection instead of the pending hint", async () => {
    const user = userEvent.setup();
    render(
      <ManifestEditor mode="create" initialYaml={SEED} onChange={vi.fn()} />,
    );
    await screen.findByTestId("af-basic");
    await user.click(screen.getByTestId("cfg-nav-sandbox"));
    expect(screen.getByTestId("sandbox-section")).toBeInTheDocument();
    expect(screen.queryByTestId("cfg-pane-pending")).not.toBeInTheDocument();
  });

  it("the 'observability' group renders ObservabilitySection instead of the pending hint", async () => {
    const user = userEvent.setup();
    render(
      <ManifestEditor mode="create" initialYaml={SEED} onChange={vi.fn()} />,
    );
    await screen.findByTestId("af-basic");
    await user.click(screen.getByTestId("cfg-nav-observability"));
    expect(screen.getByTestId("observability-section")).toBeInTheDocument();
    expect(screen.queryByTestId("cfg-pane-pending")).not.toBeInTheDocument();
  });

  it("YAML toggle round-trips: Form→YAML dumps the manifest, YAML→Form parses it back", async () => {
    const user = userEvent.setup();
    render(
      <ManifestEditor mode="create" initialYaml={SEED} onChange={vi.fn()} />,
    );
    await screen.findByTestId("af-basic");

    await user.click(screen.getByTestId("cfg-yaml-toggle"));
    const ta = screen.getByTestId("monaco-stub") as HTMLTextAreaElement;
    expect(ta.value).toContain("name: bot");
    expect(screen.queryByTestId("manifest-form-view")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("cfg-yaml-toggle"));
    expect(screen.getByTestId("af-basic")).toBeInTheDocument();
    expect(screen.queryByTestId("monaco-stub")).not.toBeInTheDocument();
  });

  it("refuses the YAML→Form switch when YAML is invalid against the schema, staying on YAML", async () => {
    const user = userEvent.setup();
    render(
      <ManifestEditor mode="create" initialYaml={SEED} onChange={vi.fn()} />,
    );
    await screen.findByTestId("af-basic");
    await user.click(screen.getByTestId("cfg-yaml-toggle"));

    const ta = screen.getByTestId("monaco-stub") as HTMLTextAreaElement;
    await user.clear(ta);
    await user.type(ta, "metadata:\n  notname: x");

    await user.click(screen.getByTestId("cfg-yaml-toggle"));
    expect(screen.getByTestId("manifest-switch-error")).toBeInTheDocument();
    expect(screen.getByTestId("monaco-stub")).toBeInTheDocument();
    expect(screen.queryByTestId("manifest-form-view")).not.toBeInTheDocument();
  });

  it("refuses the YAML→Form switch when YAML is syntactically broken", async () => {
    const user = userEvent.setup();
    render(
      <ManifestEditor mode="create" initialYaml={SEED} onChange={vi.fn()} />,
    );
    await screen.findByTestId("af-basic");
    await user.click(screen.getByTestId("cfg-yaml-toggle"));

    const ta = screen.getByTestId("monaco-stub") as HTMLTextAreaElement;
    await user.clear(ta);
    // "[[" is userEvent's escape for a literal "[", yielding "metadata: [unterminated"
    // (an unterminated flow sequence) which js-yaml v4 rejects, exercising the parse-throw branch.
    await user.type(ta, "metadata: [[unterminated");

    await user.click(screen.getByTestId("cfg-yaml-toggle"));
    expect(screen.getByTestId("manifest-switch-error")).toBeInTheDocument();
    expect(screen.queryByTestId("manifest-form-view")).not.toBeInTheDocument();
  });

  it("strips an incomplete fallback entry when serializing to YAML", async () => {
    const user = userEvent.setup();
    const withEmptyFallback =
      "metadata:\n  name: bot\n" +
      "spec:\n  model:\n    provider: openai\n    name: gpt-4o\n" +
      "    fallback:\n      - {}\n";
    render(
      <ManifestEditor
        mode="create"
        initialYaml={withEmptyFallback}
        onChange={vi.fn()}
      />,
    );
    await screen.findByTestId("af-basic");
    await user.click(screen.getByTestId("cfg-yaml-toggle"));
    const ta = screen.getByTestId("monaco-stub") as HTMLTextAreaElement;
    // The empty entry is pruned; the now-empty fallback key drops out entirely.
    expect(ta.value).not.toContain("fallback");
    expect(ta.value).toContain("name: gpt-4o");
  });

  it("emits the latest YAML through onChange on raw edits", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <ManifestEditor mode="create" initialYaml={SEED} onChange={onChange} />,
    );
    await screen.findByTestId("af-basic");
    await user.click(screen.getByTestId("cfg-yaml-toggle"));
    const ta = screen.getByTestId("monaco-stub") as HTMLTextAreaElement;
    await user.clear(ta);
    await user.type(ta, "metadata:\n  name: edited");
    expect(onChange).toHaveBeenLastCalledWith(
      expect.stringContaining("edited"),
    );
  });

  it("emits the dumped YAML through onChange when toggling Form→YAML", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <ManifestEditor mode="create" initialYaml={SEED} onChange={onChange} />,
    );
    await screen.findByTestId("af-basic");
    await user.click(screen.getByTestId("cfg-yaml-toggle"));
    expect(onChange).toHaveBeenLastCalledWith(
      expect.stringContaining("name: bot"),
    );
  });

  it("shows a leading tab as the top tree node, active by default, mounted (hidden) across group switches", async () => {
    const user = userEvent.setup();
    render(
      <ManifestEditor
        mode="create"
        initialYaml={SEED}
        onChange={vi.fn()}
        leadingTabs={[
          {
            value: "meta",
            label: "Basic info",
            content: <div data-testid="meta-form">meta</div>,
          },
        ]}
      />,
    );
    // Leading tab is active by default.
    await screen.findByTestId("meta-form");
    expect(screen.getByTestId("cfg-nav-meta")).toBeInTheDocument();

    // Switching to a manifest group hides — but does not unmount — the
    // leading pane, so any embedded antd Form keeps its state.
    await user.click(screen.getByTestId("cfg-nav-memory"));
    await screen.findByTestId("memory-tab-basic");
    const leadingPane = screen.getByTestId("manifest-leading-meta");
    expect(leadingPane).toHaveStyle({ display: "none" });
    expect(screen.getByTestId("meta-form")).toBeInTheDocument();

    // Switching back re-shows it without remounting.
    await user.click(screen.getByTestId("cfg-nav-meta"));
    expect(screen.getByTestId("manifest-leading-meta")).toHaveStyle({
      display: "block",
    });
    expect(screen.queryByTestId("memory-tab-basic")).not.toBeInTheDocument();
  });

  it("a leading tab with mergeSection folds that manifest section in and de-dupes it from its mapped group", async () => {
    render(
      <ManifestEditor
        mode="create"
        initialYaml={SEED}
        onChange={vi.fn()}
        leadingTabs={[
          {
            value: "meta",
            label: "Basic info",
            content: <div data-testid="meta-form">meta</div>,
            mergeSection: "basic",
          },
        ]}
      />,
    );
    // The merged leading tab is active by default and shows BOTH the caller's
    // content and the manifest's basic section.
    await screen.findByTestId("meta-form");
    expect(screen.getAllByTestId("af-basic")).toHaveLength(1);
    // ``bare`` drops the manifest description so it doesn't duplicate the
    // leading tab's own description field.
    expect(screen.queryByTestId("af-description")).not.toBeInTheDocument();

    // The "basic" group's only section is fully merged away, so its node is
    // hidden entirely — there's no way to re-render the section a second
    // time via the tree (asserted directly here).
    expect(screen.queryByTestId("cfg-nav-basic")).not.toBeInTheDocument();
  });

  it("clicking a group node while YAML is invalid keeps the tree highlight unchanged and stays on YAML with the error shown", async () => {
    const user = userEvent.setup();
    render(
      <ManifestEditor mode="create" initialYaml={SEED} onChange={vi.fn()} />,
    );
    await screen.findByTestId("af-basic");
    await user.click(screen.getByTestId("cfg-yaml-toggle"));

    const ta = screen.getByTestId("monaco-stub") as HTMLTextAreaElement;
    await user.clear(ta);
    await user.type(ta, "metadata:\n  notname: x");

    await user.click(screen.getByTestId("cfg-nav-capabilities"));

    expect(screen.getByTestId("manifest-switch-error")).toBeInTheDocument();
    expect(screen.getByTestId("monaco-stub")).toBeInTheDocument();
    expect(
      screen.queryByTestId("manifest-form-view"),
    ).not.toBeInTheDocument();
    // The highlight must not move to the clicked node.
    expect(screen.getByTestId("cfg-nav-basic")).toHaveClass(
      "ant-menu-item-selected",
    );
    expect(screen.getByTestId("cfg-nav-capabilities")).not.toHaveClass(
      "ant-menu-item-selected",
    );
  });

  it("clicking a group node while YAML is valid exits YAML mode and lands on the clicked group", async () => {
    const user = userEvent.setup();
    render(
      <ManifestEditor mode="create" initialYaml={SEED} onChange={vi.fn()} />,
    );
    await screen.findByTestId("af-basic");
    await user.click(screen.getByTestId("cfg-yaml-toggle"));
    expect(screen.getByTestId("monaco-stub")).toBeInTheDocument();

    await user.click(screen.getByTestId("cfg-nav-capabilities"));

    expect(screen.queryByTestId("monaco-stub")).not.toBeInTheDocument();
    expect(screen.getByTestId("af-tools")).toBeInTheDocument();
    expect(
      screen.queryByTestId("manifest-switch-error"),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId("cfg-nav-capabilities")).toHaveClass(
      "ant-menu-item-selected",
    );
  });

  it("mounts the group search box in the top bar", async () => {
    render(
      <ManifestEditor mode="create" initialYaml={SEED} onChange={vi.fn()} />,
    );
    await screen.findByTestId("af-basic");
    expect(screen.getByTestId("cfg-search")).toBeInTheDocument();
  });

  it("selecting a group via search while YAML is invalid keeps the tree highlight unchanged and stays on YAML with the error shown", async () => {
    const user = userEvent.setup();
    render(
      <ManifestEditor mode="create" initialYaml={SEED} onChange={vi.fn()} />,
    );
    await screen.findByTestId("af-basic");
    await user.click(screen.getByTestId("cfg-yaml-toggle"));

    const ta = screen.getByTestId("monaco-stub") as HTMLTextAreaElement;
    await user.clear(ta);
    await user.type(ta, "metadata:\n  notname: x");

    const searchInput = screen
      .getByTestId("cfg-search")
      .querySelector("input") as HTMLInputElement;
    await user.type(searchInput, "mcp");
    const item = await screen.findByText(
      (_content, el) =>
        el?.classList.contains("ant-select-item-option-content") === true &&
        el.textContent === en.manifest_editor.group_capabilities,
    );
    await user.click(item);

    expect(screen.getByTestId("manifest-switch-error")).toBeInTheDocument();
    expect(screen.getByTestId("monaco-stub")).toBeInTheDocument();
    expect(
      screen.queryByTestId("manifest-form-view"),
    ).not.toBeInTheDocument();
    // The highlight must not move to the searched-for node — same guard
    // as clicking the group node directly (see the test above).
    expect(screen.getByTestId("cfg-nav-basic")).toHaveClass(
      "ant-menu-item-selected",
    );
    expect(screen.getByTestId("cfg-nav-capabilities")).not.toHaveClass(
      "ant-menu-item-selected",
    );
  });

  it("selecting a group via search while YAML is valid exits YAML mode and lands on the picked group", async () => {
    const user = userEvent.setup();
    render(
      <ManifestEditor mode="create" initialYaml={SEED} onChange={vi.fn()} />,
    );
    await screen.findByTestId("af-basic");

    const searchInput = screen
      .getByTestId("cfg-search")
      .querySelector("input") as HTMLInputElement;
    await user.type(searchInput, "mcp");
    const item = await screen.findByText(
      (_content, el) =>
        el?.classList.contains("ant-select-item-option-content") === true &&
        el.textContent === en.manifest_editor.group_capabilities,
    );
    await user.click(item);

    expect(screen.getByTestId("af-tools")).toBeInTheDocument();
    expect(screen.getByTestId("cfg-nav-capabilities")).toHaveClass(
      "ant-menu-item-selected",
    );
  });

  it("excludes a fully-merged-away (hidden) group from search results", async () => {
    const user = userEvent.setup();
    render(
      <ManifestEditor
        mode="create"
        initialYaml={SEED}
        onChange={vi.fn()}
        leadingTabs={[
          {
            value: "meta",
            label: "Basic info",
            content: <div data-testid="meta-form">meta</div>,
            mergeSection: "basic",
          },
        ]}
      />,
    );
    await screen.findByTestId("meta-form");
    expect(screen.queryByTestId("cfg-nav-basic")).not.toBeInTheDocument();

    const searchInput = screen
      .getByTestId("cfg-search")
      .querySelector("input") as HTMLInputElement;
    // "name" is one of the "basic" group's search keywords — it would
    // otherwise match, but the group's node is hidden (merged away), so it
    // must never be offered as a search result either.
    await user.type(searchInput, "name");
    expect(
      screen.queryByText(en.manifest_editor.group_basic, {
        selector: ".ant-select-item-option-content",
      }),
    ).not.toBeInTheDocument();
  });
});
