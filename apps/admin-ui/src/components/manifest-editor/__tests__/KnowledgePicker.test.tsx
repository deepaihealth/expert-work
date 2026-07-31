import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import "../../../i18n";

import { KnowledgePicker } from "../KnowledgePicker";
import type { AgentManifest } from "../form_model";

// Cross-tenant W3 — the picker reads the ambient tenant scope; these tests
// don't mount a TenantScopeProvider, so mock it (home state: no scope).
vi.mock("../../../tenant/TenantScopeContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../tenant/TenantScopeContext")>()),
  useTenantScope: () => ({
    scope: "home",
    setScope: () => {},
    apiTenantScope: undefined,
  }),
}));

vi.mock("../../../api/knowledge", () => ({
  listBases: vi.fn().mockResolvedValue([
    {
      id: "1",
      name: "hr",
      chunk_max_tokens: 800,
      chunk_overlap_tokens: 80,
      created_at: null,
    },
  ]),
}));

const SEED: AgentManifest = {
  apiVersion: "expert_work/v1",
  kind: "Agent",
  metadata: { name: "bot" },
  spec: {},
};

describe("KnowledgePicker", () => {
  it("renders the knowledge section and loads bases", async () => {
    const { listBases } = await import("../../../api/knowledge");
    render(<KnowledgePicker formData={SEED} onChange={vi.fn()} />);
    expect(screen.getByTestId("af-knowledge")).toBeInTheDocument();
    await waitFor(() => expect(listBases).toHaveBeenCalled());
  });

  it("reflects the selected refs (supports multiple bases)", async () => {
    const seeded: AgentManifest = {
      ...SEED,
      spec: { knowledge: { knowledge_base_refs: ["hr", "eng"] } },
    };
    render(<KnowledgePicker formData={seeded} onChange={vi.fn()} />);
    // Both refs render as selected tags (mode="tags"); the loaded "hr" base
    // gets its chunk-config label, "eng" stays a raw value tag.
    expect(await screen.findByText(/hr/)).toBeInTheDocument();
    expect(await screen.findByText(/eng/)).toBeInTheDocument();
  });
});
