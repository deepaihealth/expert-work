import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../../i18n";

import { SubagentPicker } from "../SubagentPicker";
import type { AgentManifest } from "../form_model";

vi.mock("../../../api/agents", () => ({
  listAgents: vi.fn().mockResolvedValue({
    items: [
      { id: "a1", name: "deep-researcher", version: "1.0.0", status: "active" },
    ],
    total: 1,
    cross_tenant: false,
  }),
}));

const SEED: AgentManifest = {
  apiVersion: "expert_work/v1",
  kind: "Agent",
  metadata: { name: "bot" },
  spec: {},
};

describe("SubagentPicker", () => {
  it("renders the dynamic-subagent toggle (moved from the security/network tab) and writes dynamic_workers.enabled", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<SubagentPicker formData={SEED} onChange={onChange} />);
    const toggle = screen.getByRole("switch");
    expect(toggle).toBeChecked(); // default-on
    await user.click(toggle);
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(
      (last.spec as { dynamic_workers?: { enabled?: boolean } }).dynamic_workers,
    ).toEqual({ enabled: false });
  });

  it("loads the deployed-agent option list", async () => {
    const { listAgents } = await import("../../../api/agents");
    render(<SubagentPicker formData={SEED} onChange={vi.fn()} />);
    await waitFor(() => expect(listAgents).toHaveBeenCalled());
  });

  it("adding a sub-agent appends an empty row", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<SubagentPicker formData={SEED} onChange={onChange} />);
    await user.click(screen.getByTestId("af-subagent-add"));
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.subagents).toEqual([
      { name: "", agent_ref: "", description: "" },
    ]);
  });

  it("editing a sub-agent name patches that row", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const seeded: AgentManifest = {
      ...SEED,
      spec: { subagents: [{ name: "", agent_ref: "", description: "" }] },
    };
    render(<SubagentPicker formData={seeded} onChange={onChange} />);
    await user.type(screen.getByTestId("af-subagent-name-0"), "r");
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.subagents?.[0].name).toBe("r");
  });

  it("removing the last sub-agent drops the block", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const seeded: AgentManifest = {
      ...SEED,
      spec: { subagents: [{ name: "x", agent_ref: "y@1", description: "z" }] },
    };
    render(<SubagentPicker formData={seeded} onChange={onChange} />);
    await user.click(screen.getByTestId("af-subagent-remove-0"));
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.subagents).toBeUndefined();
  });
});
