/**
 * Knowledge E2E — KB commercial uplift.
 *
 * Smoke + axe: the bases list renders with stats; a row opens the detail page;
 * the documents tab shows localized status; the retrieval-test tab runs a query
 * and renders scored results. Knowledge routes are registered here (no fixture
 * default); most-recently-added handlers win.
 */
import { test, expect, expectNoA11yViolations, SAMPLE_JWT } from "./fixtures";

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

const DOCUMENTS_RESPONSE = {
  documents: [
    {
      id: "22222222-2222-2222-2222-222222222221",
      filename: "faq.pdf",
      status: "ready",
      error: null,
      chunk_count: 12,
      attempts: 1,
      created_at: "2026-06-12T00:00:00Z",
      updated_at: "2026-06-12T00:05:00Z",
    },
  ],
};

const TEST_RESPONSE = {
  query: "deductible",
  count: 1,
  results: [
    {
      content: "The deductible is 500 dollars.",
      source: "faq.pdf#0",
      filename: "faq.pdf",
      chunk_index: 0,
      score: 0.92,
      recall_source: "both",
    },
  ],
};

test.beforeEach(async ({ page }) => {
  await page.route("**/v1/knowledge/bases", (route) => route.fulfill({ json: { bases: [BASE] } }));
  await page.route("**/v1/knowledge/bases/support-docs", (route) => route.fulfill({ json: BASE }));
  await page.route("**/v1/knowledge/bases/*/documents", (route) =>
    route.fulfill({ json: DOCUMENTS_RESPONSE }),
  );
  await page.route("**/v1/knowledge/bases/*/test", (route) =>
    route.fulfill({ json: TEST_RESPONSE }),
  );

  await page.goto("/login");
  await expect(page.getByTestId("login-card")).toBeVisible();
  // The paste-token form sits behind the "Developer login" disclosure when
  // OIDC is configured; CI opens it by default, locally it may be collapsed.
  const tokenField = page.getByTestId("login-token");
  if (!(await tokenField.isVisible())) {
    await page.getByTestId("login-dev-toggle").click();
  }
  await tokenField.fill(SAMPLE_JWT);
  await page.getByTestId("login-submit").click();
  await expect(page).toHaveURL(/\/agents$/);
});

test("list → detail → documents tab shows localized status", async ({ page }) => {
  await page.goto("/knowledge");
  await expect(page.getByTestId("kb-table")).toBeVisible();
  await expect(page.getByText("support-docs")).toBeVisible();

  await page.getByText("support-docs").click();
  await expect(page).toHaveURL(/\/knowledge\/support-docs$/);
  await expect(page.getByTestId("knowledge-detail-root")).toBeVisible();
  await expect(page.getByText("faq.pdf")).toBeVisible();
  await expect(page.getByText("Ready")).toBeVisible();
});

test("retrieval test runs a query and renders scored results", async ({ page }) => {
  await page.goto("/knowledge/support-docs/test");
  await expect(page.getByTestId("knowledge-test-tab")).toBeVisible();
  await page.getByTestId("kb-test-query").fill("deductible");
  await page.getByTestId("kb-test-run").click();
  await expect(page.getByTestId("kb-test-results")).toBeVisible();
  await expect(page.getByText("faq.pdf#0")).toBeVisible();
  await expect(page.getByText("The deductible is 500 dollars.")).toBeVisible();
});

// B-175 L3 — batch upload queue: one row per file, a server 413 lands as a
// failed row carrying the server's limit, an unsupported pick is skipped up
// front and reported on the summary line.
test("batch upload: queue rows, 413 → failed row, unsupported → skipped summary", async ({
  page,
}) => {
  const UPLOADED = {
    id: "22222222-2222-2222-2222-222222222229",
    filename: "a.txt",
    status: "pending",
    error: null,
    chunk_count: 0,
    attempts: 0,
    created_at: "2026-10-13T00:00:00Z",
    updated_at: null,
  };
  // Most-recently-added handler wins; GETs fall through to the list stub.
  await page.route("**/v1/knowledge/bases/*/documents", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    const body = route.request().postDataBuffer()?.toString("latin1") ?? "";
    if (body.includes('filename="too-big.txt"')) {
      return route.fulfill({
        status: 413,
        json: { detail: { error: "file_too_large", limit_bytes: 1024 * 1024 } },
      });
    }
    return route.fulfill({ status: 202, json: UPLOADED });
  });

  await page.goto("/knowledge/support-docs");
  await expect(page.getByTestId("knowledge-documents-tab")).toBeVisible();

  // The dragger's testid sits on rc-upload's hidden <input type=file>.
  await page.getByTestId("doc-upload-dragger").setInputFiles([
    { name: "a.txt", mimeType: "text/plain", buffer: Buffer.from("alpha") },
    { name: "b.md", mimeType: "text/markdown", buffer: Buffer.from("# beta") },
    { name: "too-big.txt", mimeType: "text/plain", buffer: Buffer.from("gamma") },
    { name: "setup.exe", mimeType: "application/octet-stream", buffer: Buffer.from([0x4d, 0x5a]) },
  ]);

  const queue = page.getByTestId("doc-upload-queue");
  await expect(queue).toBeVisible();
  const rows = queue.locator('[data-testid^="doc-upload-queue-row-"]');
  await expect(rows).toHaveCount(3); // skipped rows stay behind the summary toggle
  await expect(queue.locator('[data-status="done"]')).toHaveCount(2);
  await expect(queue.locator('[data-status="failed"]')).toHaveCount(1);
  const failed = queue.locator('[data-status="failed"]');
  await expect(failed).toContainText("too-big.txt");
  await expect(failed).toContainText("1.0 MB"); // the server's limit, not ours
  await expect(failed.getByRole("button", { name: "Retry" })).toBeVisible();
  await expect(queue).toContainText("2 / 3 uploaded");

  const summary = page.getByTestId("doc-upload-summary");
  await expect(summary).toContainText("Skipped 1 unsupported file");
  await summary.getByRole("button", { name: "Show list" }).click();
  await expect(rows).toHaveCount(4);
  await expect(queue.locator('[data-status="skipped"]')).toContainText("setup.exe");

  // the table paginates client-side and shows the total
  await expect(page.getByTestId("doc-table-pagination")).toContainText("1 documents");

  // the queue panel (rows, progress bars, per-file buttons) must pass axe too
  await expectNoA11yViolations(page, "knowledge-batch-upload");
});

test("knowledge detail passes axe (serious + critical)", async ({ page }) => {
  await page.goto("/knowledge/support-docs");
  await expect(page.getByTestId("knowledge-detail-root")).toBeVisible();
  await expectNoA11yViolations(page, "knowledge-detail");
});
