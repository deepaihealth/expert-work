import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../../../i18n";

// The "mcp" tab mounts McpToolPicker, which loads servers on mount.
vi.mock("../../../../api/mcp-servers", () => ({
  listAvailableMcpServers: vi.fn().mockResolvedValue([]),
  listMcpServerTools: vi.fn().mockResolvedValue([]),
}));
vi.mock("../../../../api/mcp-catalog", () => ({
  listPlatformCatalog: vi.fn().mockResolvedValue([]),
  listCatalogTools: vi.fn().mockResolvedValue({ status: "ok", tools: [] }),
}));

// Cross-tenant W3 — the pickers read the ambient tenant scope; these tests
// don't mount a TenantScopeProvider, so mock it (home state: no scope).
const scopeRef = vi.hoisted(() => ({ current: undefined as string | undefined }));
vi.mock("../../../../tenant/TenantScopeContext", async (importOriginal) => {
  const { mockTenantScopeModule } = await import("../../../../test-utils/tenantScopeMock");
  return mockTenantScopeModule(
    await importOriginal<typeof import("../../../../tenant/TenantScopeContext")>(),
    scopeRef,
  );
});

import * as mcpServersApi from "../../../../api/mcp-servers";
import * as mcpCatalogApi from "../../../../api/mcp-catalog";
import { CapabilitiesSection } from "../CapabilitiesSection";
import type { AgentManifest } from "../../form_model";
import type { McpPickerSource } from "../../widgets/McpToolPicker";

function renderSection(
  formData: AgentManifest = {},
  onChange: (d: unknown) => void = vi.fn(),
  mcpSource?: McpPickerSource,
) {
  return render(
    <CapabilitiesSection
      formData={formData}
      onChange={onChange}
      mcpSource={mcpSource}
    />,
  );
}

afterEach(() => {
  vi.clearAllMocks();
});

async function openTab(
  user: ReturnType<typeof userEvent.setup>,
  label: string,
): Promise<void> {
  await user.click(screen.getByRole("tab", { name: label }));
}

describe("CapabilitiesSection", () => {
  it("shows the tools tab by default", () => {
    renderSection();
    expect(screen.getByTestId("af-tools")).toBeInTheDocument();
    expect(screen.queryByTestId("af-mcp")).not.toBeInTheDocument();
    expect(screen.queryByTestId("af-knowledge")).not.toBeInTheDocument();
    expect(screen.queryByTestId("af-skills")).not.toBeInTheDocument();
    expect(screen.queryByTestId("af-subagents")).not.toBeInTheDocument();
  });

  it("clicking each sub-tab shows that section's own FormView content", async () => {
    const user = userEvent.setup();
    renderSection();

    await openTab(user, "MCP");
    expect(screen.getByTestId("af-mcp")).toBeInTheDocument();

    await openTab(user, "Knowledge");
    expect(screen.getByTestId("af-knowledge")).toBeInTheDocument();

    await openTab(user, "Skills");
    expect(screen.getByTestId("af-skills")).toBeInTheDocument();

    await openTab(user, "Sub-agents");
    expect(screen.getByTestId("af-subagents")).toBeInTheDocument();
  });

  it("defaults the mcp tab to the 'available' source when mcpSource is omitted", async () => {
    const user = userEvent.setup();
    renderSection({}, vi.fn(), undefined);
    await openTab(user, "MCP");
    expect(mcpServersApi.listAvailableMcpServers).toHaveBeenCalled();
    expect(mcpCatalogApi.listPlatformCatalog).not.toHaveBeenCalled();
  });

  it("forwards mcpSource='catalog' through to the mcp tab's FormView/McpToolPicker", async () => {
    const user = userEvent.setup();
    renderSection({}, vi.fn(), "catalog");
    await openTab(user, "MCP");
    expect(mcpCatalogApi.listPlatformCatalog).toHaveBeenCalled();
    expect(mcpServersApi.listAvailableMcpServers).not.toHaveBeenCalled();
  });
});
