/**
 * Manifest model-select E2E — Stream S PR D, selectors migrated to the
 * group-nav + detail-pane layout (agent-config-page redesign PR1).
 *
 * Proves an admin can pick a provider + model through the curated Form's
 * linked model picker (``<ModelSelect>``) under the "Model" group, that
 * choosing a vision-capable model flips the vision indicator to its
 * supported state, and that the open Create-Agent modal (with the picker
 * mounted) passes the axe a11y check.
 *
 * The editor fetches ``GET /v1/agents/schema`` and the picker fetches
 * ``GET /v1/model-catalog`` (both enveloped) on mount, so we stub both. The
 * shared ``installControlPlaneStub`` fixture registers ``**​/v1/agents*``
 * (which also matches ``/agents/schema``); we register the more specific
 * schema route *after* it so it wins (Playwright runs the most-recently-added
 * handler first). ``model-catalog`` is not covered by the fixture, so a plain
 * route is enough.
 */
import { test, expect, expectNoA11yViolations, SAMPLE_JWT } from "./fixtures";

// spec.model is an OBJECT here (provider/name/supports_vision); the curated
// form reads it into the ModelSelect picker and the linked dropdowns engage.
const SCHEMA_ENVELOPE = {
  success: true,
  error: null,
  data: {
    type: "object",
    properties: {
      spec: {
        type: "object",
        properties: {
          model: {
            type: "object",
            properties: {
              provider: { type: "string" },
              name: { type: "string" },
              supports_vision: { type: "boolean" },
            },
          },
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
        provider: "deepseek",
        models: [
          {
            name: "deepseek-v4-pro",
            vision: false,
            embeddings: false,
            context_window: 1000000,
            deprecated: false,
          },
        ],
      },
      {
        provider: "openai",
        models: [
          {
            name: "gpt-5.5",
            vision: true,
            embeddings: false,
            context_window: 128000,
            deprecated: false,
            thinking: "effort",
            thinking_default: true,
          },
        ],
      },
    ],
  },
};

test.beforeEach(async ({ page }) => {
  // More specific than the fixture's ``**/v1/agents*`` stub; registered after
  // it so it takes precedence for the schema fetch.
  await page.route("**/v1/agents/schema", async (route) => {
    await route.fulfill({ json: SCHEMA_ENVELOPE });
  });
  await page.route("**/v1/model-catalog", async (route) => {
    await route.fulfill({ json: CATALOG_ENVELOPE });
  });
  // The modal fetches the platform embedding status on open (Stream T PR E);
  // stub the configured path so the editor renders deterministically.
  await page.route("**/v1/platform/embedding-config/status", (route) =>
    route.fulfill({
      json: { success: true, data: { configured: true }, error: null },
    }),
  );
  await page.goto("/login");
  await expect(page.getByTestId("login-card")).toBeVisible();
  // The paste-token form sits behind the "Developer login" disclosure
  // whenever OIDC is configured (``VITE_OIDC_ISSUER``). In CI it is open by
  // default; locally it may be collapsed — reveal it if needed.
  const tokenField = page.getByTestId("login-token");
  if (!(await tokenField.isVisible())) {
    await page.getByTestId("login-dev-toggle").click();
  }
  await tokenField.fill(SAMPLE_JWT);
  await page.getByTestId("login-submit").click();
  await expect(page).toHaveURL(/\/agents$/);
  await expect(page.getByText("customer-support-bot")).toBeVisible();
});

test("pick a provider + model via the form turns vision on", async ({
  page,
}) => {
  await page.getByTestId("agents-create").click();
  await expect(page.getByTestId("create-agent-modal")).toBeVisible();
  await expect(page.getByTestId("manifest-form-view")).toBeVisible();
  // The model controls live under the "Model" group in the left tree now.
  // The reflection-evaluator section reuses <ModelSelect>, so this group has
  // two ``model-select-*`` instances — scope to af-model.
  await page.getByTestId("cfg-nav-model").click();
  const model = page.getByTestId("af-model");
  await expect(model.getByTestId("model-select-field")).toBeVisible();

  // Open the provider Select and choose "openai".
  await model
    .getByTestId("model-select-provider")
    .locator(".ant-select")
    .click();
  await page
    .locator(".ant-select-item-option-content", { hasText: "openai" })
    .click();

  // Open the model Select and choose "gpt-5.5" (the vision-capable model).
  await model.getByTestId("model-select-name").locator(".ant-select").click();
  await page
    .locator(".ant-select-item-option-content", { hasText: "gpt-5.5" })
    .click();

  // Vision indicator flips to the supported state (zh-CN "视觉：支持" or
  // en "Vision: supported" — lenient on locale, strict that it's supported).
  await expect(model.getByTestId("model-select-vision")).toContainText(
    /视觉：支持|Vision: supported/,
  );

  // Thinking-Toggle — gpt-5.5 has a thinking knob, so the switch appears; as a
  // reasoning_effort vendor it cannot fully disable, so the hint shows too.
  await expect(model.getByTestId("model-select-thinking")).toBeVisible();
  await expect(
    model.getByTestId("model-select-thinking-hint"),
  ).toBeVisible();
});

test("the reflection-evaluator picker rides the reflection panel (shown once reflection is on)", async ({
  page,
}) => {
  await page.getByTestId("agents-create").click();
  await expect(page.getByTestId("manifest-form-view")).toBeVisible();
  // The reflection panel lives under the "Model" group.
  await page.getByTestId("cfg-nav-model").click();
  // Hidden while reflection self-assessment is off (the default).
  await expect(page.getByTestId("af-reflection-evaluator")).toHaveCount(0);
  // Flip the reflection switch on — the evaluator model picker appears.
  await page
    .locator('[data-field-id="reflection"]')
    .getByRole("switch")
    .click();
  const evaluator = page.getByTestId("af-reflection-evaluator");
  await expect(evaluator).toBeVisible();
  // Empty by default (reflection reuses the agent's own model) — no clear link.
  await expect(evaluator.getByTestId("model-select-field")).toBeVisible();
  await expect(page.getByTestId("af-reflection-evaluator-clear")).toHaveCount(
    0,
  );
});

test("create modal with model picker passes axe (serious + critical)", async ({
  page,
}) => {
  await page.getByTestId("agents-create").click();
  await expect(page.getByTestId("manifest-editor-create")).toBeVisible();
  await page.getByTestId("cfg-nav-model").click();
  await expect(
    page.getByTestId("af-model").getByTestId("model-select-field"),
  ).toBeVisible();
  await expectNoA11yViolations(page, "create-agent-modal");
});
