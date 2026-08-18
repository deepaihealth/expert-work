/**
 * Session sidebar e2e — browse / search / resume / rename / archive / purge
 * over the Playground's left-rail session list (the drawer was retired by the
 * three-column console), against mocked routes. Also axe.
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

const A_ID = "aaaaaaaa-0000-0000-0000-00000000000a";
const B_ID = "bbbbbbbb-0000-0000-0000-00000000000b";

function meta(id: string, title: string | null, status = "active") {
  return {
    thread_id: id,
    tenant_id: "22222222-2222-2222-2222-222222222222",
    agent_name: "demo-agent",
    agent_version: "1.0.0",
    user_id: null,
    status,
    title,
    created_by: "u",
    created_at: "2026-05-20T00:00:00Z",
    updated_at: "2026-05-25T00:00:00Z",
  };
}

const LIST = {
  success: true,
  data: {
    items: [meta(A_ID, "Quarterly report"), meta(B_ID, "今天天气", "paused")],
    total: 2,
  },
  error: null,
};

async function login(page: import("@playwright/test").Page): Promise<void> {
  await page.goto("/login");
  await page.getByTestId("login-token").fill(SAMPLE_JWT);
  await page.getByTestId("login-submit").click();
  await expect(page).toHaveURL(/\/agents$/);
}

test("browse, rename, archive, purge, resume + axe", async ({ page }) => {
  await page.route("**/v1/agents/demo-agent/1.0.0", async (route) => {
    await route.fulfill({ json: AGENT_DETAIL });
  });

  const calls: { patch?: unknown; deleted?: string; purged?: string } = {};
  await page.route("**/v1/sessions**", async (route) => {
    const req = route.request();
    const method = req.method();
    const url = req.url();
    // A thread subpath (workspace / files / messages / single get) must return
    // its own shape — the greedy glob also matches these, and serving them the
    // session-LIST envelope corrupts the Playground's state (crash on render).
    if (method === "GET" && url.includes("/workspace/files")) {
      await route.fulfill({ json: { success: true, data: { files: [] } } });
    } else if (method === "GET" && url.includes("/workspace")) {
      await route.fulfill({
        json: { success: true, data: { workspace: null, artifacts: [] } },
      });
    } else if (method === "GET" && url.includes("/messages")) {
      await route.fulfill({ json: { success: true, data: { messages: [] } } });
    } else if (method === "GET" && url.includes("/runs")) {
      await route.fulfill({ json: { success: true, data: { runs: [] } } });
    } else if (method === "GET" && url.includes("/plan")) {
      // No persisted plan for this thread (204, raw body — not enveloped).
      await route.fulfill({ status: 204, body: "" });
    } else if (method === "GET" && /\/v1\/sessions\/[^/?]+(\?|$)/.test(url)) {
      // GET /v1/sessions/{id} — single session meta.
      await route.fulfill({
        json: { success: true, data: meta(B_ID, "今天天气", "paused") },
      });
    } else if (method === "GET") {
      await route.fulfill({ json: LIST });
    } else if (method === "PATCH") {
      calls.patch = req.postDataJSON();
      await route.fulfill({
        json: { success: true, data: meta(A_ID, "Renamed thread") },
      });
    } else if (method === "DELETE") {
      calls.deleted = url;
      await route.fulfill({
        json: { success: true, data: { archived: A_ID } },
      });
    } else if (method === "POST" && url.includes(":purge")) {
      calls.purged = url;
      await route.fulfill({ json: { success: true, data: { purged: A_ID } } });
    } else {
      await route.fulfill({
        status: 201,
        json: { success: true, data: meta(A_ID, null) },
      });
    }
  });

  await login(page);
  await page.goto("/agents/demo-agent/1.0.0/playground");

  // The sidebar is always mounted on a desktop viewport → the list loads on
  // page load with human titles; no drawer to open.
  await expect(page.getByTestId("console-session-sidebar")).toBeVisible();
  await expect(page.getByText("Quarterly report")).toBeVisible();
  await expect(page.getByText("今天天气")).toBeVisible();
  await expectNoA11yViolations(page, "/agents/playground/history");

  // Search debounces into ?q=.
  const listReq = page.waitForRequest(
    (r) => r.url().includes("/v1/sessions") && r.url().includes("q=report"),
  );
  await page.getByTestId("console-session-search").fill("report");
  await listReq;
  await page.getByTestId("console-session-search").fill("");

  // Status filter is a two-value Segmented (Active / Archived) → ?status=.
  // Pick "archived" (how soft-deleted threads become visible again), then
  // switch back so the rows below are the active ones.
  const statusReq = page.waitForRequest(
    (r) =>
      r.url().includes("/v1/sessions") && r.url().includes("status=archived"),
  );
  await page
    .getByTestId("console-session-filter")
    .getByText(/archived|已归档/i)
    .click();
  await statusReq;
  await page
    .getByTestId("console-session-filter")
    .getByText(/^(active|活跃)$/i)
    .click();

  // §八.1 —— 行内动作必须**键盘**可达:从搜索框起连按 Tab,重命名按钮要能拿到
  // 焦点。`display:none` 的元素聚焦不了(`:focus-within` 因此永远触发不了),
  // 所以这条把「隐藏用 opacity、别用 display」钉死。
  await page.getByTestId("console-session-search").focus();
  const renameA = page.getByTestId(`console-session-rename-${A_ID}`);
  // e2e 走 tsconfig.node.json(`lib: ["ES2023"]`,没有 DOM),所以别用
  // `page.evaluate(() => document…)` —— 用 `:focus` 选择器读当前焦点。
  const focusedTestId = async (): Promise<string | null> => {
    const focused = page.locator(":focus");
    return (await focused.count()) === 1 ? await focused.getAttribute("data-testid") : null;
  };
  for (let i = 0; i < 10; i += 1) {
    if ((await focusedTestId()) === `console-session-rename-${A_ID}`) break;
    await page.keyboard.press("Tab");
  }
  await expect(renameA).toBeFocused();

  // Rename → PATCH with the new title. §八.1 —— 行内三个图标从常驻改成 hover
  // 才浮出来(``opacity:0`` 起步,键盘聚焦时同样浮出),所以每次点之前先把
  // 指针放到行上。
  await page.getByTestId(`console-session-item-${A_ID}`).hover();
  await page.getByTestId(`console-session-rename-${A_ID}`).click();
  await page.getByTestId("console-session-rename-input").fill("Renamed thread");
  await page.getByRole("button", { name: /save|保存/i }).click();
  await expect.poll(() => calls.patch).toEqual({ title: "Renamed thread" });

  // Archive → DELETE after confirmation. Scope the OK to the open popconfirm
  // (its label collides with the rows' archive icon-button aria-labels).
  await page.getByTestId(`console-session-item-${A_ID}`).hover();
  await page.getByTestId(`console-session-archive-${A_ID}`).click();
  await page
    .locator(".ant-popconfirm-buttons")
    .getByRole("button", { name: /^(archive|归档)$/i })
    .click();
  await expect.poll(() => calls.deleted).toContain(`/v1/sessions/${A_ID}`);

  // Purge → POST :purge after the danger confirmation (same scoping).
  await page.getByTestId(`console-session-item-${A_ID}`).hover();
  await page.getByTestId(`console-session-purge-${A_ID}`).click();
  await page
    .locator(".ant-popconfirm-buttons")
    .getByRole("button", { name: /delete forever|彻底删除/i })
    .click();
  await expect.poll(() => calls.purged).toContain(`${A_ID}:purge`);

  // Resume → click a row → the thread id shows in the header (the old
  // "resumed" banner is gone; the highlighted sidebar row carries that state).
  // Click the title, not the row's geometric centre — in the narrow rail the
  // action icons wrap under the title and the centre lands on "Rename".
  await page
    .getByTestId(`console-session-item-${B_ID}`)
    .getByText("今天天气")
    .click();
  await expect(page.getByTestId("console-thread-id")).toContainText(B_ID);
});
