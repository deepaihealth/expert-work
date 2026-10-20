/**
 * Conversation detail — trajectory layout E2E (B-202).
 *
 * ② The ledger and the details panel are written for a bounded parent: the
 *   debug console's grid gives them one, the conversation page did not, so a
 *   long trajectory grew the whole page and a click on a row near the bottom
 *   opened the panel at the top of the page — outside the viewport. Real
 *   layout only (jsdom has none), hence a browser test.
 * ③ The 概要 preview is cut at a fixed height; it must say so (「查看全文」)
 *   and never stop inside a table (the bug left a header row and blank
 *   space). Also real layout.
 *
 * Every request the page makes is stubbed: the conversation, its paired
 * messages / runs, and one run's SSE replay (31 model calls, 30 tool calls).
 */
import { test, expect, SAMPLE_JWT } from "./fixtures";
import type { Page, Route } from "@playwright/test";

const THREAD = "66666666-6666-6666-6666-666666666666";
const RUN = "77777777-7777-7777-7777-777777777777";
const TENANT = "22222222-2222-2222-2222-222222222222";
const BASE_MS = 1_792_481_680_000;
const STEPS = 30;

/** 结论 + 几段文字 + 一张表 —— 表头恰好落在预览上限附近。 */
const ANSWER = [
  "## 结论",
  "",
  "第一段:先看指标。",
  "",
  "第二段:再看趋势。",
  "",
  "第三段:最后给建议。",
  "",
  "第四段:细节见下表。",
  "",
  "| 指标 | 数值 |",
  "| --- | --- |",
  "| 血压 | 128/82 |",
  "| 血糖 | 5.6 |",
  "| 心率 | 72 |",
  "| 体重 | 64 |",
].join("\n");

function sseBody(): string {
  const frames: Array<{ id: string; event: string; data: unknown }> = [];
  let seq = 0;
  const id = (): string => {
    seq += 1;
    return `${BASE_MS + seq * 1000}-${seq}`;
  };
  frames.push({ id: id(), event: "metadata", data: { run_id: RUN } });
  for (let i = 1; i <= STEPS; i += 1) {
    frames.push({
      id: id(),
      event: "updates",
      data: {
        agent: {
          step_count: i,
          _duration_ms: 900,
          messages: [
            {
              type: "ai",
              content: "",
              tool_calls: [{ id: `c${i}`, name: "web_search", args: { q: `query ${i}` }, type: "tool_call" }],
            },
          ],
        },
      },
    });
    frames.push({
      id: id(),
      event: "updates",
      data: {
        tools: {
          messages: [
            { type: "tool", tool_call_id: `c${i}`, name: "web_search", content: `hit ${i}`, status: "success" },
          ],
        },
      },
    });
  }
  frames.push({
    id: id(),
    event: "updates",
    data: { agent: { step_count: STEPS + 1, messages: [{ type: "ai", content: ANSWER }] } },
  });
  return (
    frames
      .map((f) => `id: ${f.id}\nevent: ${f.event}\ndata: ${JSON.stringify(f.data)}\n`)
      .join("\n") + "\nevent: end\ndata: {}\n\n"
  );
}

const ok = (data: unknown) => ({ success: true, error: null, data });

async function openConversation(page: Page): Promise<void> {
  await page.route(`**/v1/conversations/${THREAD}`, async (route: Route) => {
    await route.fulfill({
      json: ok({
        thread_id: THREAD,
        tenant_id: TENANT,
        user_id: null,
        agent_name: "demo-agent",
        agent_version: "1.0.0",
        title: "long trajectory",
        status: "active",
        created_at: "2026-10-20T07:34:00Z",
        updated_at: "2026-10-20T07:44:00Z",
        run_count: 1,
        error_count: 0,
        pending_count: 0,
        last_run_at: "2026-10-20T07:34:00Z",
        tokens: null,
        runs: [
          {
            run_id: RUN,
            thread_id: THREAD,
            user_id: null,
            status: "success",
            is_resume: false,
            error: null,
            created_at: "2026-10-20T07:34:00Z",
            updated_at: "2026-10-20T07:44:00Z",
            finished_at: "2026-10-20T07:44:00Z",
            trace_id: null,
            agent_spec_sha256: null,
            tokens: null,
          },
        ],
      }),
    });
  });
  // URL-predicate matchers (see run_detail.spec.ts): these carry ``?tenant_id=``.
  await page.route(
    (url) => url.pathname === `/v1/sessions/${THREAD}/messages`,
    async (route: Route) => {
      await route.fulfill({
        json: ok({
          messages: [
            { role: "user", content: "帮我看看这份体检报告" },
            { role: "assistant", content: ANSWER },
          ],
        }),
      });
    },
  );
  await page.route(
    (url) => url.pathname === `/v1/sessions/${THREAD}/feedback`,
    async (route: Route) => {
      await route.fulfill({ json: { items: [] } });
    },
  );
  await page.route(
    (url) => url.pathname === `/v1/sessions/${THREAD}/runs`,
    async (route: Route) => {
      await route.fulfill({
        json: ok({
          runs: [
            { run_id: RUN, status: "success", is_resume: false, created_at: "2026-10-20T07:34:00Z", tokens: null },
          ],
        }),
      });
    },
  );
  await page.route(
    (url) => url.pathname === `/v1/sessions/${THREAD}/runs/${RUN}/events`,
    async (route: Route) => {
      await route.fulfill({ status: 200, contentType: "text/event-stream", body: sseBody() });
    },
  );
  await page.route(
    (url) => url.pathname === `/v1/sessions/${THREAD}/runs/${RUN}/trace`,
    async (route: Route) => {
      await route.fulfill({ json: { status: "no_trace" } });
    },
  );

  await page.goto("/login");
  await expect(page.getByTestId("login-card")).toBeVisible();
  const tokenField = page.getByTestId("login-token");
  if (!(await tokenField.isVisible())) {
    await page.getByTestId("login-dev-toggle").click();
  }
  await tokenField.fill(SAMPLE_JWT);
  await page.getByTestId("login-submit").click();
  await expect(page).toHaveURL(/\/agents$/);
  await page.goto(`/conversations/${THREAD}`);
  await page.getByTestId("console-view-tab-trajectory").click();
  // The replay landed (not just the unreplayed placeholder): the last tool
  // call is in the ledger.
  await expect(
    page.getByTestId("console-traj-row").filter({ hasText: "query 30" }),
  ).toHaveCount(1);
}

test("② a long trajectory: clicking a row near the bottom keeps the details panel in view", async ({
  page,
}) => {
  await openConversation(page);
  const rows = page.getByTestId("console-traj-row");
  // user + 31 assistant + 30 tool records — far taller than the viewport.
  const last = rows.last();
  await last.click();
  await expect(last).toHaveAttribute("data-selected", "true");
  // Header and tab bar sit at the panel's top: with the page-height ledger
  // they were scrolled out of view, with the bounded box they stay in it.
  await expect(page.getByTestId("console-detail-header")).toBeInViewport({ ratio: 1 });
  await expect(page.getByTestId("console-detail-tab-summary")).toBeInViewport({ ratio: 1 });
  // …and the ledger scrolls inside its own box instead of growing the page.
  const box = await page.getByTestId("conversation-trajectory-box").boundingBox();
  const viewport = page.viewportSize();
  expect(box).not.toBeNull();
  expect(viewport).not.toBeNull();
  expect(box!.height).toBeLessThanOrEqual(viewport!.height);
});

test("③ the summary preview of a long answer says it is cut and never stops inside the table", async ({
  page,
}) => {
  await openConversation(page);
  await page.getByTestId("console-traj-row").filter({ hasText: "结论" }).click();
  const section = page.getByTestId("console-detail-section-preview");
  const preview = section.locator(".ew-detail__preview");
  await expect(preview).toHaveAttribute("data-truncated", "true");
  const more = page.getByTestId("console-detail-section-more-preview");
  await expect(more).toBeVisible();

  // No table row straddles the cut (each one ends above it or starts below
  // it), and the cut never leaves the header row alone above it.
  const clip = await preview.boundingBox();
  expect(clip).not.toBeNull();
  const cutAt = clip!.y + clip!.height;
  const trs = preview.locator("tr");
  const n = await trs.count();
  expect(n).toBeGreaterThan(0);
  let above = 0;
  for (let i = 0; i < n; i += 1) {
    const r = await trs.nth(i).boundingBox();
    expect(r).not.toBeNull();
    const straddles = r!.y < cutAt - 0.5 && r!.y + r!.height > cutAt + 0.5;
    expect(straddles, `row ${i} straddles the preview cut`).toBe(false);
    if (r!.y + r!.height <= cutAt + 0.5) above += 1;
  }
  expect(above === 0 || above >= 2, `${above} table row(s) above the cut`).toBe(true);

  // 「查看全文」 opens the preview tab, where the whole table is there.
  await more.click();
  await expect(page.getByTestId("console-detail-tab-preview")).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("cell", { name: "64" })).toBeVisible();
});
