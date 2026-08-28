/**
 * Platform dynamic-worker guardrail config e2e — B3 PR2.
 *
 * Proves a system_admin can view the platform dynamic-worker limits at
 * /settings/platform and edit + save them (persisted via PUT). Plus an axe
 * pass.
 *
 * Mirrors platform-tool-budget.spec.ts: the section renders only on the
 * system_admin branch, so /v1/me is overridden with a system-admin identity
 * and the platform GETs are stubbed (Playwright routes are LIFO → win over
 * fixtures).
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

const CREDENTIALS_VIEW = {
  success: true,
  data: { providers: [], tools: [] },
  error: null,
};

const DYNAMIC_WORKER_CONFIG = {
  success: true,
  data: {
    configured: null,
    effective: {
      max_concurrent: 3,
      max_per_run: 16,
      max_iterations: 32,
      cap_max_concurrent: 10,
      cap_max_per_run: 64,
      cap_max_iterations: 128,
    },
  },
  error: null,
};

const DYNAMIC_WORKER_PUT_RESULT = {
  success: true,
  data: {
    configured: {
      max_concurrent: 5,
      max_per_run: 16,
      max_iterations: 32,
      cap_max_concurrent: 10,
      cap_max_per_run: 64,
      cap_max_iterations: 128,
    },
    effective: {
      max_concurrent: 5,
      max_per_run: 16,
      max_iterations: 32,
      cap_max_concurrent: 10,
      cap_max_per_run: 64,
      cap_max_iterations: 128,
    },
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
    await route.fulfill({ json: CREDENTIALS_VIEW });
  });
  await page.route("**/v1/platform/dynamic-worker-config", async (route) => {
    if (route.request().method() === "PUT") {
      await route.fulfill({ json: DYNAMIC_WORKER_PUT_RESULT });
      return;
    }
    await route.fulfill({ json: DYNAMIC_WORKER_CONFIG });
  });
});

test("system_admin views + edits the platform dynamic-worker guardrails", async ({
  page,
}) => {
  await login(page);
  await page.goto("/settings/platform?tab=cost");

  await expect(page.getByTestId("pdw-root")).toBeVisible();
  await expect(page.getByTestId("pdw-help")).toBeVisible();
  // unset platform override ⇒ env-default tag + effective values seeded
  await expect(page.getByTestId("pdw-env-default")).toBeVisible();
  await expect(page.getByTestId("pdw-max-concurrent")).toHaveValue("3");
  await expect(page.getByTestId("pdw-max-per-run")).toHaveValue("16");
  await expect(page.getByTestId("pdw-max-iterations")).toHaveValue("32");
  // 弹性 worker 预算 — hard-cap tier renders too
  await expect(page.getByTestId("pdw-cap-max-concurrent")).toHaveValue("10");
  await expect(page.getByTestId("pdw-cap-max-per-run")).toHaveValue("64");
  await expect(page.getByTestId("pdw-cap-max-iterations")).toHaveValue("128");

  await page.getByTestId("pdw-max-concurrent").fill("5");

  const [putReq] = await Promise.all([
    page.waitForRequest(
      (req) =>
        req.url().includes("/v1/platform/dynamic-worker-config") &&
        req.method() === "PUT",
    ),
    page.getByTestId("pdw-save").click(),
  ]);
  const body = putReq.postDataJSON();
  expect(body.max_concurrent).toBe(5);
  expect(body.max_per_run).toBe(16);
  expect(body.max_iterations).toBe(32);
  expect(body.cap_max_concurrent).toBe(10);
  expect(body.cap_max_per_run).toBe(64);
  expect(body.cap_max_iterations).toBe(128);
});

test("settings/platform with the dynamic-worker section passes axe", async ({
  page,
}) => {
  await login(page);
  await page.goto("/settings/platform?tab=cost");

  await expect(page.getByTestId("pdw-root")).toBeVisible();
  await expectNoA11yViolations(page, "settings-platform");
});
