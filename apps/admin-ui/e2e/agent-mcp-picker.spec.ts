/**
 * Agent-form MCP picker E2E — Stream V-G, selectors migrated to the
 * group-nav + detail-pane layout (agent-config-page redesign PR1).
 *
 * Proves an admin can:
 *   (a) open the Create-Agent modal, enable the MCP toggle, see the
 *       server checkbox list (from ``GET /v1/mcp-servers/available``),
 *       check ``github``, expand its tool collapse
 *       (``GET /v1/mcp-servers/github/tools``), check ``create_issue``,
 *       submit — and the ``POST /v1/agents`` body contains
 *       ``tools: [{type:"mcp", servers:["github"], allow_tools:["create_issue"]}]``.
 *   (b) the open form with the MCP picker passes the axe a11y audit.
 *
 * Mirrors ``manifest-editor.spec.ts``:
 *   - same login flow (SAMPLE_JWT, login-card, optional login-dev-toggle)
 *   - same schema stub registered after the fixture's agents glob
 *     so it wins (LIFO ordering)
 *   - same embedding-config stub
 *   - ``page.route`` mocks for ``/v1/mcp-servers/available`` and
 *     ``/v1/mcp-servers/github/tools``
 *   - POST body intercepted via ``page.waitForRequest`` (mirrors the PUT
 *     intercept in ``manifest-edit.spec.ts``)
 *
 * The shared ``installControlPlaneStub`` fixture auto-registers
 * ``**​/v1/agents*`` (returns a list with customer-support-bot) so the
 * Agents page renders.  We do NOT assert on that agent name — the test
 * only needs the Create button to appear.
 *
 * For the POST we register a ``**​/v1/agents`` route that intercepts only
 * POST requests and calls ``route.fallback()`` for everything else so the
 * fixture's broader glob still handles the GET list.
 */
import { test, expect, expectNoA11yViolations, SAMPLE_JWT } from "./fixtures";

// ── Stub data ─────────────────────────────────────────────────────────────

const SCHEMA_ENVELOPE = {
  success: true,
  error: null,
  data: {
    type: "object",
    properties: {
      metadata: {
        type: "object",
        properties: {
          name: { type: "string", title: "Name" },
        },
      },
    },
  },
};

const CATALOG_ENVELOPE = {
  success: true,
  error: null,
  data: {
    providers: [
      {
        provider: "openai",
        models: [
          {
            name: "gpt-5.5",
            vision: true,
            embeddings: false,
            context_window: 128000,
            deprecated: false,
          },
        ],
      },
    ],
  },
};

const AVAILABLE_SERVERS = {
  success: true,
  data: [
    { name: "github", source: "tenant", enabled: true },
    { name: "fs", source: "platform", enabled: true },
  ],
  error: null,
};

// B-61 —— 这两个桩工具带上 input_schema,子弹窗里的「参数绑定」界面才会渲染。
// 不带的话,新增的 <label htmlFor> + Select id + 展开按钮那一层在 e2e / axe 里
// 从来不会出现,等于裸着上船(复评 L3)。
const GITHUB_TOOLS = {
  success: true,
  data: [
    {
      name: "create_issue",
      description: "Create a new GitHub issue",
      input_schema: {
        type: "object",
        properties: { repo: {}, title: {}, body: {} },
        required: ["repo", "title"],
      },
    },
    {
      name: "list_repos",
      description: "List repositories",
      input_schema: { type: "object", properties: { owner: {} } },
    },
  ],
  error: null,
};

// POST /v1/agents returns a minimal success envelope so the modal closes.
const CREATE_AGENT_OK = {
  success: true,
  data: {
    record: {
      id: "aaaaaaaa-0000-0000-0000-000000000099",
      tenant_id: "22222222-2222-2222-2222-222222222222",
      name: "mcp-agent",
      version: "1.0.0",
      status: "active",
      spec_sha256: "a".repeat(64),
      created_by: "alice@acme.com",
      created_at: "2026-06-03T10:00:00Z",
      updated_at: "2026-06-03T10:00:00Z",
      spec: {},
    },
  },
  error: null,
};

// ── Common route/login setup ───────────────────────────────────────────────

test.beforeEach(async ({ page }) => {
  // Schema stub — registered after the fixture's ``**/v1/agents*`` glob
  // so it takes precedence (LIFO).
  await page.route("**/v1/agents/schema", async (route) => {
    await route.fulfill({ json: SCHEMA_ENVELOPE });
  });

  // Model catalog — needed by the form's ModelSelect field on open.
  await page.route("**/v1/model-catalog", async (route) => {
    await route.fulfill({ json: CATALOG_ENVELOPE });
  });

  // Embedding-status stub — modal renders the editor only when configured.
  await page.route("**/v1/platform/embedding-config/status", (route) =>
    route.fulfill({
      json: { success: true, data: { configured: true }, error: null },
    }),
  );

  // MCP available-servers list.
  await page.route("**/v1/mcp-servers/available", async (route) => {
    await route.fulfill({ json: AVAILABLE_SERVERS });
  });

  // github tools (also catches any other server name via the wildcard).
  await page.route("**/v1/mcp-servers/github/tools", async (route) => {
    await route.fulfill({ json: GITHUB_TOOLS });
  });

  // POST /v1/agents — stub so the modal can close after submit.
  // Uses route.fallback() for non-POST requests so the fixture's broader
  // ``**/v1/agents*`` GET stub still handles the agents-list fetch.
  await page.route("**/v1/agents", async (route) => {
    if (route.request().method() === "POST") {
      await route.fulfill({ json: CREATE_AGENT_OK });
      return;
    }
    await route.fallback();
  });

  // Login.
  await page.goto("/login");
  await expect(page.getByTestId("login-card")).toBeVisible();
  const tokenField = page.getByTestId("login-token");
  if (!(await tokenField.isVisible())) {
    await page.getByTestId("login-dev-toggle").click();
  }
  await tokenField.fill(SAMPLE_JWT);
  await page.getByTestId("login-submit").click();
  await expect(page).toHaveURL(/\/agents$/);
  // Wait for the agents page to settle (list rendered) — assert on the
  // create button, not a specific agent name, so no pre-existing agent is
  // required.
  await expect(page.getByTestId("agents-create")).toBeVisible();
});

// ── Tests ─────────────────────────────────────────────────────────────────

test("(a) create-agent: enable MCP, pick server+tool, submit — POST body contains mcp entry", async ({
  page,
}) => {
  // Open the Create Agent modal.
  await page.getByTestId("agents-create").click();
  await expect(page.getByTestId("create-agent-modal")).toBeVisible();
  await expect(page.getByTestId("manifest-form-view")).toBeVisible();

  // Give the agent a name so the manifest is valid enough for the backend stub.
  const nameInput = page.getByTestId("af-name").locator("input");
  await nameInput.clear();
  await nameInput.fill("mcp-agent");

  // MCP lives behind the capabilities group's [MCP] sub-tab (config-page
  // redesign v2, Task 5) — open the group, then the tab; the server list
  // shows there and selecting a server enables MCP.
  await page.getByTestId("cfg-nav-capabilities").click();
  await page.getByRole("tab", { name: "MCP" }).click();

  // The McpToolPicker mounts and fetches /v1/mcp-servers/available.
  await expect(page.getByTestId("af-mcp-server-github")).toBeVisible();
  await expect(page.getByTestId("af-mcp-server-fs")).toBeVisible();

  // Check the github server (= enable MCP with github selected).
  await page.getByTestId("af-mcp-server-github").click();

  // Click the gear to open the tool-selection sub-modal.
  await page.getByTestId("af-mcp-choose-github").click();

  // In the sub-modal: wait for + check the create_issue tool, then close it.
  await expect(page.getByTestId("af-mcp-tool-create_issue")).toBeVisible();
  await page.getByTestId("af-mcp-tool-create_issue").click();
  await page
    .getByTestId("af-mcp-tool-modal")
    .getByRole("button", { name: /完成|Done/ })
    .click();

  // Intercept the POST and grab its body.
  const postPromise = page.waitForRequest(
    (req) => req.method() === "POST" && req.url().includes("/v1/agents"),
  );

  // Submit.
  await page.getByTestId("create-agent-submit").click();

  const postReq = await postPromise;
  const body = postReq.postDataJSON() as { manifest_yaml: string };

  // The manifest is submitted as YAML.  Parse the tools list from it.
  // We assert structurally: the YAML must contain the mcp entry with
  // servers: [github] and allow_tools: [create_issue].
  const yaml = body.manifest_yaml;
  expect(yaml).toContain("type: mcp");
  expect(yaml).toContain("github");
  expect(yaml).toContain("create_issue");
});

test("(b) create modal with MCP picker passes axe (serious + critical)", async ({
  page,
}) => {
  await page.getByTestId("agents-create").click();
  await expect(page.getByTestId("manifest-form-view")).toBeVisible();

  // MCP lives behind the capabilities group's [MCP] sub-tab (config-page
  // redesign v2, Task 5).
  await page.getByTestId("cfg-nav-capabilities").click();
  await page.getByRole("tab", { name: "MCP" }).click();

  // Wait for the picker to load so axe sees the full DOM.
  await expect(page.getByTestId("af-mcp-server-github")).toBeVisible();

  await expectNoA11yViolations(page, "create-agent-modal-mcp");
});

// B-61 —— 绑定编辑器(参数名 <label htmlFor> + antd Select 的 id + 展开按钮的
// aria-expanded/aria-controls)是这次新增的一层 markup,而唯一会打开工具子弹窗的
// 用例 (a) 之前用的桩工具没有 input_schema,所以这层在 Playwright / axe 里从没被
// 渲染过。本仓库有过「vitest 绿而 Playwright/axe 红」的先例,所以让 axe 真扫一遍。
test("(c) MCP per-parameter binding editor passes axe (serious + critical)", async ({
  page,
}) => {
  await page.getByTestId("agents-create").click();
  await expect(page.getByTestId("manifest-form-view")).toBeVisible();

  // 先声明一个提示词变量 —— 没有声明变量时绑定区只给一句提示,下拉根本不渲染。
  await page.getByTestId("cfg-nav-prompt").click();
  await page.getByTestId("af-prompt-jinja").click();
  await page.getByTestId("af-prompt-var-add").click();
  await page.getByTestId("af-prompt-var-name-0").fill("project_code");

  // 再到 MCP:勾服务器 → 开工具子弹窗 → 展开 create_issue 的参数绑定。
  await page.getByTestId("cfg-nav-capabilities").click();
  await page.getByRole("tab", { name: "MCP" }).click();
  await page.getByTestId("af-mcp-server-github").click();
  await page.getByTestId("af-mcp-choose-github").click();
  await expect(page.getByTestId("af-mcp-tool-create_issue")).toBeVisible();
  await page.getByTestId("af-mcp-bind-toggle-create_issue").click();

  // 逐参数一行都在(必填的 repo/title + 选填的 body)。
  await expect(page.getByTestId("af-mcp-bind-row-create_issue-repo")).toBeVisible();
  await expect(page.getByTestId("af-mcp-bind-row-create_issue-body")).toBeVisible();
  // aria-controls 指得着真实的展开区。
  const panelId = await page
    .getByTestId("af-mcp-bind-toggle-create_issue")
    .getAttribute("aria-controls");
  expect(panelId).toBeTruthy();
  await expect(page.locator(`#${panelId}`)).toBeVisible();

  await expectNoA11yViolations(page, "create-agent-modal-mcp-bindings");
});
