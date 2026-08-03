import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import "../../../i18n";

import { KnowledgePicker } from "../KnowledgePicker";
import type { AgentManifest } from "../form_model";

// Cross-tenant W3 — the picker reads the ambient tenant scope; these tests
// don't mount a TenantScopeProvider, so mock it (home state: no scope).
const scopeRef = vi.hoisted(() => ({ current: undefined as string | undefined }));
vi.mock("../../../tenant/TenantScopeContext", async (importOriginal) => {
  const { mockTenantScopeModule } = await import("../../../test-utils/tenantScopeMock");
  return mockTenantScopeModule(
    await importOriginal<typeof import("../../../tenant/TenantScopeContext")>(),
    scopeRef,
  );
});

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

  it("collapses the '*' aggregate to the caller's tenant", async () => {
    // W4: /v1/knowledge/bases aggregates every tenant under "*", but a ref
    // binds by name inside the agent's OWN tenant — offering foreign bases
    // would let the author pick one the runtime can never resolve.
    const { listBases } = await import("../../../api/knowledge");
    (listBases as ReturnType<typeof vi.fn>).mockClear();
    scopeRef.current = "*";
    try {
      render(<KnowledgePicker formData={SEED} onChange={vi.fn()} />);
      await waitFor(() => expect(listBases).toHaveBeenCalled());
      expect(listBases).toHaveBeenCalledWith(undefined);
    } finally {
      scopeRef.current = undefined;
    }
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
