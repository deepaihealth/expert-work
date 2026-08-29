/**
 * Manifest edit-via-form E2E — Stream S PR E, selectors migrated to the
 * group-nav + detail-pane layout (agent-config-page redesign PR1).
 *
 * Proves an admin can open the agent-detail "配置清单"/Manifest tab — which now
 * renders the visual ``<ManifestEditor>`` form by default (no view/edit
 * toggle) — flip to the raw YAML escape hatch via the top-right toggle
 * button, and Save, firing ``PUT /v1/agents/{name}/{version}``. A second
 * test runs axe over the editor.
 *
 * The editor fetches ``GET /v1/agents/schema`` and ``GET /v1/model-catalog``
 * (both enveloped) on mount; the shared ``installControlPlaneStub`` fixture
 * stubs the agent LIST (``**​/v1/agents*``) but NOT the detail
 * (``/v1/agents/{name}/{version}``) or the PUT, so we register those here.
 * Because the fixture's ``**​/v1/agents*`` glob also matches the detail and
 * schema paths, we register our more-specific routes *after* it — Playwright
 * runs the most-recently-added handler first, so ours win. The detail route
 * reuses the fixture's demo agent (``customer-support-bot`` / ``3.4.2``).
 */
import { test, expect, expectNoA11yViolations, SAMPLE_JWT } from "./fixtures";

const AGENT_NAME = "customer-support-bot";
const AGENT_VERSION = "3.4.2";

// spec.model is an OBJECT (provider/name/supports_vision); the curated form
// reads it into the ModelSelect picker — same shape as manifest-model-select.
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

// Full ``AgentDetailResponse`` envelope — record carries every list field plus
// the full manifest ``spec`` (apiVersion/kind/metadata/spec with a model obj).
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
          model: {
            provider: "openai",
            name: "gpt-5.5",
            supports_vision: true,
          },
          system_prompt: "You are a helpful customer-support assistant.",
        },
      },
    },
  },
};

test.beforeEach(async ({ page }) => {
  // More specific than the fixture's ``**/v1/agents*`` stub; registered after
  // it so it wins for the schema fetch.
  await page.route("**/v1/agents/schema", async (route) => {
    await route.fulfill({ json: SCHEMA_ENVELOPE });
  });
  await page.route("**/v1/model-catalog", async (route) => {
    await route.fulfill({ json: CATALOG_ENVELOPE });
  });
  // Detail GET + Save PUT on the same path — branch on method. The PUT returns
  // the same detail so the parent's post-save refetch succeeds.
  await page.route(
    `**/v1/agents/${AGENT_NAME}/${AGENT_VERSION}`,
    async (route) => {
      await route.fulfill({ json: DETAIL_ENVELOPE });
    },
  );

  await page.goto("/login");
  await expect(page.getByTestId("login-card")).toBeVisible();
  // The paste-token form sits behind the "Developer login" disclosure whenever
  // OIDC is configured (``VITE_OIDC_ISSUER``). In CI it is open by default;
  // locally it may be collapsed — reveal it if needed.
  const tokenField = page.getByTestId("login-token");
  if (!(await tokenField.isVisible())) {
    await page.getByTestId("login-dev-toggle").click();
  }
  await tokenField.fill(SAMPLE_JWT);
  await page.getByTestId("login-submit").click();
  await expect(page).toHaveURL(/\/agents$/);
  await expect(page.getByText(AGENT_NAME)).toBeVisible();

  await page.goto(`/agents/${AGENT_NAME}/${AGENT_VERSION}/manifest`);
});

test("edit a manifest via the form", async ({ page }) => {
  // Visual editor mounts directly on its Form tab — no view/edit toggle.
  await expect(page.getByTestId("manifest-tab")).toBeVisible();
  await expect(page.getByTestId("manifest-editor-edit")).toBeVisible();
  await expect(page.getByTestId("manifest-form-view")).toBeVisible();
  await expect(page.getByTestId("manifest-save-btn")).toBeVisible();
  await expect(page.getByTestId("manifest-reset-btn")).toBeVisible();

  // Switch to the raw YAML escape hatch via the top-right toggle button
  // (replaces the old flat-tab row's "yaml" tab).
  await page.getByTestId("cfg-yaml-toggle").click();
  await expect(page.getByTestId("manifest-yaml-view")).toBeVisible();

  // Save fires the PUT; the editor stays mounted.
  const putPromise = page.waitForRequest(
    (req) =>
      req.method() === "PUT" &&
      req.url().includes(`/v1/agents/${AGENT_NAME}/${AGENT_VERSION}`),
  );
  await page.getByTestId("manifest-save-btn").click();
  const put = await putPromise;
  // 并发编辑保护:请求必须真的带上编辑时读到的那一版 sha。单元测试只验到
  // updateAgent 的实参,验不到它有没有变成 HTTP 头(axios config 写错就是这
  // 两层之间的洞)。
  expect(put.headers()["if-match"]).toBe("a".repeat(64));
  await expect(page.getByTestId("manifest-editor-edit")).toBeVisible();
});

test("editor passes axe (serious + critical)", async ({ page }) => {
  await expect(page.getByTestId("manifest-editor-edit")).toBeVisible();
  await expectNoA11yViolations(page, "manifest-tab");
});
