/**
 * Platform delegation-gate config e2e — perf phase2 PR3.
 *
 * Proves a system_admin can view the platform delegation-gate capacity at
 * /settings/platform and edit + save it (persisted via PUT). Plus an axe
 * pass.
 *
 * Mirrors platform-dynamic-worker.spec.ts: the section renders only on the
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

const DELEGATION_CONFIG = {
  success: true,
  data: {
    configured: null,
    effective: { max_concurrent_delegations: 16 },
  },
  error: null,
};

const DELEGATION_PUT_RESULT = {
  success: true,
  data: {
    configured: { max_concurrent_delegations: 5 },
    effective: { max_concurrent_delegations: 5 },
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
  await page.route("**/v1/platform/delegation-config", async (route) => {
    if (route.request().method() === "PUT") {
      await route.fulfill({ json: DELEGATION_PUT_RESULT });
      return;
    }
    await route.fulfill({ json: DELEGATION_CONFIG });
  });
});

test("system_admin views + edits the platform delegation-gate capacity", async ({
  page,
}) => {
  await login(page);
  await page.goto("/settings/platform?tab=cost");

  await expect(page.getByTestId("pdg-root")).toBeVisible();
  await expect(page.getByTestId("pdg-help")).toBeVisible();
  // unset platform override ⇒ env-default tag + effective value seeded
  await expect(page.getByTestId("pdg-env-default")).toBeVisible();
  await expect(page.getByTestId("pdg-max-concurrent-delegations")).toHaveValue("16");

  await page.getByTestId("pdg-max-concurrent-delegations").fill("5");

  const [putReq] = await Promise.all([
    page.waitForRequest(
      (req) =>
        req.url().includes("/v1/platform/delegation-config") &&
        req.method() === "PUT",
    ),
    page.getByTestId("pdg-save").click(),
  ]);
  const body = putReq.postDataJSON();
  expect(body.max_concurrent_delegations).toBe(5);
});

test("settings/platform with the delegation section passes axe", async ({
  page,
}) => {
  await login(page);
  await page.goto("/settings/platform?tab=cost");

  await expect(page.getByTestId("pdg-root")).toBeVisible();
  await expectNoA11yViolations(page, "settings-platform");
});
