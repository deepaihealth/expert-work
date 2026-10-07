/**
 * Manifest editor E2E — Stream S PR C, selectors migrated to the group-nav +
 * detail-pane layout (agent-config-page redesign PR1).
 *
 * Proves the Create-Agent modal opens the visual ``<ManifestEditor>`` on its
 * Form view (the "basic" group by default), that the top-right YAML toggle
 * switches the pane to the raw YAML view, and that the open modal passes the
 * axe a11y check.
 *
 * The editor fetches ``GET /v1/agents/schema`` (enveloped) on mount, so
 * we stub it here. Because the shared ``installControlPlaneStub`` fixture
 * registers ``**​/v1/agents*`` (which also matches ``/agents/schema``),
 * we register the more specific schema route *after* it — Playwright runs
 * the most-recently-added handler first, so ours wins.
 */
import { test, expect, expectNoA11yViolations, SAMPLE_JWT } from "./fixtures";

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

test.beforeEach(async ({ page }) => {
  // More specific than the fixture's ``**/v1/agents*`` stub; registered
  // after it so it takes precedence for the schema fetch.
  await page.route("**/v1/agents/schema", async (route) => {
    await route.fulfill({ json: SCHEMA_ENVELOPE });
  });
  // The modal fetches the platform embedding status on open (Stream T PR E);
  // stub the configured path so the editor renders deterministically.
  await page.route("**/v1/platform/embedding-config/status", (route) =>
    route.fulfill({ json: { success: true, data: { configured: true }, error: null } }),
  );
  await page.goto("/login");
  await expect(page.getByTestId("login-card")).toBeVisible();
  // The paste-token form sits behind the "Developer login" disclosure
  // whenever OIDC is configured (``VITE_OIDC_ISSUER``). In CI it is
  // open by default; locally it may be collapsed — reveal it if needed.
  const tokenField = page.getByTestId("login-token");
  if (!(await tokenField.isVisible())) {
    await page.getByTestId("login-dev-toggle").click();
  }
  await tokenField.fill(SAMPLE_JWT);
  await page.getByTestId("login-submit").click();
  await expect(page).toHaveURL(/\/agents$/);
  await expect(page.getByText("customer-support-bot")).toBeVisible();
});

test("create modal opens the manifest editor on the Form tab", async ({ page }) => {
  await page.getByTestId("agents-create").click();
  await expect(page.getByTestId("create-agent-modal")).toBeVisible();
  await expect(page.getByTestId("manifest-editor-create")).toBeVisible();
  await expect(page.getByTestId("manifest-form-view")).toBeVisible();

  // Top-right YAML toggle (replaces the old flat-tab row's "yaml" tab).
  await page.getByTestId("cfg-yaml-toggle").click();
  await expect(page.getByTestId("manifest-yaml-view")).toBeVisible();
});

test("YAML view renders Monaco from the local bundle (no CDN request)", async ({ page }) => {
  // B-152: Monaco used to be fetched from cdn.jsdelivr.net at runtime.
  const cdnRequests: string[] = [];
  page.on("request", (req) => {
    if (new URL(req.url()).hostname !== "localhost") cdnRequests.push(req.url());
  });
  await page.getByTestId("agents-create").click();
  await expect(page.getByTestId("manifest-editor-create")).toBeVisible();
  await page.getByTestId("cfg-yaml-toggle").click();
  // 开发服务器冷启动时首次编译 Monaco 较慢(CI 上同样),放宽等待。
  await expect(page.getByTestId("manifest-yaml-view").locator(".monaco-editor")).toBeVisible({
    timeout: 30_000,
  });
  expect(cdnRequests.filter((u) => u.includes("jsdelivr"))).toEqual([]);
});

test("create modal passes axe (serious + critical)", async ({ page }) => {
  await page.getByTestId("agents-create").click();
  await expect(page.getByTestId("manifest-editor-create")).toBeVisible();
  await expectNoA11yViolations(page, "create-agent-modal");
});
