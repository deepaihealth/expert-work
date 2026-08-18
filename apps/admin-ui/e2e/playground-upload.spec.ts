/**
 * Playground image-upload e2e — Stream P (PR M, Mini-ADR P-16).
 *
 * Drives the multimodal input path end to end against mocked routes:
 * the thread is created lazily on the first action (attaching an image →
 * mocked ``POST /v1/sessions`` then ``POST .../uploads`` → ``expert_work://image/
 * ...``), and Run posts the SSE stream with that ref in ``image_refs``.
 * Also runs axe on the tab.
 */
import { test, expect, expectNoA11yViolations, SAMPLE_JWT } from "./fixtures";

const AGENT_DETAIL = {
  success: true,
  data: {
    record: {
      id: "11111111-1111-1111-1111-111111111111",
      tenant_id: "22222222-2222-2222-2222-222222222222",
      name: "demo-agent",
      version: "1.0.0",
      status: "active",
      spec_sha256: "a".repeat(64),
      created_by: "alice@acme.com",
      created_at: "2026-04-12T09:00:00Z",
      updated_at: "2026-05-25T07:00:00Z",
      spec: {},
    },
  },
  error: null,
};

const THREAD = {
  success: true,
  data: {
    thread_id: "33333333-3333-3333-3333-333333333333",
    tenant_id: "22222222-2222-2222-2222-222222222222",
    agent_name: "demo-agent",
    agent_version: "1.0.0",
    user_id: null,
    status: "active",
    created_by: "u",
    created_at: "2026-05-25T00:00:00Z",
    updated_at: "2026-05-25T00:00:00Z",
  },
  error: null,
};

const SSE_BODY = ["event: end", 'data: "ok"', "", ""].join("\n");

async function login(page: import("@playwright/test").Page): Promise<void> {
  await page.goto("/login");
  await page.getByTestId("login-token").fill(SAMPLE_JWT);
  await page.getByTestId("login-submit").click();
  await expect(page).toHaveURL(/\/agents$/);
}

test("attach image, run, and send image_refs + pass axe", async ({ page }) => {
  // Specific routes win over the fixture defaults (LIFO).
  await page.route("**/v1/agents/demo-agent/1.0.0", async (route) => {
    await route.fulfill({ json: AGENT_DETAIL });
  });
  await page.route("**/v1/sessions", async (route) => {
    await route.fulfill({ status: 201, json: THREAD });
  });
  await page.route("**/v1/sessions/*/uploads", async (route) => {
    await route.fulfill({
      status: 201,
      json: { image_ref: "expert_work://image/demo.png" },
    });
  });

  let runBody: { input?: string; image_refs?: string[] } | null = null;
  await page.route("**/v1/sessions/*/runs", async (route) => {
    runBody = route.request().postDataJSON();
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: SSE_BODY,
    });
  });

  await login(page);
  await page.goto("/agents/demo-agent/1.0.0/playground");

  // Lazy — no thread is created on mount. Attaching an image is the first
  // action, so it creates the thread (uploads are thread-scoped) then uploads.
  await page.getByTestId("playground-file-input").setInputFiles({
    name: "shot.png",
    mimeType: "image/png",
    buffer: Buffer.from([0x89, 0x50, 0x4e, 0x47]),
  });
  await expect(page.getByTestId("playground-attachment")).toHaveText(
    /shot\.png/,
  );
  // The lazy createSession fired — the thread id now shows in the header.
  await expect(page.getByText(/33333333-3333-3333/)).toBeVisible();

  await page.getByTestId("playground-input").fill("describe this image");
  await expectNoA11yViolations(page, "/agents/playground");

  await page.getByTestId("playground-run").click();
  // The turn lands in the transcript and settles as soon as the stub's ``end``
  // frame arrives (the raw event view is gone; per-frame detail now lives in
  // the right rail's Raw tab).
  const turn = page.getByTestId("console-turn");
  await expect(turn).toBeVisible();
  await expect(turn.getByText("describe this image")).toBeVisible();
  await expect(turn.getByTestId("console-turn-status")).toHaveText(
    /done|完成/i,
  );

  expect(runBody).toEqual({
    input: "describe this image",
    image_refs: ["expert_work://image/demo.png"],
  });
  // Attachment chip cleared after the turn consumed it.
  await expect(page.getByTestId("playground-attachment")).toHaveCount(0);
});
