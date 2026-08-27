/**
 * Platform Skills UI tests — Stream X (X5).
 *
 * Covers the platform skill page (admin gate + table + empty/error
 * states) and the create / add-version / status-change / pin / import
 * flows.
 *
 * Backend ``/v1/platform/skills`` returns *raw* ``JSONResponse`` payloads
 * (NOT the ``{success, data, error}`` envelope), so the adapter mock
 * returns the raw object. An earlier version of this mock wrongly
 * enveloped the responses, which masked the SDK's envelope-vs-raw bug
 * (the page threw ``request failed`` against the real backend). Keep the
 * mock raw so it matches production.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { App } from "antd";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../i18n";

import { SettingsPlatformSkills } from "../SettingsPlatformSkills";
import { AuthProvider } from "../../auth/AuthContext";
import { apiClient, setStoredToken } from "../../api/client";

const TENANT = "00000000-0000-0000-0000-00000000acme";

function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.`;
}

interface RouteHandler {
  match: (url: string, method: string) => boolean;
  respond: (config: {
    data?: unknown;
    url: string;
    method: string;
    params?: Record<string, unknown>;
  }) => unknown;
  status?: number;
}

function installAdapter(handlers: RouteHandler[]) {
  apiClient.defaults.adapter = (config) => {
    const url = config.url ?? "";
    const method = (config.method ?? "get").toLowerCase();
    const handler = handlers.find((h) => h.match(url, method));
    if (handler === undefined) {
      return Promise.reject({
        isAxiosError: true,
        response: { status: 404, data: { detail: `no mock for ${method} ${url}` } },
        message: `no mock for ${method} ${url}`,
        config,
      });
    }
    return Promise.resolve({
      data:
        handler.respond({
          data: config.data,
          url,
          method,
          params: config.params as Record<string, unknown> | undefined,
        }) ?? {},
      status: handler.status ?? 200,
      statusText: "OK",
      headers: {},
      config,
      request: {},
    });
  };
}

const SKILL = {
  id: "psk-1",
  name: "web_search",
  status: "active" as const,
  latest_version: 2,
  description: "Search the web and return top N results.",
  category: "web",
  pinned: false,
  required_tier: "pro" as const,
  last_used_at: "2026-05-25T10:00:00Z",
  state_changed_at: "2026-05-20T10:00:00Z",
  created_at: "2026-05-20T10:00:00Z",
  updated_at: "2026-05-26T10:00:00Z",
};

const SKILL2 = { ...SKILL, id: "psk-2", name: "code_search" };

const VERSION = {
  id: "v1",
  skill_id: "psk-1",
  version: 1,
  prompt_fragment: "Always cite sources.",
  tool_names: ["web_search"],
  description: "First cut.",
  category: "web",
  required_models: [],
  authored_by: "platform",
  supporting_files: {},
  lazy_load: false,
  high_risk: false,
  created_at: "2026-05-20T10:00:00Z",
};

/** The backend returns the payload raw (no ``{success, data, error}``
 *  envelope); the mock mirrors that exactly. */
function raw<T>(data: T): T {
  return data;
}

/** Shared list + categories mocks for the batch "set category" tests below —
 *  two rows (so a header-checkbox selection yields ``ids: ["psk-1", "psk-2"]``,
 *  mirroring the batch-lock test) and a non-empty category list (so the
 *  "set category" dropdown has an item to click). */
const listAndCategoryHandlers: RouteHandler[] = [
  {
    match: (u, m) => u.endsWith("/platform/skills") && m === "get",
    respond: () => raw({ items: [SKILL, SKILL2], total: 2 }),
  },
  {
    match: (u) => u.endsWith("/platform/skills/categories"),
    respond: () => raw({ categories: ["研发"] }),
  },
];

function renderPage(roles: string[]) {
  setStoredToken(makeJwt({ sub: "u1", tenant_id: TENANT, roles }));
  return render(
    <MemoryRouter>
      <AuthProvider>
        <App>
          <SettingsPlatformSkills />
        </App>
      </AuthProvider>
    </MemoryRouter>,
  );
}

/** Probe rendered by the detail route so the test can assert navigation. */
function DetailProbe() {
  const loc = useLocation();
  return <div data-testid="detail-probe">{loc.pathname}</div>;
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("SettingsPlatformSkills page", () => {
  it("non-system-admin sees the admin-only notice, no table", async () => {
    installAdapter([
      {
        match: (u) => u.endsWith("/platform/skills"),
        respond: () => raw({ items: [], total: 0 }),
      },
    ]);
    renderPage(["admin"]);
    await waitFor(() => expect(screen.getByTestId("ps-not-admin")).toBeInTheDocument());
    expect(screen.queryByTestId("ps-table")).not.toBeInTheDocument();
  });

  it("system_admin sees the table with skill rows", async () => {
    installAdapter([
      {
        match: (u) => u.endsWith("/platform/skills"),
        respond: () => raw({ items: [SKILL], total: 1 }),
      },
    ]);
    renderPage(["system_admin"]);
    await waitFor(() => expect(screen.getByTestId("ps-table")).toBeInTheDocument());
    expect(screen.getByText("web_search")).toBeInTheDocument();
    expect(screen.getByTestId("ps-manage-psk-1")).toBeInTheDocument();
    expect(screen.getByTestId("ps-pin-toggle-psk-1")).toBeInTheDocument();
  });

  it("row export button downloads the latest version as .skill (W4 D1)", async () => {
    let exportCalls = 0;
    installAdapter([
      {
        match: (u, m) =>
          u.endsWith("/platform/skills/psk-1/versions/2/export") && m === "get",
        respond: () => {
          exportCalls += 1;
          return new Blob(["zip"]);
        },
      },
      {
        match: (u, m) => u.endsWith("/platform/skills") && m === "get",
        respond: () => raw({ items: [SKILL], total: 1 }),
      },
    ]);
    (URL as unknown as { createObjectURL: () => string }).createObjectURL = vi.fn(
      () => "blob:mock",
    );
    (URL as unknown as { revokeObjectURL: (u: string) => void }).revokeObjectURL =
      vi.fn();
    const downloads: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      downloads.push(this.download);
    });

    renderPage(["system_admin"]);
    await waitFor(() => expect(screen.getByTestId("ps-export-psk-1")).toBeInTheDocument());
    await userEvent.click(screen.getByTestId("ps-export-psk-1"));
    await waitFor(() => expect(exportCalls).toBe(1));
    expect(downloads).toEqual(["web_search-v2.skill"]);
  });

  it("batch-locks the selected skills via the server-side batch endpoint", async () => {
    const batched: Array<Record<string, unknown>> = [];
    installAdapter([
      {
        match: (u, m) => u.endsWith("/platform/skills/batch") && m === "post",
        respond: ({ data }) => {
          batched.push(typeof data === "string" ? JSON.parse(data) : (data as object));
          return raw({ updated: 2 });
        },
      },
      {
        match: (u) => u.endsWith("/platform/skills"),
        respond: () => raw({ items: [SKILL, SKILL2], total: 2 }),
      },
    ]);
    renderPage(["system_admin"]);
    await waitFor(() => expect(screen.getByTestId("ps-table")).toBeInTheDocument());

    // The header checkbox selects all rows → the batch toolbar appears.
    const checkboxes = screen.getAllByRole("checkbox");
    await userEvent.click(checkboxes[0]);
    await waitFor(() => expect(screen.getByTestId("ps-batch-toolbar")).toBeInTheDocument());
    // GUARD: labels must resolve, not render raw i18n keys (regression #769).
    expect(screen.getByTestId("ps-batch-lock").textContent).not.toContain("platform_skills");

    await userEvent.click(screen.getByTestId("ps-batch-lock"));
    // One server-side batch call carrying the page selection as ``ids``.
    await waitFor(() => expect(batched.length).toBe(1));
    expect(batched[0].set_pinned).toBe(true);
    expect(batched[0].ids).toEqual(["psk-1", "psk-2"]);
    expect(batched[0].filter).toBeUndefined();
    // Toolbar clears after the batch completes.
    await waitFor(() =>
      expect(screen.queryByTestId("ps-batch-toolbar")).not.toBeInTheDocument(),
    );
  });

  it("batch-sets a category on the selected skills via the batch endpoint", async () => {
    const batched: Array<Record<string, unknown>> = [];
    installAdapter([
      ...listAndCategoryHandlers,
      {
        match: (u, m) => u.endsWith("/platform/skills/batch") && m === "post",
        respond: ({ data }) => {
          batched.push(typeof data === "string" ? JSON.parse(data) : (data as object));
          return raw({ updated: 2 });
        },
      },
    ]);
    renderPage(["system_admin"]);
    await waitFor(() => expect(screen.getByTestId("ps-table")).toBeInTheDocument());

    // Select all rows on the page (header checkbox) → the batch toolbar appears.
    const checkboxes = screen.getAllByRole("checkbox");
    await userEvent.click(checkboxes[0]);
    await waitFor(() => expect(screen.getByTestId("ps-batch-toolbar")).toBeInTheDocument());

    await userEvent.click(screen.getByTestId("ps-batch-set-category"));
    await userEvent.click(await screen.findByText("研发"));

    await waitFor(() => expect(batched.length).toBe(1));
    expect(batched[0].set_category).toBe("研发");
    expect(batched[0].ids).toEqual(["psk-1", "psk-2"]);
  });

  it("batch clear-category posts set_category as empty string", async () => {
    const batched: Array<Record<string, unknown>> = [];
    installAdapter([
      ...listAndCategoryHandlers,
      {
        match: (u, m) => u.endsWith("/platform/skills/batch") && m === "post",
        respond: ({ data }) => {
          batched.push(typeof data === "string" ? JSON.parse(data) : (data as object));
          return raw({ updated: 2 });
        },
      },
    ]);
    renderPage(["system_admin"]);
    await waitFor(() => expect(screen.getByTestId("ps-table")).toBeInTheDocument());

    const checkboxes = screen.getAllByRole("checkbox");
    await userEvent.click(checkboxes[0]);
    await waitFor(() => expect(screen.getByTestId("ps-batch-toolbar")).toBeInTheDocument());

    await userEvent.click(screen.getByTestId("ps-batch-set-category"));
    // Test env resolves i18n to "en" (jsdom navigator.language "en-US") → en.ts's
    // "Clear category" (see en.ts platform_skills.batch_clear_category).
    await userEvent.click(await screen.findByText("Clear category"));

    await waitFor(() => expect(batched.length).toBe(1));
    expect(batched[0].set_category).toBe("");
  });

  it("search input refetches the list with the q param (debounced)", async () => {
    const listParams: Array<Record<string, unknown>> = [];
    installAdapter([
      {
        match: (u, m) => u.endsWith("/platform/skills") && m === "get",
        respond: ({ params }) => {
          listParams.push(params ?? {});
          return raw({ items: [SKILL], total: 1 });
        },
      },
    ]);
    renderPage(["system_admin"]);
    await waitFor(() => expect(screen.getByTestId("ps-table")).toBeInTheDocument());
    listParams.length = 0; // ignore the initial load

    // antd ``Input.Search`` forwards data-testid to the underlying input.
    await userEvent.type(screen.getByTestId("ps-search"), "web");
    await waitFor(() => expect(listParams.some((p) => p.q === "web")).toBe(true));
  });

  it("pagination change refetches with the new offset", async () => {
    const listParams: Array<Record<string, unknown>> = [];
    const many = Array.from({ length: 20 }, (_, i) => ({
      ...SKILL,
      id: `psk-${i}`,
      name: `skill_${i}`,
    }));
    installAdapter([
      {
        match: (u, m) => u.endsWith("/platform/skills") && m === "get",
        respond: ({ params }) => {
          listParams.push(params ?? {});
          return raw({ items: many, total: 45 });
        },
      },
    ]);
    renderPage(["system_admin"]);
    await waitFor(() => expect(screen.getByTestId("ps-table")).toBeInTheDocument());
    listParams.length = 0;

    // Page 2 (default pageSize 20) → offset 20.
    await userEvent.click(screen.getByTitle("2"));
    await waitFor(() => expect(listParams.some((p) => p.offset === 20)).toBe(true));
  });

  it("'apply to all matching' posts a filter (not ids) to the batch endpoint", async () => {
    const batched: Array<Record<string, unknown>> = [];
    // total (5) > rows on page (2) AND a search is active → "all matching" shows.
    installAdapter([
      {
        match: (u, m) => u.endsWith("/platform/skills/batch") && m === "post",
        respond: ({ data }) => {
          batched.push(typeof data === "string" ? JSON.parse(data) : (data as object));
          return raw({ updated: 5 });
        },
      },
      {
        match: (u, m) => u.endsWith("/platform/skills") && m === "get",
        respond: () => raw({ items: [SKILL, SKILL2], total: 5 }),
      },
    ]);
    renderPage(["system_admin"]);
    await waitFor(() => expect(screen.getByTestId("ps-table")).toBeInTheDocument());

    // Activate a search so the "all matching" scope becomes available.
    await userEvent.type(screen.getByTestId("ps-search"), "web");

    // Select the page rows → toolbar appears.
    const checkboxes = screen.getAllByRole("checkbox");
    await userEvent.click(checkboxes[0]);
    await waitFor(() =>
      expect(screen.getByTestId("ps-batch-all-matching")).toBeInTheDocument(),
    );

    // Toggle "apply to all N matching" (antd forwards data-testid onto the
    // checkbox <input> itself), then archive.
    const allMatchingBox = screen.getByTestId("ps-batch-all-matching");
    await userEvent.click(allMatchingBox);
    await waitFor(() => expect(allMatchingBox).toBeChecked());
    await userEvent.click(screen.getByTestId("ps-batch-archive"));

    await waitFor(() => expect(batched.length).toBe(1));
    expect(batched[0].set_status).toBe("archived");
    expect(batched[0].ids).toBeUndefined();
    expect(batched[0].filter).toMatchObject({ q: "web" });
  });

  it("shows the guided empty state for an admin with no skills", async () => {
    installAdapter([
      {
        match: (u) => u.endsWith("/platform/skills"),
        respond: () => raw({ items: [], total: 0 }),
      },
    ]);
    renderPage(["system_admin"]);
    await waitFor(() => expect(screen.getByTestId("ps-empty")).toBeInTheDocument());
    // Empty state offers Import (Phase D: creation is import-only).
    expect(screen.getByTestId("ps-empty-import")).toBeInTheDocument();
  });

  it("renders the error Alert when the list fails", async () => {
    apiClient.defaults.adapter = (config) =>
      Promise.reject({
        isAxiosError: true,
        response: { status: 500, data: { detail: { code: "BOOM", message: "boom" } } },
        message: "boom",
        config,
      });
    renderPage(["system_admin"]);
    await waitFor(() => expect(screen.getByTestId("ps-error")).toBeInTheDocument());
  });

  it("creation is import-only — no hand-build entry points (Phase D)", async () => {
    installAdapter([
      {
        match: (u, m) => u.endsWith("/platform/skills") && m === "get",
        respond: () => raw({ items: [SKILL], total: 1 }),
      },
    ]);
    renderPage(["system_admin"]);
    await waitFor(() => expect(screen.getByTestId("ps-import-btn")).toBeInTheDocument());
    // The "New skill" hand-build drawer is removed.
    expect(screen.queryByTestId("ps-add")).not.toBeInTheDocument();
    expect(screen.queryByTestId("psc-form")).not.toBeInTheDocument();
  });

  it("import flow uploads a .skill ZIP and refreshes the list", async () => {
    let importPosted = false;
    let listCalls = 0;
    installAdapter([
      {
        match: (u, m) => u.endsWith("/platform/skills/import") && m === "post",
        respond: () => {
          importPosted = true;
          return raw({ skill: SKILL, version: VERSION, created: true });
        },
        status: 201,
      },
      {
        match: (u, m) => u.endsWith("/platform/skills") && m === "get",
        respond: () => {
          listCalls += 1;
          // First load empty; after import the refreshed list has the skill.
          return raw({ items: importPosted ? [SKILL] : [], total: importPosted ? 1 : 0 });
        },
      },
    ]);
    const user = userEvent.setup();
    renderPage(["system_admin"]);
    await waitFor(() => expect(screen.getByTestId("ps-import-btn")).toBeInTheDocument());

    const file = new File(["PK"], "web_search.skill", {
      type: "application/zip",
    });
    await user.upload(screen.getByTestId<HTMLInputElement>("ps-import-input"), file);

    await waitFor(() => expect(importPosted).toBe(true));
    // The import handler triggers a refresh ⇒ a second GET, now non-empty.
    await waitFor(() => expect(screen.getByText("web_search")).toBeInTheDocument());
    expect(listCalls).toBeGreaterThanOrEqual(2);
  });

  it("Manage navigates to the platform skill detail page (Phase C)", async () => {
    // The old in-place Manage drawer is retired; "Manage" now routes to the
    // full detail editor at /settings/platform-skills/:skillId.
    installAdapter([
      {
        match: (u, m) => u.endsWith("/platform/skills") && m === "get",
        respond: () => raw({ items: [SKILL], total: 1 }),
      },
    ]);
    setStoredToken(makeJwt({ sub: "u1", tenant_id: TENANT, roles: ["system_admin"] }));
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/settings/platform-skills"]}>
        <AuthProvider>
          <App>
            <Routes>
              <Route path="/settings/platform-skills" element={<SettingsPlatformSkills />} />
              <Route path="/settings/platform-skills/:skillId" element={<DetailProbe />} />
            </Routes>
          </App>
        </AuthProvider>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByTestId("ps-manage-psk-1")).toBeInTheDocument());
    await user.click(screen.getByTestId("ps-manage-psk-1"));
    await waitFor(() =>
      expect(screen.getByTestId("detail-probe").textContent).toBe(
        "/settings/platform-skills/psk-1",
      ),
    );
  });

  it("shows the skill description in the list", async () => {
    installAdapter([
      {
        match: (u, m) => u.endsWith("/platform/skills") && m === "get",
        respond: () => raw({ items: [SKILL], total: 1 }),
      },
      {
        match: (u) => u.endsWith("/platform/skills/categories"),
        respond: () => raw({ categories: ["web"] }),
      },
    ]);
    renderPage(["system_admin"]);
    await waitFor(() => expect(screen.getByTestId("ps-table")).toBeInTheDocument());
    expect(
      screen.getByText("Search the web and return top N results."),
    ).toBeInTheDocument();
  });

  it("category filter refetches with the category param", async () => {
    const calls: Array<Record<string, unknown> | undefined> = [];
    installAdapter([
      {
        match: (u, m) => u.endsWith("/platform/skills") && m === "get",
        respond: ({ params }) => {
          calls.push(params);
          return raw({ items: [SKILL], total: 1 });
        },
      },
      {
        match: (u) => u.endsWith("/platform/skills/categories"),
        respond: () => raw({ categories: ["web", "data"] }),
      },
    ]);
    const user = userEvent.setup();
    renderPage(["system_admin"]);
    await waitFor(() => expect(screen.getByTestId("ps-table")).toBeInTheDocument());

    // Open the category filter and pick "data".
    const filter = screen.getByTestId("ps-category-filter");
    await user.click(filter.querySelector(".ant-select-selector")!);
    await user.click(await screen.findByTitle("data"));

    await waitFor(() =>
      expect(calls.some((p) => p?.category === "data")).toBe(true),
    );
  });

  it("export-all button downloads the whole library as one zip", async () => {
    let exportAllCalls = 0;
    installAdapter([
      {
        match: (u, m) => u.endsWith("/platform/skills:export-all") && m === "get",
        respond: () => {
          exportAllCalls += 1;
          return new Blob(["zip"]);
        },
      },
      {
        match: (u, m) => u.endsWith("/platform/skills") && m === "get",
        respond: () => raw({ items: [SKILL], total: 1 }),
      },
      {
        match: (u) => u.endsWith("/platform/skills/categories"),
        respond: () => raw({ categories: [] }),
      },
    ]);
    (URL as unknown as { createObjectURL: () => string }).createObjectURL = vi.fn(
      () => "blob:mock",
    );
    (URL as unknown as { revokeObjectURL: (u: string) => void }).revokeObjectURL =
      vi.fn();
    const downloads: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      downloads.push(this.download);
    });

    renderPage(["system_admin"]);
    await waitFor(() =>
      expect(screen.getByTestId("ps-export-all-btn")).toBeInTheDocument(),
    );
    await userEvent.click(screen.getByTestId("ps-export-all-btn"));
    await waitFor(() => expect(exportAllCalls).toBe(1));
    expect(downloads).toEqual(["platform-skills.zip"]);
  });

  it("bulk import uploads the outer zip, lists per-pack results, refreshes", async () => {
    let batchPosted = false;
    let listCalls = 0;
    installAdapter([
      {
        match: (u, m) => u.endsWith("/platform/skills:import-batch") && m === "post",
        respond: () => {
          batchPosted = true;
          return raw({
            results: [
              { name: "alpha", status: "imported", version: 1 },
              { name: "beta", status: "skipped", version: 3 },
              { name: "gamma", status: "failed", reason: "not a valid zip" },
            ],
          });
        },
      },
      {
        match: (u, m) => u.endsWith("/platform/skills") && m === "get",
        respond: () => {
          listCalls += 1;
          return raw({ items: batchPosted ? [SKILL] : [], total: batchPosted ? 1 : 0 });
        },
      },
      {
        match: (u) => u.endsWith("/platform/skills/categories"),
        respond: () => raw({ categories: [] }),
      },
    ]);
    const user = userEvent.setup();
    renderPage(["system_admin"]);
    await waitFor(() =>
      expect(screen.getByTestId("ps-import-batch-btn")).toBeInTheDocument(),
    );

    const file = new File(["PK"], "platform-skills.zip", { type: "application/zip" });
    await user.upload(screen.getByTestId<HTMLInputElement>("ps-import-batch-input"), file);

    await waitFor(() => expect(batchPosted).toBe(true));
    // Per-pack result modal: one row per pack, failure carries its reason.
    await waitFor(() =>
      expect(screen.getByTestId("ps-bulk-results-modal")).toBeInTheDocument(),
    );
    const rows = screen.getByTestId("ps-bulk-results");
    expect(rows.children).toHaveLength(3);
    expect(rows).toHaveTextContent("alpha");
    expect(rows).toHaveTextContent("beta");
    expect(rows).toHaveTextContent("gamma");
    expect(rows).toHaveTextContent("not a valid zip");
    // The import triggered a list refresh (initial load + post-import).
    expect(listCalls).toBeGreaterThanOrEqual(2);
  });
});
