/**
 * Settings — MCP Servers page e2e (Stream V-F).
 *
 * Proves a tenant admin can:
 *   (a) see the MCP servers table with registered servers;
 *   (b) expand a row to reveal its live tool list;
 *   (c) click the per-row 测试 button and see the connected status;
 *   (d) open the add drawer, hit 测试连接 and see the success result;
 *   (e) pass the axe accessibility audit.
 *
 * Mirrors ``tenants.spec.ts``: same login helper, same ``SAMPLE_JWT`` /
 * ``expectNoA11yViolations`` imports from ``./fixtures``, and spec-level
 * ``page.route`` mocks that win over the fixture's global stubs (LIFO).
 */
import { test, expect, expectNoA11yViolations, SAMPLE_JWT } from "./fixtures";

// ── Fixture data ──────────────────────────────────────────────────────────

const MCP_SERVERS_LIST = {
  success: true,
  data: [
    {
      id: "aaaaaaaa-0000-0000-0000-000000000001",
      name: "github",
      transport: "sse",
      url: "https://mcp.github.com/sse",
      auth_type: "bearer",
      timeout_s: 30,
      enabled: true,
      created_at: "2026-05-01T10:00:00Z",
      updated_at: "2026-05-01T10:00:00Z",
    },
    {
      id: "aaaaaaaa-0000-0000-0000-000000000002",
      name: "linear",
      transport: "streamable_http",
      url: "https://mcp.linear.app/mcp",
      auth_type: "bearer",
      timeout_s: 60,
      enabled: false,
      created_at: "2026-05-10T08:00:00Z",
      updated_at: "2026-05-10T08:00:00Z",
    },
  ],
  error: null,
};

const TOOLS_LIST = {
  success: true,
  data: [
    {
      name: "create_issue",
      description: "Create a new issue",
      // B-318 —— 参数表(与平台目录页共用)显示可选值。
      input_schema: {
        type: "object",
        properties: {
          title: { type: "string", description: "Issue title" },
          state: {
            type: "string",
            oneOf: [
              { const: "open", title: "打开" },
              { const: "closed", title: "关闭" },
            ],
          },
        },
        required: ["title"],
      },
    },
    { name: "list_repos", description: "List repositories" },
  ],
  error: null,
};

const TEST_CONNECTION_OK = {
  success: true,
  data: { tool_count: 2 },
  error: null,
};

// ── Login helper ──────────────────────────────────────────────────────────

async function login(page: import("@playwright/test").Page): Promise<void> {
  await page.goto("/login");
  await expect(page.getByTestId("login-card")).toBeVisible();
  const tokenField = page.getByTestId("login-token");
  if (!(await tokenField.isVisible())) {
    await page.getByTestId("login-dev-toggle").click();
  }
  await tokenField.fill(SAMPLE_JWT);
  await page.getByTestId("login-submit").click();
  await expect(page).toHaveURL(/\/agents$/);
}

// ── Route setup ──────────────────────────────────────────────────────────

// Stream W — the "Add MCP server" action now opens a catalog browser first.
const CATALOG_LIST = { success: true, data: [], error: null };

// The unified list also fetches GET /v1/mcp-servers/available (platform
// allowlist rows). This spec exercises only custom (tenant) servers, so an
// empty platform list is sufficient — the page merges it with MCP_SERVERS_LIST.
const AVAILABLE_LIST = { success: true, data: [], error: null };

test.beforeEach(async ({ page }) => {
  // GET /v1/mcp-servers/catalog → tenant catalog (register before the broader
  // /v1/mcp-servers route so glob ordering is correct).
  await page.route("**/v1/mcp-servers/catalog", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: CATALOG_LIST });
      return;
    }
    await route.fallback();
  });
  // GET /v1/mcp-servers/available → unified-list platform rows (empty here).
  await page.route("**/v1/mcp-servers/available", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: AVAILABLE_LIST });
      return;
    }
    await route.fallback();
  });
  // GET /v1/mcp-servers → server list (must be registered before the
  // more-specific tools route so the glob ordering is correct).
  await page.route("**/v1/mcp-servers", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: MCP_SERVERS_LIST });
      return;
    }
    await route.fallback();
  });
  // GET /v1/mcp-servers/*/tools → tool list.
  await page.route("**/v1/mcp-servers/*/tools", async (route) => {
    await route.fulfill({ json: TOOLS_LIST });
  });
  // POST /v1/mcp-servers/test → probe-only success.
  await page.route("**/v1/mcp-servers/test", async (route) => {
    if (route.request().method() === "POST") {
      await route.fulfill({ json: TEST_CONNECTION_OK });
      return;
    }
    await route.fallback();
  });
});

// ── Tests ─────────────────────────────────────────────────────────────────

test("(a) lists MCP servers in the table", async ({ page }) => {
  await login(page);
  await page.goto("/settings/mcp-servers");

  await expect(page.getByTestId("ms-table")).toBeVisible();
  await expect(page.getByText("github", { exact: true })).toBeVisible();
});

test("(b) expanding a row shows its live tools", async ({ page }) => {
  await login(page);
  await page.goto("/settings/mcp-servers");

  await expect(page.getByTestId("ms-table")).toBeVisible();

  // Click the expand icon for the first row (github).
  await page.locator(".ant-table-row-expand-icon").first().click();

  // The tool list div should appear with a tool name.
  await expect(page.getByTestId("ms-tools-github")).toBeVisible();
  await expect(page.getByText("create_issue")).toBeVisible();
});

test("(c) clicking 测试 shows connected status", async ({ page }) => {
  await login(page);
  await page.goto("/settings/mcp-servers");

  await expect(page.getByTestId("ms-table")).toBeVisible();

  // Click the 测试 button for the github row.
  await page.getByTestId("ms-test-github").click();

  // After the probe returns, the status column updates to "connected".
  // The connected text includes the tool count: "已连接 · 2 个工具"
  await expect(page.getByText(/已连接|Connected/)).toBeVisible();
});

test("(d) advanced custom path → 测试连接 shows success", async ({ page }) => {
  await login(page);
  await page.goto("/settings/mcp-servers");

  await expect(page.getByTestId("ms-table")).toBeVisible();

  // Open the catalog browser, then reach the legacy custom form via the
  // demoted "Advanced — add a custom server" affordance (Stream W).
  await page.getByTestId("ms-add").click();
  await page.getByTestId("amsd-custom").click();
  await expect(page.getByTestId("cms-form")).toBeVisible();

  // Fill in the minimum required fields.
  await page.getByTestId("cms-name").fill("test-server");
  await page.getByTestId("cms-url").fill("https://mcp.example.com/mcp");

  // Click 测试连接 / Test connection.
  await page.getByTestId("cms-test").click();

  // The result area should appear showing success.
  await expect(page.getByTestId("cms-test-result")).toBeVisible();
});

test("(f) custom headers tab → header reaches the test request", async ({
  page,
}) => {
  await login(page);
  await page.goto("/settings/mcp-servers");
  await expect(page.getByTestId("ms-table")).toBeVisible();

  // Capture the body of the test-connection probe.
  let testBody: Record<string, unknown> | null = null;
  await page.route("**/v1/mcp-servers/test", async (route) => {
    if (route.request().method() === "POST") {
      testBody = route.request().postDataJSON();
      await route.fulfill({ json: TEST_CONNECTION_OK });
      return;
    }
    await route.fallback();
  });

  await page.getByTestId("ms-add").click();
  await page.getByTestId("amsd-custom").click();
  await expect(page.getByTestId("cms-form")).toBeVisible();

  await page.getByTestId("cms-name").fill("hdr-server");
  await page.getByTestId("cms-url").fill("https://mcp.example.com/mcp");

  // Switch to the headers tab, add one header.
  await page.getByRole("tab", { name: /请求头|Headers/ }).click();
  await page.getByTestId("cms-header-add").click();
  await page.getByTestId("cms-header-key-0").fill("X-API-Key");
  await page.getByTestId("cms-header-value-0").fill("KEY-123");

  // Back to the basic tab and run the connection test.
  await page.getByRole("tab", { name: /认证|Auth/ }).click();
  await page.getByTestId("cms-test").click();
  await expect(page.getByTestId("cms-test-result")).toBeVisible();

  expect(testBody).not.toBeNull();
  expect(testBody!.custom_headers).toEqual({ "X-API-Key": "KEY-123" });
});

test("(e) settings/mcp-servers passes axe", async ({ page }) => {
  await login(page);
  await page.goto("/settings/mcp-servers");

  await expect(page.getByTestId("ms-table")).toBeVisible();
  await expectNoA11yViolations(page, "settings-mcp-servers");
});

// B-318 —— 租户页展开工具是和平台目录页同一张参数表,包括「可选：…」那一行。
test("(g) expanding a tool shows its parameter table with allowed values + axe", async ({
  page,
}) => {
  await login(page);
  await page.goto("/settings/mcp-servers");
  await expect(page.getByTestId("ms-table")).toBeVisible();
  await page.locator(".ant-table-row-expand-icon").first().click();
  const tools = page.getByTestId("ms-tools-github");
  await expect(tools).toBeVisible();
  await tools.locator(".ant-collapse-header", { hasText: "create_issue" }).click();

  const table = page.getByTestId("ms-params-github-create_issue");
  await expect(table).toBeVisible();
  await expect(table).toContainText("Issue title");
  await expect(page.getByTestId("ms-params-github-create_issue-options-state")).toHaveText(
    /(可选：|Allowed: )打开 open \/ 关闭 closed/,
  );
  await expectNoA11yViolations(page, "settings-mcp-servers-tool-params");
});
