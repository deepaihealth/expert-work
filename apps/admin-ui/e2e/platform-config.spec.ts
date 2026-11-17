/**
 * Platform Credentials page e2e — Stream P (PR I).
 *
 * system_admin sees the provider/tool tables; a non-admin sees the
 * "system admin only" notice. Both run axe. The default mock has
 * ``is_system_admin: false``, so the admin test overrides ``/v1/me`` and
 * stubs the platform-credentials GET (Playwright routes are LIFO).
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

const VIEW = {
  success: true,
  data: {
    providers: [
      {
        provider: "anthropic",
        source: "db",
        secret_ref: "kms://platform/anthropic",
        enabled: true,
        keys: [
          {
            key_id: "default",
            secret_ref: "kms://platform/anthropic",
            enabled: true,
            priority: 100,
          },
        ],
        used_by_agents: 3,
        tenant_override_count: 0,
      },
      {
        provider: "qwen",
        source: "unset",
        secret_ref: null,
        enabled: false,
        keys: [],
        used_by_agents: 0,
        // B-51 —— 未设置 + 平台自己在用 → 行上警示 + 顶部 banner(带进 axe)。
        platform_uses: [
          { feature: "embedding", model: "text-embedding-v4", enabled: true },
          { feature: "rerank", model: "qwen-plus", enabled: false },
        ],
        tenant_override_count: 0,
      },
    ],
    tools: [
      {
        tool: "web_search",
        source: "env",
        secret_ref: "secret://tavily",
        enabled: true,
        used_by_agents: 1,
      },
    ],
  },
  error: null,
};

async function login(page: import("@playwright/test").Page): Promise<void> {
  await page.goto("/login");
  await page.getByTestId("login-token").fill(SAMPLE_JWT);
  await page.getByTestId("login-submit").click();
  await expect(page).toHaveURL(/\/agents$/);
}

test("system_admin sees platform credential tables + passes axe", async ({
  page,
}) => {
  await page.route("**/v1/me", async (route) => {
    await route.fulfill({ json: SYS_ADMIN_ME });
  });
  await page.route("**/v1/platform/credentials", async (route) => {
    await route.fulfill({ json: VIEW });
  });
  await login(page);
  await page.goto("/settings/platform");

  await expect(page.getByTestId("pc-providers-table")).toBeVisible();
  await expect(page.getByText("anthropic", { exact: true })).toBeVisible();
  // The per-tool credential table is gone — keyless SearXNG web_search made
  // it dead config (see SettingsPlatformConfig).
  await expect(page.getByTestId("pc-tools-table")).not.toBeVisible();
  // B-51 —— qwen 未设置但向量化在用它:行上警示 + 顶部 banner。已配好的
  // anthropic 一行不许染色。
  await expect(page.getByTestId("pc-source-qwen")).toContainText(
    "needed by the platform",
  );
  await expect(
    page.getByTestId("pc-platform-credentials-missing"),
  ).toContainText("qwen");
  await expect(page.getByTestId("pc-source-anthropic")).not.toContainText(
    "needed by the platform",
  );
  await expectNoA11yViolations(page, "/settings/platform");
});

test("system_admin adds a key — PUT to the key endpoint carries value + priority (Y-MK)", async ({
  page,
}) => {
  await page.route("**/v1/me", async (route) => {
    await route.fulfill({ json: SYS_ADMIN_ME });
  });
  await page.route("**/v1/platform/credentials", async (route) => {
    await route.fulfill({ json: VIEW });
  });
  await page.route(
    "**/v1/platform/credentials/providers/anthropic/keys/acct-2",
    async (route) => {
      await route.fulfill({
        json: {
          success: true,
          data: {
            key_id: "acct-2",
            secret_ref: "secret://expert-work/platform/llm/anthropic/acct-2",
            enabled: true,
            priority: 100,
          },
          error: null,
        },
      });
    },
  );
  await login(page);
  await page.goto("/settings/platform");

  await page.getByTestId("pc-add-key-anthropic").click();
  // The mode toggle confirms the modal opened (antd keeps a hidden Modal-root
  // wrapper in the DOM, so don't assert on the root's visibility).
  await expect(page.getByTestId("pc-edit-mode")).toBeVisible();
  await page.getByTestId("pc-edit-key-id").fill("acct-2");
  // Default mode is "paste a key" — a password input (not echoed). antd
  // Input.Password forwards data-testid to the input element itself.
  const valueInput = page.getByTestId("pc-edit-value");
  await expect(valueInput).toBeVisible();
  await expect(valueInput).toHaveAttribute("type", "password");
  await valueInput.fill("sk-ant-REAL-KEY");
  await expectNoA11yViolations(page, "/settings/platform (add-key modal)");

  const [req] = await Promise.all([
    page.waitForRequest(
      "**/v1/platform/credentials/providers/anthropic/keys/acct-2",
    ),
    page.getByRole("button", { name: "Save" }).click(),
  ]);
  const body = req.postDataJSON();
  expect(body.value).toBe("sk-ant-REAL-KEY");
  expect(body.priority).toBe(100);
  expect(body.secret_ref).toBeUndefined();
});

// B-308 —— 请求地址:列表只显示主机、编辑弹窗带地址、连通测试失败给「仍然保存」。
const B308_VIEW = {
  success: true,
  data: {
    providers: [
      {
        provider: "openai",
        source: "db",
        secret_ref: "secret://expert-work/platform/llm/openai",
        enabled: true,
        official_base_url: "https://api.openai.com/v1",
        keys: [
          {
            key_id: "default",
            secret_ref: "secret://expert-work/platform/llm/openai",
            enabled: true,
            priority: 100,
            base_url: null,
            base_url_unverified: false,
          },
          {
            key_id: "relay",
            secret_ref: "secret://expert-work/platform/llm/openai/relay",
            enabled: true,
            priority: 200,
            base_url: "https://relay.example.com/team-token/v1",
            base_url_unverified: true,
          },
        ],
        used_by_agents: 1,
        tenant_override_count: 0,
      },
    ],
    tools: [],
  },
  error: null,
};

test("system_admin sets a request address — check fails, then 'save anyway' resends with force_save (B-308)", async ({
  page,
}) => {
  await page.route("**/v1/me", async (route) => {
    await route.fulfill({ json: SYS_ADMIN_ME });
  });
  await page.route("**/v1/platform/credentials", async (route) => {
    await route.fulfill({ json: B308_VIEW });
  });
  const bodies: Record<string, unknown>[] = [];
  await page.route(
    "**/v1/platform/credentials/providers/openai/keys/default",
    async (route) => {
      bodies.push(route.request().postDataJSON());
      if (bodies.length === 1) {
        await route.fulfill({
          status: 422,
          json: {
            detail: {
              code: "BASE_URL_CHECK_FAILED",
              message: "the connectivity check of the request address failed",
              reason: "vendor_error",
              status: 502,
              interface: "chat",
            },
          },
        });
        return;
      }
      await route.fulfill({
        json: {
          success: true,
          data: {
            ...B308_VIEW.data.providers[0].keys[0],
            base_url: "https://relay.example.com/v1",
            base_url_unverified: true,
          },
          error: null,
        },
      });
    },
  );
  await login(page);
  await page.goto("/settings/platform");

  await page.getByRole("button", { name: "Expand row" }).click();
  // The list shows the host and the "check failed" marker, never the path.
  const relayCell = page.getByTestId("pc-key-base-url-openai-relay");
  await expect(relayCell).toContainText("relay.example.com");
  await expect(relayCell).toContainText("Check failed");
  await expect(relayCell).not.toContainText("team-token");
  await expect(page.getByTestId("pc-key-base-url-openai-default")).toContainText(
    "Official",
  );

  await page.getByTestId("pc-key-edit-openai-default").click();
  const address = page.getByTestId("pc-edit-base-url");
  await expect(address).toBeVisible();
  await expect(address).toHaveAttribute("placeholder", "https://api.openai.com/v1");
  await address.fill("https://relay.example.com/v1");
  await expect(page.getByTestId("pc-edit-key-hint")).toContainText(
    "also enter this service's key",
  );
  await page.getByTestId("pc-edit-value").fill("sk-relay-KEY");
  await page.getByRole("button", { name: "Save" }).click();

  const failed = page.getByTestId("pc-edit-check-failed");
  await expect(failed).toContainText("provider error (502)");
  await expect(failed).toContainText("chat");
  await expectNoA11yViolations(page, "/settings/platform (check failed)");
  expect(bodies[0].base_url).toBe("https://relay.example.com/v1");
  expect(bodies[0].force_save).toBeUndefined();

  await page.getByTestId("pc-edit-force-save").check();
  await page.getByRole("button", { name: "Save" }).click();
  await expect.poll(() => bodies.length).toBe(2);
  expect(bodies[1].force_save).toBe(true);
  expect(bodies[1].value).toBe("sk-relay-KEY");
});

test("non-admin sees system-admin-only notice + passes axe", async ({
  page,
}) => {
  await login(page);
  await page.goto("/settings/platform");

  await expect(page.getByTestId("pc-not-admin")).toBeVisible();
  await expect(page.getByTestId("pc-providers-table")).toHaveCount(0);
  await expectNoA11yViolations(page, "/settings/platform");
});
