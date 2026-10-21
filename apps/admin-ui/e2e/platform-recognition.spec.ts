/**
 * Platform 「文档识别」 config e2e — B-209 §5.7.
 *
 * A system_admin sees the document-recognition section on the 模型服务 tab of
 * /settings/platform, changes the fallback model and the page limit and saves
 * (PUT body asserted), plus an axe pass. Mirrors platform-judge.spec.ts: the
 * section renders only on the system_admin branch, so /v1/me is overridden and
 * the platform GETs are stubbed (Playwright routes are LIFO → win over fixtures).
 */
import { test, expect, expectNoA11yViolations, SAMPLE_JWT } from "./fixtures";

const SYS_ADMIN_ME = {
  success: true,
  data: {
    subject_id: "11111111-1111-1111-1111-111111111111",
    subject_type: "user",
    tenant_id: "22222222-2222-2222-2222-222222222222",
    auth_method: "jwt",
    roles: ["admin"],
    scopes: [],
    is_system_admin: true,
    allowed_tenants: "*",
  },
  error: null,
};

const RECOGNITION_CONFIG = {
  success: true,
  data: {
    ocr: { provider: "glm", model: "glm-ocr" },
    fallback: { provider: "qwen", model: "qwen3.7-flash" },
    figure: { provider: "qwen", model: "qwen3.8-flash" },
    max_pages: 1000,
    is_default: true,
    available_ocr: [
      { provider: "glm", model: "glm-ocr" },
      { provider: "qwen", model: "qwen3.7-flash" },
      { provider: "qwen", model: "qwen3.8-flash" },
    ],
    available_figure: [
      { provider: "qwen", model: "qwen3.7-flash" },
      { provider: "qwen", model: "qwen3.8-flash" },
    ],
  },
  error: null,
};

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

test.beforeEach(async ({ page }) => {
  await page.route("**/v1/me", async (route) => {
    await route.fulfill({ json: SYS_ADMIN_ME });
  });
  await page.route("**/v1/platform/credentials", async (route) => {
    await route.fulfill({ json: { success: true, data: { providers: [], tools: [] }, error: null } });
  });
  await page.route("**/v1/platform/embedding-config", async (route) => {
    await route.fulfill({
      json: {
        success: true,
        data: { embedding: null, rerank: null, available_embedding: [], available_rerank: [] },
        error: null,
      },
    });
  });
  await page.route("**/v1/platform/judge-config", async (route) => {
    await route.fulfill({ json: { success: true, data: { judge: null, available: [] }, error: null } });
  });
  await page.route("**/v1/platform/recognition-config", async (route) => {
    if (route.request().method() === "PUT") {
      const body = route.request().postDataJSON();
      await route.fulfill({
        json: {
          success: true,
          data: {
            ocr: { provider: body.ocr_provider, model: body.ocr_model },
            fallback: { provider: body.fallback_provider, model: body.fallback_model },
            figure: { provider: body.figure_provider, model: body.figure_model },
            max_pages: body.max_pages,
            is_default: false,
          },
          error: null,
        },
      });
      return;
    }
    await route.fulfill({ json: RECOGNITION_CONFIG });
  });
});

test("system_admin views + saves the document recognition config", async ({ page }) => {
  await login(page);
  await page.goto("/settings/platform?tab=models");

  await expect(page.getByTestId("pr-root")).toBeVisible();
  await expect(page.getByTestId("pr-help")).toBeVisible();
  await expect(page.getByTestId("pr-default")).toBeVisible();

  await page.getByTestId("pr-fallback").locator(".ant-select").click();
  await page
    .locator(".ant-select-dropdown:visible .ant-select-item-option-content", {
      hasText: "qwen / qwen3.8-flash",
    })
    .click();
  await page.getByTestId("pr-max-pages").fill("300");

  const [putReq] = await Promise.all([
    page.waitForRequest(
      (req) => req.url().includes("/v1/platform/recognition-config") && req.method() === "PUT",
    ),
    page.getByTestId("pr-save").click(),
  ]);
  expect(putReq.postDataJSON()).toEqual({
    ocr_provider: "glm",
    ocr_model: "glm-ocr",
    fallback_provider: "qwen",
    fallback_model: "qwen3.8-flash",
    figure_provider: "qwen",
    figure_model: "qwen3.8-flash",
    max_pages: 300,
  });
  await expect(page.getByTestId("pr-default")).toHaveCount(0);
});

test("settings/platform with the recognition section passes axe", async ({ page }) => {
  await login(page);
  await page.goto("/settings/platform?tab=models");

  await expect(page.getByTestId("pr-root")).toBeVisible();
  await expectNoA11yViolations(page, "settings-platform-recognition");
});
