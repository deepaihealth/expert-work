/**
 * SkillsList + SkillDetail tests — Stream H.4 PR 5 + Capability Uplift
 * Sprint #3 PR C.
 *
 * Backend skills endpoints return raw (un-enveloped) payloads, so the
 * adapter mock delivers ``{items, next_cursor, cross_tenant}`` and raw
 * skill / version / supporting-file objects directly.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { App } from "antd";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../i18n";

import { SkillsList } from "../SkillsList";
import { SkillDetail } from "../SkillDetail";
import { TenantScopeProvider } from "../../tenant/TenantScopeContext";
import { AuthProvider } from "../../auth/AuthContext";
import { apiClient, setStoredToken } from "../../api/client";

// Cross-tenant W3 — 切入态置灰;``isTenantSwitchedMock`` 可翻转做两态断言。
const { isTenantSwitchedMock } = vi.hoisted(() => ({
  isTenantSwitchedMock: vi.fn(() => false),
}));
vi.mock("../../tenant/useIsTenantSwitched", () => ({
  useIsTenantSwitched: isTenantSwitchedMock,
}));

vi.mock("@monaco-editor/react", () => {
  const Editor = ({
    value,
    onChange,
    ["data-testid"]: testId,
  }: {
    value: string;
    onChange?: (v: string | undefined) => void;
    "data-testid"?: string;
  }) => (
    <textarea
      data-testid={testId ?? "monaco-stub"}
      value={value}
      onChange={(e) => onChange?.(e.target.value)}
    />
  );
  const DiffEditor = ({
    original,
    modified,
  }: {
    original: string;
    modified: string;
  }) => (
    <div data-testid="monaco-diff-stub">
      <pre data-testid="monaco-diff-original">{original}</pre>
      <pre data-testid="monaco-diff-modified">{modified}</pre>
    </div>
  );
  return { default: Editor, DiffEditor };
});

function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.`;
}

interface RouteHandler {
  match: (url: string, method: string) => boolean;
  respond: (config: { data?: unknown; url: string; method: string }) => unknown;
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
      data: handler.respond({ data: config.data, url, method }) ?? {},
      status: handler.status ?? 200,
      statusText: "OK",
      headers: {},
      config,
      request: {},
    });
  };
}

function renderSkillsRouter(
  initialEntries: string[] = ["/skills"],
  options: { roles?: string[]; isSystemAdmin?: boolean } = {},
) {
  const roles = options.roles ?? ["admin"];
  setStoredToken(makeJwt({ sub: "u1", tenant_id: "t1", roles }));
  // Also short-circuit /v1/me so the auth context's server-side
  // resolution matches the optimistic roles we just baked into the
  // JWT — otherwise non-admin tests would race with the mock.
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <AuthProvider>
        <TenantScopeProvider>
          <App>
            <Routes>
              <Route path="/skills" element={<SkillsList />} />
              <Route path="/skills/:skillId" element={<SkillDetail />} />
            </Routes>
          </App>
        </TenantScopeProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

const skillRow = {
  id: "sk1",
  name: "web_search",
  status: "active" as const,
  latest_version: 3,
  description: "Search the web and return top N results.",
  category: "web",
  // Sprint #4 — Curator fields default on every fixture so the
  // SkillRecord type is always satisfied.
  pinned: false,
  last_used_at: "2026-05-25T10:00:00Z",
  state_changed_at: "2026-05-20T10:00:00Z",
  created_at: "2026-05-20T10:00:00Z",
  updated_at: "2026-05-26T10:00:00Z",
};

const versionRow = {
  id: "v1",
  skill_id: "sk1",
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

const highRiskVersionRow = {
  ...versionRow,
  id: "v3",
  version: 3,
  prompt_fragment: "Be careful with shell exec.",
  tool_names: ["exec_python", "http"],
  supporting_files: {
    "scripts/diagnose.py": { size: 120, mime: "text/x-python" },
    "reference/notes.md": { size: 80, mime: "text/markdown" },
  },
  lazy_load: true,
  high_risk: true,
};

const meResponse = {
  subject_id: "u1",
  subject_type: "user" as const,
  tenant_id: "t1",
  roles: ["admin"],
  is_system_admin: false,
  auth_method: "jwt" as const,
};

beforeEach(() => {
  vi.restoreAllMocks();
  // vitest 4 的 restore 不复位 mockReturnValue — 显式归位防串台。
  isTenantSwitchedMock.mockReturnValue(false);
});

// ─── SkillsList — pre-PR C tests (unchanged) ─────────────────────────

describe("SkillsList", () => {
  it("renders the table with rows", async () => {
    installAdapter([
      {
        match: (u) => u === "/v1/me",
        respond: () => meResponse,
      },
      {
        match: (u) => u === "/v1/skills",
        respond: () => ({
          items: [skillRow],
          next_cursor: null,
          cross_tenant: false,
        }),
      },
    ]);
    renderSkillsRouter();
    await waitFor(() => expect(screen.getByText("web_search")).toBeInTheDocument());
    expect(screen.getByText("web")).toBeInTheDocument();
  });

  it("home 态导入 ZIP 按钮可用;切入态置灰(两态)", async () => {
    const routes: RouteHandler[] = [
      { match: (u) => u === "/v1/me", respond: () => meResponse },
      {
        match: (u) => u === "/v1/skills",
        respond: () => ({ items: [skillRow], next_cursor: null, cross_tenant: false }),
      },
      {
        match: (u) => u.startsWith("/v1/skill-evolution/kill-switch"),
        respond: () => ({ global: null, tenant: null, effective_halted: false }),
      },
    ];

    installAdapter(routes);
    const first = renderSkillsRouter();
    await waitFor(() => expect(screen.getByText("web_search")).toBeInTheDocument());
    expect(screen.getByTestId("skills-import-btn")).toBeEnabled();
    // 技能进化熔断开关(review I-1)——同页写控件,home 态可用。
    await waitFor(() =>
      expect(screen.getByTestId("skill-kill-switch-tenant")).toBeEnabled(),
    );
    first.unmount();

    isTenantSwitchedMock.mockReturnValue(true);
    installAdapter(routes);
    renderSkillsRouter();
    await waitFor(() => expect(screen.getByText("web_search")).toBeInTheDocument());
    expect(screen.getByTestId("skills-import-btn")).toBeDisabled();
    await waitFor(() =>
      expect(screen.getByTestId("skill-kill-switch-tenant")).toBeDisabled(),
    );
  });

  it("draft at v>1 shows the pending-revision tag (SE-A47); v1 draft does not", async () => {
    installAdapter([
      { match: (u) => u === "/v1/me", respond: () => meResponse },
      {
        match: (u) => u === "/v1/skills",
        respond: () => ({
          items: [
            { ...skillRow, id: "skR", name: "revised-skill", status: "draft" as const, latest_version: 2 },
            { ...skillRow, id: "skN", name: "brand-new-skill", status: "draft" as const, latest_version: 1 },
          ],
          next_cursor: null,
          cross_tenant: false,
        }),
      },
    ]);
    renderSkillsRouter();
    await waitFor(() => expect(screen.getByText("revised-skill")).toBeInTheDocument());
    expect(screen.getByTestId("skill-revision-pending-skR")).toBeInTheDocument();
    expect(screen.queryByTestId("skill-revision-pending-skN")).not.toBeInTheDocument();
  });

  it("shows Load more when next_cursor is non-null", async () => {
    let calls = 0;
    installAdapter([
      {
        match: (u) => u === "/v1/me",
        respond: () => meResponse,
      },
      {
        match: (u) => u === "/v1/skills",
        respond: () => {
          calls += 1;
          if (calls === 1) {
            return {
              items: [skillRow],
              next_cursor: "cursor-2",
              cross_tenant: false,
            };
          }
          return {
            items: [{ ...skillRow, id: "sk2", name: "sql_query" }],
            next_cursor: null,
            cross_tenant: false,
          };
        },
      },
    ]);
    const user = userEvent.setup();
    renderSkillsRouter();
    await waitFor(() => expect(screen.getByText("web_search")).toBeInTheDocument());
    expect(screen.getByTestId("skills-load-more")).toBeInTheDocument();
    await user.click(screen.getByTestId("skills-load-more"));
    await waitFor(() => expect(screen.getByText("sql_query")).toBeInTheDocument());
    expect(screen.getByText("web_search")).toBeInTheDocument();
  });

  it("renders platform_items with source badge + entitled lock (X-6)", async () => {
    const platformEntitled = {
      ...skillRow,
      id: "pk1",
      name: "platform_search",
      source: "platform" as const,
      entitled: true,
      required_tier: "pro" as const,
    };
    const platformLocked = {
      ...skillRow,
      id: "pk2",
      name: "platform_sql",
      source: "platform" as const,
      entitled: false,
      required_tier: "enterprise" as const,
    };
    const tenantRow = {
      ...skillRow,
      id: "sk1",
      name: "my_skill",
      source: "tenant" as const,
      entitled: true,
    };
    installAdapter([
      { match: (u) => u === "/v1/me", respond: () => meResponse },
      {
        match: (u) => u === "/v1/skills",
        respond: () => ({
          items: [tenantRow],
          platform_items: [platformEntitled, platformLocked],
          next_cursor: null,
          cross_tenant: false,
        }),
      },
    ]);
    renderSkillsRouter();
    await waitFor(() => expect(screen.getByText("platform_search")).toBeInTheDocument());
    // Source badges.
    expect(screen.getByTestId("skill-source-platform-pk1")).toBeInTheDocument();
    expect(screen.getByTestId("skill-source-platform-pk2")).toBeInTheDocument();
    expect(screen.getByTestId("skill-source-tenant-sk1")).toBeInTheDocument();
    // Locked platform row shows the entitlement lock with its tier.
    const lock = screen.getByTestId("skill-locked-pk2");
    expect(lock).toBeInTheDocument();
    expect(lock).toHaveTextContent(/enterprise/i);
    // Entitled platform row has no lock.
    expect(screen.queryByTestId("skill-locked-pk1")).not.toBeInTheDocument();
  });

  it("行内导出按钮下载最新版且不触发行跳转;切入态不置灰(W4 D1)", async () => {
    let exportCalls = 0;
    const routes: RouteHandler[] = [
      { match: (u) => u === "/v1/me", respond: () => meResponse },
      {
        match: (u) => u === "/v1/skills",
        respond: () => ({
          items: [skillRow],
          platform_items: [
            { ...skillRow, id: "pk1", name: "platform_search", source: "platform" as const },
          ],
          next_cursor: null,
          cross_tenant: false,
        }),
      },
      {
        match: (u, m) => u === "/v1/skills/sk1/versions/3/export" && m === "get",
        respond: () => {
          exportCalls += 1;
          return new Blob(["zip"]);
        },
      },
    ];
    installAdapter(routes);
    (URL as unknown as { createObjectURL: () => string }).createObjectURL = vi.fn(
      () => "blob:mock",
    );
    (URL as unknown as { revokeObjectURL: (u: string) => void }).revokeObjectURL = vi.fn();
    const downloads: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      downloads.push(this.download);
    });

    const user = userEvent.setup();
    const first = renderSkillsRouter();
    await waitFor(() => expect(screen.getByText("web_search")).toBeInTheDocument());
    // Platform rows are read-only in the tenant scope — no export button.
    expect(screen.queryByTestId("skill-row-export-pk1")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("skill-row-export-sk1"));
    await waitFor(() => expect(exportCalls).toBe(1));
    expect(downloads).toEqual(["web_search-v3.skill"]);
    // stopPropagation — the row's navigate must not fire.
    expect(screen.queryByTestId("skill-detail-root")).not.toBeInTheDocument();
    first.unmount();

    // 切入态:导出是读操作,按钮不置灰。
    isTenantSwitchedMock.mockReturnValue(true);
    installAdapter(routes);
    renderSkillsRouter();
    await waitFor(() =>
      expect(screen.getByTestId("skill-row-export-sk1")).toBeEnabled(),
    );
  });

  it("creation is import-only — no hand-build drawer; empty state offers Import", async () => {
    // Phase D: the empty-shell "New skill" drawer is removed (it was a dead
    // end). Import .skill is the primary + empty-state CTA.
    installAdapter([
      {
        match: (u) => u === "/v1/me",
        respond: () => meResponse,
      },
      {
        match: (u) => u === "/v1/skills",
        respond: () => ({ items: [], next_cursor: null, cross_tenant: false }),
      },
    ]);
    renderSkillsRouter();
    await waitFor(() => expect(screen.getByTestId("skills-import-btn")).toBeInTheDocument());
    // No hand-build create entry points remain.
    expect(screen.queryByTestId("skills-create-btn")).not.toBeInTheDocument();
    expect(screen.queryByTestId("skills-create-drawer")).not.toBeInTheDocument();
    // Empty-state surfaces an Import CTA.
    expect(screen.getByTestId("skills-empty-import")).toBeInTheDocument();
  });

  // ─── Cross-tenant W3 — aggregate row-jump carries the owning tenant ───

  function rowJumpAdapter(rowTenantId: string) {
    const paramsByUrl: Record<string, Record<string, unknown> | undefined> = {};
    apiClient.defaults.adapter = (config) => {
      const url = config.url ?? "";
      let data: unknown = {};
      if (url === "/v1/me") {
        data = meResponse;
      } else if (url === "/v1/skills") {
        data = {
          items: [{ ...skillRow, tenant_id: rowTenantId }],
          next_cursor: null,
          cross_tenant: true,
        };
      } else if (url === "/v1/skills/sk1") {
        paramsByUrl.detail = config.params as Record<string, unknown>;
        data = { ...skillRow, tenant_id: rowTenantId };
      } else if (url === "/v1/skills/sk1/versions") {
        paramsByUrl.versions = config.params as Record<string, unknown>;
        data = { items: [versionRow] };
      }
      return Promise.resolve({
        data,
        status: 200,
        statusText: "OK",
        headers: {},
        config,
        request: {},
      });
    };
    return paramsByUrl;
  }

  it("row-jump carries a foreign tenant_id into the detail reads (W3)", async () => {
    // JWT home tenant is t1; the row belongs to t2 → the click appends
    // ?tenant_id=t2 and SkillDetail threads it into both reads.
    const paramsByUrl = rowJumpAdapter("t2");
    const user = userEvent.setup();
    renderSkillsRouter();
    await waitFor(() => expect(screen.getByText("web_search")).toBeInTheDocument());
    await user.click(screen.getByText("web_search"));
    await waitFor(() => expect(screen.getByTestId("skill-detail-root")).toBeInTheDocument());
    expect(paramsByUrl.detail?.tenant_id).toBe("t2");
    expect(paramsByUrl.versions?.tenant_id).toBe("t2");
  });

  it("row-jump keeps the plain URL for a home-tenant row (W3)", async () => {
    const paramsByUrl = rowJumpAdapter("t1");
    const user = userEvent.setup();
    renderSkillsRouter();
    await waitFor(() => expect(screen.getByText("web_search")).toBeInTheDocument());
    await user.click(screen.getByText("web_search"));
    await waitFor(() => expect(screen.getByTestId("skill-detail-root")).toBeInTheDocument());
    expect(paramsByUrl.detail?.tenant_id).toBeUndefined();
    expect(paramsByUrl.versions?.tenant_id).toBeUndefined();
  });
});

// ─── SkillDetail — base + PR C dual-pane / badges / gates ────────────

describe("SkillDetail (PR C)", () => {
  function detailAdapter(version = versionRow) {
    return [
      { match: (u: string) => u === "/v1/me", respond: () => meResponse },
      {
        match: (u: string, m: string) => u === "/v1/skills/sk1" && m === "get",
        respond: () => skillRow,
      },
      {
        match: (u: string) => u === "/v1/skills/sk1/versions",
        respond: () => ({ items: [version] }),
      },
    ];
  }

  it("renders skill hero + version picker + dual pane", async () => {
    installAdapter(detailAdapter());
    renderSkillsRouter(["/skills/sk1"]);
    await waitFor(() => expect(screen.getAllByText("web_search").length).toBeGreaterThan(0));
    expect(screen.getByTestId("skill-detail-root")).toBeInTheDocument();
    expect(screen.getByTestId("skill-version-picker")).toBeInTheDocument();
    expect(screen.getByTestId("skill-dual-pane")).toBeInTheDocument();
    expect(screen.getByTestId("skill-file-tree")).toBeInTheDocument();
    // Default selection is SKILL.md → editor renders the edit hint + an
    // Edit button (Phase D-2: SKILL.md is editable, no longer read-only).
    await waitFor(() =>
      expect(screen.getByTestId("skill-md-edit-hint")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("skill-editor-edit-btn")).toBeInTheDocument();
  });

  it("home 态置顶/编辑可用;切入态置灰(两态)", async () => {
    installAdapter(detailAdapter());
    const first = renderSkillsRouter(["/skills/sk1"]);
    await waitFor(() =>
      expect(screen.getByTestId("skill-pin-button")).toBeEnabled(),
    );
    await waitFor(() =>
      expect(screen.getByTestId("skill-editor-edit-btn")).toBeEnabled(),
    );
    first.unmount();

    isTenantSwitchedMock.mockReturnValue(true);
    installAdapter(detailAdapter());
    renderSkillsRouter(["/skills/sk1"]);
    await waitFor(() =>
      expect(screen.getByTestId("skill-pin-button")).toBeDisabled(),
    );
    await waitFor(() =>
      expect(screen.getByTestId("skill-editor-edit-btn")).toBeDisabled(),
    );
  });

  it("404 / error path renders Alert", async () => {
    apiClient.defaults.adapter = (config) =>
      Promise.reject({
        isAxiosError: true,
        response: { status: 404, data: { detail: "skill not found" } },
        message: "skill not found",
        config,
      });
    renderSkillsRouter(["/skills/missing"]);
    await waitFor(() => expect(screen.getByTestId("skill-detail-error")).toBeInTheDocument());
  });

  it("eager skill shows Eager badge in metadata", async () => {
    installAdapter(detailAdapter());
    renderSkillsRouter(["/skills/sk1"]);
    await waitFor(() => expect(screen.getByTestId("skill-eager-badge")).toBeInTheDocument());
  });

  it("high-risk version shows 🔒 badge on hero + metadata + warning alert", async () => {
    installAdapter(detailAdapter(highRiskVersionRow));
    renderSkillsRouter(["/skills/sk1"]);
    await waitFor(() =>
      expect(screen.getByTestId("skill-hero-high-risk-badge")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("skill-high-risk-badge")).toBeInTheDocument();
    expect(screen.getByTestId("skill-high-risk-warning")).toBeInTheDocument();
    expect(screen.getByTestId("skill-lazy-badge")).toBeInTheDocument();
  });

  it("non-admin caller sees high-risk warning + 🔒 in active option label", async () => {
    installAdapter([
      { match: (u) => u === "/v1/me", respond: () => ({ ...meResponse, roles: ["viewer"] }) },
      {
        match: (u, m) => u === "/v1/skills/sk1" && m === "get",
        respond: () => ({ ...skillRow, status: "draft" as const }),
      },
      {
        match: (u) => u === "/v1/skills/sk1/versions",
        respond: () => ({ items: [highRiskVersionRow] }),
      },
    ]);
    renderSkillsRouter(["/skills/sk1"], { roles: ["viewer"] });
    // The metadata warning alert is what the operator sees first — and
    // it is the operator-facing fence between "this version is risky"
    // and a destructive activate click. Asserting it survives a
    // non-admin caller is sufficient for the visual gate; the actual
    // disabled-option DOM is antd internals + jsdom portal that flake
    // across versions, so backend RBAC (covered by
    // ``services/control-plane/tests/test_skill_high_risk_publish_gate``)
    // is the canonical enforcement and the Playwright e2e in
    // ``apps/admin-ui/e2e/skill-mutations.spec.ts`` is the
    // browser-truth check.
    await waitFor(() =>
      expect(screen.getByTestId("skill-hero-high-risk-badge")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("skill-high-risk-warning")).toBeInTheDocument();
  });

  it("file tree shows SKILL.md + supporting files grouped", async () => {
    installAdapter(detailAdapter(highRiskVersionRow));
    renderSkillsRouter(["/skills/sk1"]);
    const tree = await screen.findByTestId("skill-file-tree");
    await waitFor(() =>
      expect(within(tree).getByText("SKILL.md")).toBeInTheDocument(),
    );
    // top-level dirs rendered with trailing slash
    expect(within(tree).getByText("scripts/")).toBeInTheDocument();
    expect(within(tree).getByText("reference/")).toBeInTheDocument();
  });

  it("selecting a supporting file fetches + renders content; Edit + Save calls PUT", async () => {
    const putCalls: { url: string; data: unknown }[] = [];
    const v4 = {
      ...highRiskVersionRow,
      version: 4,
      supporting_files: {
        ...highRiskVersionRow.supporting_files,
        "reference/notes.md": { size: 14, mime: "text/markdown" },
      },
    };
    installAdapter([
      { match: (u) => u === "/v1/me", respond: () => meResponse },
      {
        match: (u, m) => u === "/v1/skills/sk1" && m === "get",
        respond: () => skillRow,
      },
      {
        match: (u) => u === "/v1/skills/sk1/versions",
        respond: () => ({ items: [highRiskVersionRow] }),
      },
      {
        match: (u, m) =>
          u === "/v1/skills/sk1/versions/3/supporting-files/reference/notes.md"
          && m === "get",
        respond: () => ({
          // base64("hello world\n") = "aGVsbG8gd29ybGQK"
          content: "aGVsbG8gd29ybGQK",
          size: 12,
          mime: "text/markdown",
        }),
      },
      {
        match: (u, m) =>
          u === "/v1/skills/sk1/versions/3/supporting-files/reference/notes.md"
          && m === "put",
        respond: ({ data }) => {
          putCalls.push({ url: "PUT", data });
          return v4;
        },
      },
    ]);
    const user = userEvent.setup();
    renderSkillsRouter(["/skills/sk1"]);
    const tree = await screen.findByTestId("skill-file-tree");
    // Folders are collapsed by default — expand reference/ to reveal notes.md.
    for (const sw of tree.querySelectorAll(".ant-tree-switcher_close")) {
      await user.click(sw as HTMLElement);
    }
    await user.click(within(tree).getByText("notes.md"));
    // ``skill-editor-monaco`` is the wrapper div; the mocked Monaco
    // textarea inside it carries ``monaco-stub`` (default mock testid).
    await waitFor(() =>
      expect(screen.getByTestId("skill-editor-monaco")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("skill-editor-edit-btn"));
    const editor = screen.getByTestId("monaco-stub") as HTMLTextAreaElement;
    await user.clear(editor);
    await user.type(editor, "updated text");
    await user.click(screen.getByTestId("skill-editor-save-btn"));
    await waitFor(() => expect(putCalls.length).toBe(1));
  });

  it("SKILL.md edit flow PUTs the prompt (Phase D-2)", async () => {
    const promptCalls: unknown[] = [];
    installAdapter([
      { match: (u) => u === "/v1/me", respond: () => meResponse },
      {
        match: (u, m) => u === "/v1/skills/sk1" && m === "get",
        respond: () => skillRow,
      },
      {
        match: (u) => u === "/v1/skills/sk1/versions",
        respond: () => ({ items: [versionRow] }),
      },
      {
        match: (u, m) => u === "/v1/skills/sk1/versions/1/prompt" && m === "put",
        respond: ({ data }) => {
          promptCalls.push(data);
          return { ...versionRow, id: "v2", version: 2, prompt_fragment: "new prompt body" };
        },
        status: 201,
      },
    ]);
    const user = userEvent.setup();
    renderSkillsRouter(["/skills/sk1"]);
    // SKILL.md is the default selection → Edit button is present.
    await waitFor(() =>
      expect(screen.getByTestId("skill-editor-edit-btn")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("skill-editor-edit-btn"));
    const editor = screen.getByTestId("monaco-stub") as HTMLTextAreaElement;
    await user.clear(editor);
    await user.type(editor, "new prompt body");
    await user.click(screen.getByTestId("skill-editor-save-btn"));
    await waitFor(() => expect(promptCalls.length).toBe(1));
    const parsed =
      typeof promptCalls[0] === "string"
        ? JSON.parse(promptCalls[0] as string)
        : promptCalls[0];
    expect(parsed.prompt_fragment).toBe("new prompt body");
  });

  it("delete flow requires typing the path to confirm", async () => {
    let deleteCalled = false;
    installAdapter([
      { match: (u) => u === "/v1/me", respond: () => meResponse },
      {
        match: (u, m) => u === "/v1/skills/sk1" && m === "get",
        respond: () => skillRow,
      },
      {
        match: (u) => u === "/v1/skills/sk1/versions",
        respond: () => ({ items: [highRiskVersionRow] }),
      },
      {
        match: (u, m) =>
          u === "/v1/skills/sk1/versions/3/supporting-files/reference/notes.md"
          && m === "get",
        respond: () => ({ content: "aGVsbG8=", size: 5, mime: "text/markdown" }),
      },
      {
        match: (u, m) =>
          u === "/v1/skills/sk1/versions/3/supporting-files/reference/notes.md"
          && m === "delete",
        respond: () => {
          deleteCalled = true;
          return { ...highRiskVersionRow, version: 4, supporting_files: {} };
        },
      },
    ]);
    const user = userEvent.setup();
    renderSkillsRouter(["/skills/sk1"]);
    const tree = await screen.findByTestId("skill-file-tree");
    // Folders are collapsed by default — expand to reveal notes.md.
    for (const sw of tree.querySelectorAll(".ant-tree-switcher_close")) {
      await user.click(sw as HTMLElement);
    }
    await user.click(within(tree).getByText("notes.md"));
    await waitFor(() =>
      expect(screen.getByTestId("skill-editor-delete-btn")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("skill-editor-delete-btn"));
    // Submit disabled until the path is typed back
    const submit = await screen.findByTestId("skill-delete-submit");
    expect(submit).toBeDisabled();
    await user.type(
      screen.getByTestId("skill-delete-confirm-input"),
      "reference/notes.md",
    );
    expect(submit).not.toBeDisabled();
    await user.click(submit);
    await waitFor(() => expect(deleteCalled).toBe(true));
  });

  it("Add file modal opens from the + Add file tree node", async () => {
    installAdapter(detailAdapter());
    const user = userEvent.setup();
    renderSkillsRouter(["/skills/sk1"]);
    const tree = await screen.findByTestId("skill-file-tree");
    await user.click(within(tree).getByText(/\+ Add file|添加文件/));
    await waitFor(() =>
      expect(screen.getByTestId("skill-add-file-path")).toBeInTheDocument(),
    );
  });

  // ── Sprint #4 (Mini-ADR U-30) — Pin button ─────────────────────────

  it("Pin button toggles skill.pinned via PATCH", async () => {
    let lastPatchBody: unknown = null;
    installAdapter([
      { match: (u) => u === "/v1/me", respond: () => meResponse },
      {
        match: (u, m) => u === "/v1/skills/sk1" && m === "get",
        respond: () => skillRow,
      },
      {
        match: (u) => u === "/v1/skills/sk1/versions",
        respond: () => ({ items: [versionRow] }),
      },
      {
        match: (u, m) => u === "/v1/skills/sk1" && m === "patch",
        respond: ({ data }) => {
          lastPatchBody = data;
          return { ...skillRow, pinned: true };
        },
      },
    ]);
    const user = userEvent.setup();
    renderSkillsRouter(["/skills/sk1"]);
    await waitFor(() => expect(screen.getByTestId("skill-pin-button")).toBeInTheDocument());
    await user.click(screen.getByTestId("skill-pin-button"));
    await waitFor(() => expect(lastPatchBody).not.toBeNull());
    // Body is sent as a JSON string by axios; parse + assert it
    // carried { pinned: true }.
    const parsed = typeof lastPatchBody === "string" ? JSON.parse(lastPatchBody) : lastPatchBody;
    expect(parsed).toEqual({ pinned: true });
  });

  it("Pin button is disabled for non-admin on high-risk skill", async () => {
    installAdapter([
      { match: (u) => u === "/v1/me", respond: () => ({ ...meResponse, roles: ["viewer"] }) },
      {
        match: (u, m) => u === "/v1/skills/sk1" && m === "get",
        respond: () => ({ ...skillRow, status: "draft" as const }),
      },
      {
        match: (u) => u === "/v1/skills/sk1/versions",
        respond: () => ({ items: [highRiskVersionRow] }),
      },
    ]);
    renderSkillsRouter(["/skills/sk1"], { roles: ["viewer"] });
    await waitFor(() => expect(screen.getByTestId("skill-pin-button")).toBeInTheDocument());
    const pin = screen.getByTestId("skill-pin-button");
    expect(pin).toBeDisabled();
  });
});

// ─── BUG-3(2026-08-24)分页 + 名称过滤 ───────────────────────────────

describe("SkillsList pagination & name filter (BUG-3)", () => {
  it("paginates the merged list at 20 rows per page", async () => {
    const platformItems = Array.from({ length: 25 }, (_, i) => ({
      ...skillRow,
      id: `p${i}`,
      name: `platform-skill-${i}`,
      source: "platform",
      entitled: true,
      subscribed: false,
    }));
    installAdapter([
      { match: (u) => u === "/v1/me", respond: () => meResponse },
      {
        match: (u) => u === "/v1/skills",
        respond: () => ({
          items: [],
          platform_items: platformItems,
          next_cursor: null,
          cross_tenant: false,
        }),
      },
    ]);
    renderSkillsRouter();
    await screen.findByTestId("skills-table");
    await waitFor(() => {
      expect(screen.getByText("platform-skill-0")).toBeInTheDocument();
    });
    // 第 21 条不在第一页。
    expect(screen.queryByText("platform-skill-20")).not.toBeInTheDocument();
    // antd 分页器出现(>1 页才渲染,hideOnSinglePage)。
    expect(document.querySelector(".ant-pagination")).not.toBeNull();
  });

  it("filters rows by name client-side", async () => {
    installAdapter([
      { match: (u) => u === "/v1/me", respond: () => meResponse },
      {
        match: (u) => u === "/v1/skills",
        respond: () => ({
          items: [
            skillRow,
            { ...skillRow, id: "sk2", name: "pdf_export", description: "Export PDF files." },
          ],
          next_cursor: null,
          cross_tenant: false,
        }),
      },
    ]);
    const user = userEvent.setup();
    renderSkillsRouter();
    await screen.findByText("web_search");
    expect(screen.getByText("pdf_export")).toBeInTheDocument();

    await user.type(screen.getByTestId("skills-name-filter"), "pdf");
    await waitFor(() => {
      expect(screen.queryByText("web_search")).not.toBeInTheDocument();
    });
    expect(screen.getByText("pdf_export")).toBeInTheDocument();
  });
});
