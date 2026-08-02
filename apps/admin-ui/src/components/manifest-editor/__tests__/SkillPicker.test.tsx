import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../../i18n";

import { SkillPicker } from "../SkillPicker";
import { listSkills, type SkillRecord } from "../../../api/skills";
import type { AgentManifest } from "../form_model";

// Cross-tenant W3 — the picker reads the ambient tenant scope; these tests
// don't mount a TenantScopeProvider, so mock it (switchable per test;
// undefined = home state).
const scopeRef = vi.hoisted(() => ({ current: undefined as string | undefined }));
vi.mock("../../../tenant/TenantScopeContext", async (importOriginal) => {
  const { mockTenantScopeModule } = await import("../../../test-utils/tenantScopeMock");
  return mockTenantScopeModule(
    await importOriginal<typeof import("../../../tenant/TenantScopeContext")>(),
    scopeRef,
  );
});

/** Open an antd Select (by its data-testid root) and click the option whose
 *  visible content matches — a string (exact) or regex (language-tolerant,
 *  since source labels are localized). Mirrors ModelSelect.test's helper. */
async function pickOption(
  user: ReturnType<typeof userEvent.setup>,
  root: HTMLElement,
  match: string | RegExp,
): Promise<void> {
  await user.click(root.querySelector(".ant-select-selector") as HTMLElement);
  const item = await screen.findByText((_content, el) => {
    if (el?.classList.contains("ant-select-item-option-content") !== true)
      return false;
    const txt = el.textContent ?? "";
    return typeof match === "string" ? txt === match : match.test(txt);
  });
  await user.click(item);
}

function rec(over: Partial<SkillRecord> & { name: string }): SkillRecord {
  return {
    id: over.name,
    status: "active",
    latest_version: 1,
    description: "",
    category: "general",
    pinned: false,
    last_used_at: null,
    state_changed_at: null,
    created_at: "",
    updated_at: "",
    ...over,
  } as SkillRecord;
}

vi.mock("../../../api/skills", () => ({
  listSkills: vi.fn().mockResolvedValue({
    items: [
      rec({
        name: "pptx",
        description: "Build slide decks",
        category: "office",
        source: "tenant",
      }),
    ],
    platform_items: [
      rec({
        name: "sql-analyst",
        description: "Query databases",
        category: "data",
        source: "platform",
        entitled: true,
      }),
      rec({
        name: "premium-x",
        description: "Locked capability",
        category: "pro",
        source: "platform",
        entitled: false,
        required_tier: "enterprise",
      }),
    ],
    next_cursor: null,
    cross_tenant: false,
  }),
}));

const SEED: AgentManifest = {
  apiVersion: "expert_work/v1",
  kind: "Agent",
  metadata: { name: "bot" },
  spec: {},
};

describe("SkillPicker", () => {
  it("renders each skill with description, source and category", async () => {
    render(<SkillPicker formData={SEED} onChange={vi.fn()} />);
    expect(await screen.findByText("Build slide decks")).toBeInTheDocument();
    expect(screen.getByText("Query databases")).toBeInTheDocument();
    expect(screen.getByText("office")).toBeInTheDocument();
    // both a platform and a tenant badge are present
    expect(screen.getAllByText(/平台|Platform/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/租户|Tenant/).length).toBeGreaterThan(0);
  });

  it("checking a skill emits it into spec.skills", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<SkillPicker formData={SEED} onChange={onChange} />);
    const check = await screen.findByTestId("af-skill-check-pptx");
    await user.click(check);
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.skills).toEqual(["pptx"]);
  });

  it("a tier-locked platform skill cannot be checked", async () => {
    render(<SkillPicker formData={SEED} onChange={vi.fn()} />);
    const locked = await screen.findByTestId("af-skill-check-premium-x");
    expect(locked).toBeDisabled();
  });

  it("an already-selected skill stays checked even when not in the list", async () => {
    const seeded: AgentManifest = {
      ...SEED,
      spec: { skills: ["hand-added"] },
    };
    render(<SkillPicker formData={seeded} onChange={vi.fn()} />);
    const check = await screen.findByTestId("af-skill-check-hand-added");
    expect(check).toBeChecked();
  });

  it("unchecking a selected skill removes it", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const seeded: AgentManifest = { ...SEED, spec: { skills: ["pptx"] } };
    render(<SkillPicker formData={seeded} onChange={onChange} />);
    const check = await screen.findByTestId("af-skill-check-pptx");
    await user.click(check);
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.skills).toBeUndefined();
  });

  // SE-16 (SE-A42) — evolution auto-attach opt-in
  it("auto-attach switch is off by default and writes true when toggled", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<SkillPicker formData={SEED} onChange={onChange} />);
    const toggle = await screen.findByTestId("af-auto-attach-evolved-switch");
    expect(toggle).not.toBeChecked();
    await user.click(toggle);
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.auto_attach_evolved_skills).toBe(true);
  });

  it("turning auto-attach off drops the key (clean YAML)", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const seeded: AgentManifest = {
      ...SEED,
      spec: { auto_attach_evolved_skills: true },
    };
    render(<SkillPicker formData={seeded} onChange={onChange} />);
    const toggle = await screen.findByTestId("af-auto-attach-evolved-switch");
    expect(toggle).toBeChecked();
    await user.click(toggle);
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.auto_attach_evolved_skills).toBeUndefined();
  });

  // Filtering (category + source dropdowns) + a capped-height scroll area.
  // A larger roster (>6) is what surfaces the filter controls at all.
  const MANY = {
    items: [
      rec({
        name: "t-office",
        description: "tenant office",
        category: "office",
        source: "tenant",
      }),
      rec({
        name: "t-data",
        description: "tenant data",
        category: "data",
        source: "tenant",
      }),
    ],
    platform_items: [
      rec({
        name: "p-med-1",
        description: "med one",
        category: "medical",
        source: "platform",
        entitled: true,
      }),
      rec({
        name: "p-med-2",
        description: "med two",
        category: "medical",
        source: "platform",
        entitled: true,
      }),
      rec({
        name: "p-med-3",
        description: "med three",
        category: "medical",
        source: "platform",
        entitled: true,
      }),
      rec({
        name: "p-eff-1",
        description: "eff one",
        category: "efficiency",
        source: "platform",
        entitled: true,
      }),
      rec({
        name: "p-eff-2",
        description: "eff two",
        category: "efficiency",
        source: "platform",
        entitled: true,
      }),
    ],
    next_cursor: null,
    cross_tenant: false,
  };

  it("category filter narrows the list to the chosen category", async () => {
    const user = userEvent.setup();
    vi.mocked(listSkills).mockResolvedValueOnce(MANY);
    render(<SkillPicker formData={SEED} onChange={vi.fn()} />);
    expect(
      await screen.findByTestId("af-skill-row-p-med-1"),
    ).toBeInTheDocument();

    await pickOption(user, screen.getByTestId("af-skills-category"), "medical");

    expect(screen.getByTestId("af-skill-row-p-med-1")).toBeInTheDocument();
    expect(screen.getByTestId("af-skill-row-p-med-3")).toBeInTheDocument();
    expect(
      screen.queryByTestId("af-skill-row-t-office"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("af-skill-row-p-eff-1"),
    ).not.toBeInTheDocument();
  });

  it("source filter narrows the list to the chosen source", async () => {
    const user = userEvent.setup();
    vi.mocked(listSkills).mockResolvedValueOnce(MANY);
    render(<SkillPicker formData={SEED} onChange={vi.fn()} />);
    expect(
      await screen.findByTestId("af-skill-row-t-office"),
    ).toBeInTheDocument();

    await pickOption(
      user,
      screen.getByTestId("af-skills-source"),
      /^(租户|Tenant)$/,
    );

    expect(screen.getByTestId("af-skill-row-t-office")).toBeInTheDocument();
    expect(screen.getByTestId("af-skill-row-t-data")).toBeInTheDocument();
    expect(
      screen.queryByTestId("af-skill-row-p-med-1"),
    ).not.toBeInTheDocument();
  });

  it("wraps the list in a capped-height scroll area", async () => {
    render(<SkillPicker formData={SEED} onChange={vi.fn()} />);
    const scroll = await screen.findByTestId("af-skills-scroll");
    expect(scroll).toHaveStyle({ overflowY: "auto" });
  });

  it("threads the ambient tenant scope into listSkills (W3)", async () => {
    scopeRef.current = "22222222-2222-2222-2222-222222222222";
    try {
      render(<SkillPicker formData={SEED} onChange={vi.fn()} />);
      await waitFor(() =>
        expect(listSkills).toHaveBeenCalledWith({ tenantScope: scopeRef.current }),
      );
    } finally {
      scopeRef.current = undefined;
    }
  });
});
