/**
 * Structured-output field tree E2E — B-315.
 *
 * The health-manager agent's reply schema (two lists with item limits) used
 * to fall back to "complex structure — edit in YAML". Now the form shows it
 * as a tree: this test lowers ``messages``' max items from 4 to 3 through
 * the row's Limits popover, flips to the YAML view and checks the change
 * landed there (``maxItems: 3``) while ``media``'s own ``maxItems: 3``
 * line is still the only other one — i.e. exactly one value changed.
 *
 * Route stubbing mirrors manifest-edit.spec.ts: the detail GET is
 * registered after the fixture's ``**​/v1/agents*`` glob so it wins.
 */
import { test, expect, SAMPLE_JWT } from "./fixtures";

const AGENT_NAME = "customer-support-bot";
const AGENT_VERSION = "3.4.2";

const HEALTH_MANAGER_OUTPUT_SCHEMA = {
  json_schema: {
    type: "object",
    additionalProperties: false,
    required: ["messages", "media"],
    properties: {
      messages: {
        type: "array",
        maxItems: 4,
        items: { type: "string", minLength: 1, maxLength: 300, pattern: "\\S" },
      },
      media: {
        type: "array",
        maxItems: 3,
        uniqueItems: true,
        items: { type: "integer", minimum: 1 },
      },
    },
  },
};

const DETAIL_ENVELOPE = {
  success: true,
  error: null,
  data: {
    record: {
      id: "33333333-3333-3333-3333-333333333333",
      tenant_id: "22222222-2222-2222-2222-222222222222",
      name: AGENT_NAME,
      version: AGENT_VERSION,
      status: "active",
      spec_sha256: "a".repeat(64),
      created_by: "alice@acme.com",
      created_at: "2026-04-12T09:00:00Z",
      updated_at: "2026-05-25T07:00:00Z",
      spec: {
        apiVersion: "expert_work/v1",
        kind: "Agent",
        metadata: { name: AGENT_NAME, version: AGENT_VERSION },
        spec: {
          model: { provider: "openai", name: "gpt-5.5" },
          system_prompt: "You are a health manager.",
          output_schema: HEALTH_MANAGER_OUTPUT_SCHEMA,
        },
      },
    },
  },
};

// Tall enough that Monaco renders every line of the manifest (it only
// draws the visible ones).
test.use({ viewport: { width: 1280, height: 1600 } });

test.beforeEach(async ({ page }) => {
  await page.route("**/v1/agents/schema", async (route) => {
    await route.fulfill({ json: { success: true, error: null, data: { type: "object" } } });
  });
  await page.route("**/v1/model-catalog", async (route) => {
    await route.fulfill({ json: { success: true, error: null, data: { providers: [] } } });
  });
  await page.route(`**/v1/agents/${AGENT_NAME}/${AGENT_VERSION}`, async (route) => {
    await route.fulfill({ json: DETAIL_ENVELOPE });
  });
  // Save = PUT .../draft; stubbed so a save never reaches a real backend.
  await page.route(`**/v1/agents/${AGENT_NAME}/${AGENT_VERSION}/draft`, async (route) => {
    await route.fulfill({ json: DETAIL_ENVELOPE });
  });

  await page.goto("/login");
  await expect(page.getByTestId("login-card")).toBeVisible();
  const tokenField = page.getByTestId("login-token");
  if (!(await tokenField.isVisible())) {
    await page.getByTestId("login-dev-toggle").click();
  }
  await tokenField.fill(SAMPLE_JWT);
  await page.getByTestId("login-submit").click();
  await expect(page).toHaveURL(/\/agents$/);
  await page.goto(`/agents/${AGENT_NAME}/${AGENT_VERSION}/manifest`);
  await expect(page.getByTestId("manifest-form-view")).toBeVisible();
});

test("health-manager schema: messages maxItems 4 → 3 in the form shows up in YAML", async ({
  page,
}) => {
  await page.getByTestId("cfg-nav-prompt").click();
  const section = page.getByTestId("af-output-schema");
  await expect(section).toBeVisible();
  // The tree, not the old "complex structure" fallback.
  await expect(page.getByTestId("af-output-schema-readonly")).toHaveCount(0);
  await expect(page.getByTestId("af-output-schema-name-0")).toHaveValue("messages");
  await expect(page.getByTestId("af-output-schema-row-0.items")).toBeVisible();

  await page.getByTestId("af-output-schema-limits-0").click();
  const maxItems = page.getByTestId("af-output-schema-limit-maxItems-0");
  await expect(maxItems).toHaveValue("4");
  await maxItems.fill("3");
  await maxItems.press("Tab");

  await page.getByTestId("cfg-yaml-toggle").click();
  const yamlView = page.getByTestId("manifest-yaml-view");
  await expect(yamlView.locator(".monaco-editor")).toBeVisible({ timeout: 30_000 });
  // Monaco renders spaces as nbsp; count matches over the rendered lines.
  // Two ``maxItems: 3`` = media's own + messages' new value; no ``4`` left.
  const yamlText = async (): Promise<string> =>
    ((await yamlView.locator(".view-lines").textContent()) ?? "").replace(/\u00a0/g, " ");
  await expect.poll(async () => (await yamlText()).match(/maxItems: 3\b/g)?.length ?? 0).toBe(2);
  expect(await yamlText()).not.toMatch(/maxItems: 4\b/);
  // Sanity: the whole schema is on screen (the last line of it rendered).
  expect(await yamlText()).toContain("minimum: 1");
});

test("a duplicated field name blocks saving until it is fixed", async ({ page }) => {
  await page.getByTestId("cfg-nav-prompt").click();
  const name = page.getByTestId("af-output-schema-name-0");
  await expect(name).toHaveValue("messages");
  const save = page.getByTestId("manifest-save-btn");
  await expect(save).toBeEnabled();

  await name.fill("media");
  await expect(page.getByTestId("af-output-schema-name-errors")).toBeVisible();
  await expect(name).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByTestId("af-output-schema-name-1")).toHaveAttribute("aria-invalid", "true");
  await expect(save).toBeDisabled();

  await name.fill("msgs");
  await expect(page.getByTestId("af-output-schema-name-errors")).toHaveCount(0);
  await expect(save).toBeEnabled();
  const putPromise = page.waitForRequest(
    (req) => req.method() === "PUT" && req.url().endsWith(`/v1/agents/${AGENT_NAME}/${AGENT_VERSION}/draft`),
  );
  await save.click();
  const body = (await putPromise).postDataJSON() as { manifest_yaml: string };
  expect(body.manifest_yaml).toContain("msgs:");
  expect(body.manifest_yaml).toContain("media:");
  expect(body.manifest_yaml).not.toContain("messages:");
});
