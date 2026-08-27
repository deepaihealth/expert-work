/**
 * AgentDetail overview — egress summary (终审 I-2).
 *
 * ``readEgress`` used to read ``spec.sandbox`` off ``record.spec`` directly,
 * but ``record.spec`` is the FULL manifest ({apiVersion, kind, metadata,
 * spec}) — ``sandbox`` lives one level down at ``spec.spec.sandbox``. The
 * card silently always showed the "proxy, no allowlist" default regardless
 * of what the manifest actually configured (same「多包一层壳」family as the
 * two Playground bugs, see PlaygroundTab.tsx:138/:213). This renders the
 * real page (route params + getAgent fetch) so the fix is covered end to
 * end, mirroring AgentKillSwitch.test.tsx's skeleton.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { App } from "antd";
import { render, screen } from "@testing-library/react";
import i18n from "../../i18n";

import { AgentDetail } from "../AgentDetail";
import type { AgentDetailResponse } from "../../api/agents";
import { getAgent } from "../../api/agents";

vi.mock("../../api/agents", async () => {
  const actual = await vi.importActual<typeof import("../../api/agents")>("../../api/agents");
  return {
    ...actual,
    getAgent: vi.fn(),
    disableAgent: vi.fn(),
    enableAgent: vi.fn(),
  };
});

const { isTenantSwitchedMock } = vi.hoisted(() => ({
  isTenantSwitchedMock: vi.fn(() => false),
}));
const scopeRef = vi.hoisted(() => ({ current: undefined as string | undefined }));
vi.mock("../../tenant/TenantScopeContext", async (importOriginal) => {
  const { mockTenantScopeModule } = await import("../../test-utils/tenantScopeMock");
  return mockTenantScopeModule(
    await importOriginal<typeof import("../../tenant/TenantScopeContext")>(),
    scopeRef,
  );
});
vi.mock("../../tenant/useIsTenantSwitched", () => ({
  useIsTenantSwitched: isTenantSwitchedMock,
}));

function detail(spec: Record<string, unknown>): AgentDetailResponse {
  return {
    record: {
      id: "11111111-1111-1111-1111-111111111111",
      tenant_id: "22222222-2222-2222-2222-222222222222",
      name: "code-reviewer",
      version: "1.0.0",
      status: "active",
      spec_sha256: "a".repeat(64),
      created_by: "user-1",
      created_at: "2026-06-12T00:00:00Z",
      updated_at: "2026-06-12T00:00:00Z",
      spec,
    },
    disabled: false,
    disable: null,
  } as AgentDetailResponse;
}

function renderPage(): void {
  render(
    <MemoryRouter initialEntries={["/agents/code-reviewer/1.0.0/overview"]}>
      <App>
        <Routes>
          <Route path="/agents/:name/:version/:tab" element={<AgentDetail />} />
        </Routes>
      </App>
    </MemoryRouter>,
  );
}

afterEach(() => vi.clearAllMocks());

beforeEach(() => {
  isTenantSwitchedMock.mockReturnValue(false);
});

describe("AgentDetail overview — egress summary", () => {
  // Locale-sensitive assertions — pin zh-CN explicitly and restore afterward
  // (the i18n singleton persists its resolved language across `it` blocks;
  // jsdom's default navigator.language resolves the detector to "en").
  let priorLang: string;
  beforeEach(async () => {
    priorLang = i18n.language;
    await i18n.changeLanguage("zh-CN");
  });
  afterEach(async () => {
    await i18n.changeLanguage(priorLang);
  });

  it("shows isolated when the full manifest's spec.sandbox.network.egress is none", async () => {
    vi.mocked(getAgent).mockResolvedValue(
      detail({
        apiVersion: "expert_work.io/v1",
        kind: "Agent",
        metadata: { name: "code-reviewer", version: "1.0.0", tenant: "acme" },
        spec: {
          sandbox: { network: { egress: "none", allowlist: [] } },
        },
      }),
    );
    renderPage();

    expect(await screen.findByText("隔离（不出网）")).toBeInTheDocument();
  });

  it("shows the allowlist host when the full manifest's spec.sandbox.network is proxy with an allowlist", async () => {
    vi.mocked(getAgent).mockResolvedValue(
      detail({
        apiVersion: "expert_work.io/v1",
        kind: "Agent",
        metadata: { name: "code-reviewer", version: "1.0.0", tenant: "acme" },
        spec: {
          sandbox: {
            network: { egress: "proxy", allowlist: ["api.example.com"] },
          },
        },
      }),
    );
    renderPage();

    expect(await screen.findByText("api.example.com")).toBeInTheDocument();
  });
});
