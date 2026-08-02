/**
 * Platform skill detail page — skill-authoring-ia Phase C.
 *
 * The platform detail page reuses the tenant ``SkillDetail`` editor through
 * the ``platform`` variant. This locks the variant differences:
 *   - the tenant-flywheel panels (governance / lineage / eval evidence) are
 *     hidden (platform skills are human-curated, not agent-evolved)
 *   - the ``required_tier`` tag is shown
 *   - the editor (file tree + version picker) still renders
 *
 * Backend ``/v1/platform/skills`` is raw (no envelope); the adapter mock
 * returns raw objects.
 */
import { describe, expect, it, beforeEach, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { App } from "antd";
import { render, screen, waitFor } from "@testing-library/react";
import "../../i18n";

import { SettingsPlatformSkillDetail } from "../SettingsPlatformSkillDetail";
import { AuthProvider } from "../../auth/AuthContext";
import { apiClient, setStoredToken } from "../../api/client";

// Cross-tenant W3 — SkillDetail reads the ambient tenant scope; these tests
// don't mount a TenantScopeProvider, so mock it (home state: no scope).
vi.mock("../../tenant/TenantScopeContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../tenant/TenantScopeContext")>()),
  useTenantScope: () => ({
    scope: "home",
    setScope: () => {},
    apiTenantScope: undefined,
  }),
}));

const TENANT = "00000000-0000-0000-0000-00000000acme";

function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.`;
}

const SKILL = {
  id: "psk-1",
  name: "web_search",
  status: "active",
  latest_version: 1,
  description: "Search the web.",
  category: "web",
  pinned: false,
  required_tier: "pro",
  last_used_at: null,
  state_changed_at: null,
  created_at: "2026-05-20T10:00:00Z",
  updated_at: "2026-05-26T10:00:00Z",
};

const VERSION = {
  id: "v1",
  skill_id: "psk-1",
  version: 1,
  prompt_fragment: "Always cite sources.",
  tool_names: ["web_search"],
  description: "First cut.",
  category: "web",
  required_models: [],
  authored_by: "human",
  supporting_files: {},
  lazy_load: false,
  high_risk: false,
  created_at: "2026-05-20T10:00:00Z",
};

const patchCalls: Array<Record<string, unknown>> = [];

function installAdapter() {
  patchCalls.length = 0;
  apiClient.defaults.adapter = (config) => {
    const url = config.url ?? "";
    const method = (config.method ?? "get").toLowerCase();
    let data: unknown = {};
    if (url.endsWith("/platform/skills/categories")) data = { categories: ["web", "data"] };
    else if (url.endsWith("/platform/skills/psk-1/versions")) data = { items: [VERSION] };
    else if (url.endsWith("/platform/skills/psk-1") && method === "patch") {
      const body = JSON.parse(config.data as string) as Record<string, unknown>;
      patchCalls.push(body);
      data = { ...SKILL, category: (body.category as string) || null };
    } else if (url.endsWith("/platform/skills/psk-1")) data = SKILL;
    return Promise.resolve({
      data,
      status: 200,
      statusText: "OK",
      headers: {},
      config,
      request: {},
    });
  };
}

function renderDetail() {
  setStoredToken(makeJwt({ sub: "u1", tenant_id: TENANT, roles: ["system_admin"] }));
  return render(
    <MemoryRouter initialEntries={["/settings/platform-skills/psk-1"]}>
      <AuthProvider>
        <App>
          <Routes>
            <Route
              path="/settings/platform-skills/:skillId"
              element={<SettingsPlatformSkillDetail />}
            />
          </Routes>
        </App>
      </AuthProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  installAdapter();
});

describe("SettingsPlatformSkillDetail (platform variant)", () => {
  it("renders the editor but hides the tenant-flywheel panels", async () => {
    renderDetail();
    await waitFor(() => expect(screen.getByTestId("skill-detail-root")).toBeInTheDocument());
    // Editor surfaces (version picker) render.
    expect(screen.getByTestId("skill-version-picker")).toBeInTheDocument();
    // Flywheel panels are platform-hidden.
    expect(screen.queryByTestId("skill-governance-panel")).not.toBeInTheDocument();
    expect(screen.queryByTestId("skill-lineage-panel")).not.toBeInTheDocument();
    expect(screen.queryByTestId("skill-eval-panel")).not.toBeInTheDocument();
  });

  it("shows the required_tier tag", async () => {
    renderDetail();
    await waitFor(() => expect(screen.getByTestId("skill-detail-root")).toBeInTheDocument());
    // platform_skills.tier_pro — zh "专业版" / en "Pro" depending on the
    // detected test locale.
    expect(screen.getByText(/^(专业版|Pro)$/)).toBeInTheDocument();
  });

  it("edits the category via the metadata panel (platform only)", async () => {
    const { default: userEvent } = await import("@testing-library/user-event");
    const user = userEvent.setup();
    renderDetail();
    await waitFor(() => expect(screen.getByTestId("skill-metadata-panel")).toBeInTheDocument());

    // The testid is on the AutoComplete wrapper; the editable element is the
    // inner combobox input.
    const input = screen
      .getByTestId("skill-category-input")
      .querySelector("input") as HTMLInputElement;
    await user.click(input);
    await user.clear(input);
    await user.type(input, "mental-health");
    await user.click(screen.getByTestId("skill-category-save"));

    await waitFor(() =>
      expect(patchCalls.some((b) => b.category === "mental-health")).toBe(true),
    );
  });
});
