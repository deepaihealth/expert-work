/**
 * Knowledge E2E — B-222 PR-2: estimate the cost before ingest, confirm before
 * it starts (mocked control plane).
 *
 * A waiting row shows its estimate and days left and is confirmed by its
 * estimate id; the banner adds the base up; an upload's queue row follows its
 * document to 「待确认」. The page with the estimate parts passes axe.
 */
import { test, expect, expectNoA11yViolations, SAMPLE_JWT } from "./fixtures";

const BASE = {
  id: "11111111-1111-1111-1111-111111111111",
  name: "support-docs",
  description: null,
  chunk_max_tokens: 400,
  chunk_overlap_tokens: 0,
  created_at: "2026-10-28T00:00:00Z",
  updated_at: "2026-10-28T00:00:00Z",
  retrieval_config: { top_k: 5, score_threshold: null, method: "hybrid", rerank_enabled: false },
  needs_reindex: false,
  reindexing: false,
  stats: { document_count: 1, chunk_count: 0 },
};

const IN_7_DAYS = new Date(Date.now() + 6.5 * 24 * 3600 * 1000).toISOString();

function estimate(id: string, totalMicros: number) {
  return {
    id,
    version: 1,
    origin: "upload",
    estimated: true,
    created_at: "2026-10-28T00:00:00Z",
    duplicate_of: null,
    allow_duplicate: false,
    currency: "CNY",
    total_micros: totalMicros,
    unpriced: false,
    notes: ["default_average"],
    counts: { chunks: 3, scanned_pages: 0, figures: 0, pages_over_limit: 0 },
    items: [
      {
        kind: "embedding",
        units: 3,
        unit: "chunks",
        reused_units: 0,
        model: "acme/embed-1",
        input_tokens: 900,
        output_tokens: 0,
        micros: totalMicros,
        basis: "local_count",
        priced: true,
      },
    ],
    confirmed_at: null,
    confirmed_by: null,
  };
}

const WAITING = {
  id: "22222222-2222-2222-2222-222222222221",
  filename: "handbook.md",
  status: "awaiting_confirmation",
  error: null,
  chunk_count: 0,
  attempts: 0,
  created_at: "2026-10-28T00:00:00Z",
  updated_at: "2026-10-28T00:00:00Z",
  ingest_estimate: estimate("33333333-3333-3333-3333-333333333331", 120_000),
  confirm_by: IN_7_DAYS,
  ingest_actual: null,
};

const SUMMARY = {
  estimating: 0,
  awaiting: 1,
  duplicates: 0,
  total_micros: 120_000,
  unpriced: false,
  earliest_confirm_by: IN_7_DAYS,
};

test.beforeEach(async ({ page }) => {
  await page.route(
    (url) => url.pathname === "/v1/knowledge/bases",
    (route) => route.fulfill({ json: { bases: [BASE], total: 1, offset: 0, limit: 20 } }),
  );
  await page.route("**/v1/knowledge/bases/support-docs", (route) => route.fulfill({ json: BASE }));
  await page.route(
    (url) => /^\/v1\/knowledge\/bases\/[^/]+\/documents$/.test(url.pathname),
    (route) =>
      route.fulfill({
        json: { documents: [WAITING], total: 1, offset: 0, limit: 50, estimate_summary: SUMMARY },
      }),
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
});

test("a waiting document shows its estimate and is confirmed by its estimate id", async ({
  page,
}) => {
  let confirmed: unknown = null;
  await page.route("**/v1/knowledge/bases/support-docs/documents/confirm", async (route) => {
    confirmed = route.request().postDataJSON();
    await route.fulfill({
      json: { results: [{ document_id: WAITING.id, outcome: "confirmed" }] },
    });
  });

  await page.goto("/knowledge/support-docs");
  await expect(page.getByTestId(`doc-cost-${WAITING.id}`)).toContainText("Estimate ¥0.12");
  await expect(page.getByTestId(`doc-days-left-${WAITING.id}`)).toContainText(
    "confirm within 7 days",
  );
  await expect(page.getByTestId("doc-estimate-banner-text")).toContainText(
    "1 document awaits confirmation, estimated ¥0.12 in total",
  );
  await expectNoA11yViolations(page, "knowledge-estimates");

  await page.getByTestId(`doc-confirm-${WAITING.id}`).click();
  await expect(page.getByText("Confirmed 1 document; ingesting.")).toBeVisible();
  expect(confirmed).toEqual({
    documents: [{ document_id: WAITING.id, estimate_id: WAITING.ingest_estimate.id }],
  });
});

test("an upload's queue row follows its document to awaiting confirmation", async ({ page }) => {
  const uploaded = { ...WAITING, id: "22222222-2222-2222-2222-222222222229", filename: "a.md" };
  await page.route(
    (url) => /^\/v1\/knowledge\/bases\/[^/]+\/documents$/.test(url.pathname),
    async (route) => {
      if (route.request().method() === "POST") {
        return route.fulfill({
          status: 202,
          json: { ...uploaded, status: "estimating", ingest_estimate: null, unchanged: false },
        });
      }
      const asked = new URL(route.request().url()).searchParams.getAll("document_id");
      const documents = asked.length ? [uploaded] : [WAITING];
      return route.fulfill({
        json: { documents, total: documents.length, offset: 0, limit: 50, estimate_summary: SUMMARY },
      });
    },
  );

  await page.goto("/knowledge/support-docs");
  await page
    .getByTestId("doc-upload-dragger")
    .setInputFiles([{ name: "a.md", mimeType: "text/markdown", buffer: Buffer.from("# a") }]);

  await expect(page.getByTestId(`doc-upload-server-${uploaded.id}`)).toHaveAttribute(
    "data-server-status",
    "awaiting_confirmation",
  );
  await expect(page.getByTestId("doc-upload-batch-text")).toContainText(
    "This batch (1 file): 1 awaits confirmation, estimated ¥0.12",
  );
  await expectNoA11yViolations(page, "knowledge-estimate-queue");
});
