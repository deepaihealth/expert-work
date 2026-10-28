/**
 * Knowledge retrieval records + upgraded retrieval test E2E — B-175 P2-PR4.
 *
 * The records tab lists a base's searches and opens one: its question, each
 * result's document (or that it was deleted), four scores and live text, and
 * the agent's conversation. The retrieval test shows four scores and saves
 * the changed parameters (only those) as the base's defaults. Responses come
 * from the shared contract fixtures the backend tests check its keys against.
 */
import { readFileSync } from "node:fs";

import { test, expect, expectNoA11yViolations, SAMPLE_JWT } from "./fixtures";

function fixture(name: string): Record<string, unknown> {
  const url = new URL(`../src/api/__fixtures__/${name}`, import.meta.url);
  return JSON.parse(readFileSync(url, "utf-8")) as Record<string, unknown>;
}

const LOGS = fixture("knowledge_retrieval_logs.json") as {
  page: { logs: { id: string; thread_id: string }[] };
  detail: unknown;
  passages: unknown;
};
const TESTED = fixture("knowledge_retrieval_test.json");
const [ROW] = LOGS.page.logs;

const BASE = {
  id: "11111111-1111-1111-1111-111111111111",
  name: "support-docs",
  description: "Customer FAQ",
  chunk_max_tokens: 512,
  chunk_overlap_tokens: 64,
  created_at: "2026-06-12T00:00:00Z",
  updated_at: "2026-06-12T00:00:00Z",
  created_by: "alice@acme.com",
  retrieval_config: { top_k: 5, score_threshold: null, method: "hybrid", rerank_enabled: true },
  embedding_provider: "qwen",
  embedding_model: "text-embedding-v4",
  needs_reindex: false,
  reindexing: false,
  stats: { document_count: 1, chunk_count: 12 },
};

test.beforeEach(async ({ page }) => {
  // Predicates, not globs: list reads carry ``?limit=&offset=``.
  await page.route(
    (url) => url.pathname === "/v1/knowledge/bases",
    (route) => route.fulfill({ json: { bases: [BASE], total: 1, offset: 0, limit: 20 } }),
  );
  await page.route(
    (url) => url.pathname === "/v1/knowledge/bases/support-docs",
    (route) => route.fulfill({ json: BASE }),
  );
  await page.route(
    (url) => url.pathname === "/v1/knowledge/bases/support-docs/retrieval-logs",
    (route) => route.fulfill({ json: LOGS.page }),
  );
  await page.route(
    (url) => url.pathname === `/v1/knowledge/bases/support-docs/retrieval-logs/${ROW.id}`,
    (route) => route.fulfill({ json: LOGS.detail }),
  );
  await page.route(
    (url) =>
      url.pathname === `/v1/knowledge/bases/support-docs/retrieval-logs/${ROW.id}/passages`,
    (route) => route.fulfill({ json: LOGS.passages }),
  );
  await page.route("**/v1/knowledge/bases/*/test", (route) => route.fulfill({ json: TESTED }));

  await page.goto("/login");
  await expect(page.getByTestId("login-card")).toBeVisible();
  const tokenField = page.getByTestId("login-token");
  if (!(await tokenField.isVisible())) {
    await page.getByTestId("login-dev-toggle").click();
  }
  await tokenField.fill(SAMPLE_JWT);
  await page.getByTestId("login-submit").click();
  await expect(page).toHaveURL(/\/agents$/);
});

test("records tab lists the base's searches and opens one", async ({ page }) => {
  await page.goto("/knowledge/support-docs/logs");
  await expect(page.getByTestId("knowledge-logs-tab")).toBeVisible();
  await expect(page.getByTestId("kb-logs-retention")).toContainText("30");
  await expect(page.getByTestId("kb-logs-table")).toContainText("What is the deductible?");

  await page.getByTestId(`kb-logs-view-${ROW.id}`).click();
  const drawer = page.getByTestId("kb-log-drawer");
  await expect(drawer.getByTestId("kb-log-query")).toHaveText("What is the deductible?");
  await expect(drawer.getByTestId("kb-log-result-doc-0")).toContainText("D12");
  await expect(drawer.getByTestId("kb-log-result-doc-1-deleted")).toBeVisible();
  await expect(drawer.getByTestId("kb-log-result-scores-0")).toContainText("0.9112");
  await expect(drawer.getByTestId("kb-log-result-mark-0")).toHaveText(
    "The deductible is 500 per year.",
  );
  await expect(drawer.getByTestId("kb-log-conversation-link")).toHaveAttribute(
    "href",
    `/conversations/${ROW.thread_id}`,
  );
  // The sample identity is a tenant admin: the run is reachable too.
  await expect(drawer.getByTestId("kb-log-run-link")).toBeVisible();
});

test("records tab and an open record pass axe (serious + critical)", async ({ page }) => {
  await page.goto("/knowledge/support-docs/logs");
  await expect(page.getByTestId("kb-logs-table")).toContainText("What is the deductible?");
  await expectNoA11yViolations(page, "knowledge-logs");
  await page.getByTestId(`kb-logs-view-${ROW.id}`).click();
  await expect(page.getByTestId("kb-log-result-text-0")).toBeVisible();
  await expectNoA11yViolations(page, "knowledge-log-drawer");
});

test("retrieval test shows four scores and saves only the changed defaults", async ({ page }) => {
  const patches: unknown[] = [];
  await page.route(
    (url) => url.pathname === "/v1/knowledge/bases/support-docs",
    async (route) => {
      if (route.request().method() === "PATCH") {
        patches.push(route.request().postDataJSON());
        await route.fulfill({
          json: { ...BASE, retrieval_config: { ...BASE.retrieval_config, top_k: 8 } },
        });
        return;
      }
      await route.fulfill({ json: BASE });
    },
  );
  await page.goto("/knowledge/support-docs/test");
  await page.getByTestId("kb-test-query").fill("deductible");
  await page.getByTestId("kb-test-run").click();
  await expect(page.getByTestId("kb-test-scores-0")).toContainText("0.0328");
  await expect(page.getByTestId("kb-test-doc-0")).toContainText("D12");
  await expect(page.getByTestId("kb-test-logged")).toBeVisible();

  await expect(page.getByTestId("kb-test-save-defaults")).toBeDisabled();
  await page.getByRole("spinbutton", { name: "Top-k" }).fill("8");
  await page.getByTestId("kb-test-save-defaults").click();
  await expect(page.getByTestId("kb-test-save-defaults-changes")).toContainText("5 → 8");
  await page.getByRole("button", { name: "Save as base defaults" }).last().click();
  await expect.poll(() => patches).toEqual([{ retrieval_top_k: 8 }]);
  await expect(page.getByTestId("kb-test-save-defaults")).toBeDisabled();
  // The results stay: the page took the saved base without reloading.
  await expect(page.getByTestId("kb-test-scores-0")).toBeVisible();

  await page.getByTestId("kb-test-open-logs").click();
  await expect(page).toHaveURL(/\/knowledge\/support-docs\/logs$/);
});
