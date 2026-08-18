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

// 一帧 metadata(带 run_id)+ 一帧 end —— run_id 是右栏头部「Run 详情」链接
// (§八.6)的前提。
const SSE_BODY = [
  "event: metadata",
  'data: {"run_id":"44444444-4444-4444-4444-444444444444"}',
  "",
  "event: end",
  'data: "ok"',
  "",
  "",
].join("\n");

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
  // 空态(还没跑)的一次扫描 —— 过程条 / 泳道 / 行表 / 详情都还不在 DOM 里。
  await expectNoA11yViolations(page, "/agents/playground (empty)");

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

  // §八.7 —— 泳道块可点:真浏览器里验证「块点击没被横向拖选的指针捕获吃
  // 掉」(jsdom 没有 pointer capture,只有这里能证)。哪怕这个桩 run 只有一
  // 帧,USER 行也一定有一个块。
  const block = page.getByTestId("console-lane-block").first();
  await expect(block).toBeVisible();
  await block.click();
  await expect(page.getByTestId("console-detail-header")).toBeVisible();
  // §八.6 —— 「查看运行」的新家:右栏头部的 Run 详情链接。
  await expect(page.getByTestId("console-inspect-run-link")).toHaveAttribute(
    "href",
    "/runs/33333333-3333-3333-3333-333333333333/44444444-4444-4444-4444-444444444444",
  );

  // 真正扫得到新 UI 的那一次:run 结束 + 点过泳道块之后,过程条、轮次脚注、
  // 泳道、行表(listbox)、右栏详情全在 DOM 里。空态那次一个新组件都覆盖不到。
  await expectNoA11yViolations(page, "/agents/playground (after run)");

  expect(runBody).toEqual({
    input: "describe this image",
    image_refs: ["expert_work://image/demo.png"],
  });
  // Attachment chip cleared after the turn consumed it.
  await expect(page.getByTestId("playground-attachment")).toHaveCount(0);
});
