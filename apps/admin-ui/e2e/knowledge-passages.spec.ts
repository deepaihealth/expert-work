/**
 * Knowledge maintenance E2E — B-175 P3-PR3.
 *
 * The segment-preview drawer opens on the passages (整块) and points to what a
 * re-ingest left for review; switching an inherited passage on settles it. An
 * edit refused with 409 ``revision_conflict`` shows the server's copy and
 * overwrites against its revision. Rows of the documents table can be
 * switched off in a batch. Saving changed chunking asks right away to
 * re-ingest the documents still cut the old way. axe on the drawer and on the
 * table with a selection. Routes are registered here; most recent wins.
 */
import type { Page, Request } from "@playwright/test";

import { test, expect, expectNoA11yViolations, SAMPLE_JWT } from "./fixtures";

const BASE = {
  id: "11111111-1111-1111-1111-111111111111",
  name: "support-docs",
  description: "Customer FAQ",
  chunk_max_tokens: 400,
  chunk_overlap_tokens: 0,
  parent_max_tokens: 1024,
  chunk_params_hash: "hash-400",
  created_at: "2026-06-12T00:00:00Z",
  updated_at: "2026-06-12T00:00:00Z",
  created_by: "alice@acme.com",
  retrieval_config: { top_k: 5, score_threshold: null, method: "hybrid", rerank_enabled: true },
  embedding_provider: "qwen",
  embedding_model: "text-embedding-v4",
  needs_reindex: false,
  reindexing: false,
  stats: { document_count: 2, chunk_count: 12 },
};

function document(id: string, filename: string, review = { inherited: 0, unapplied: 0 }) {
  return {
    id,
    filename,
    status: "ready",
    error: null,
    chunk_count: 6,
    attempts: 1,
    enabled: true,
    review,
    created_at: "2026-06-12T00:00:00Z",
    updated_at: "2026-06-12T00:05:00Z",
  };
}

const FAQ = document("22222222-2222-2222-2222-222222222221", "faq.pdf", {
  inherited: 1,
  unapplied: 0,
});
const GUIDE = document("22222222-2222-2222-2222-222222222222", "guide.pdf");

const PARENT = {
  id: "33333333-3333-3333-3333-333333333331",
  kind: "parent",
  chunk_index: 0,
  content: "Deductible rules.\n\nThe deductible is 500 dollars.",
  heading_path: ["Benefits"],
  enabled: true,
  disabled_reason: null,
  edited: false,
  revision: 1,
  children: [
    {
      id: "33333333-3333-3333-3333-333333333332",
      chunk_index: 0,
      content: "Deductible rules.",
      heading_path: ["Benefits"],
      enabled: true,
      disabled_reason: null,
    },
    {
      id: "33333333-3333-3333-3333-333333333333",
      chunk_index: 1,
      content: "The deductible is 500 dollars.",
      heading_path: ["Benefits"],
      enabled: true,
      disabled_reason: null,
    },
  ],
};

const INHERITED = {
  id: "33333333-3333-3333-3333-333333333334",
  kind: "chunk",
  chunk_index: 2,
  content: "A paragraph someone switched off before the file changed.",
  heading_path: ["Notes"],
  enabled: false,
  disabled_reason: "inherited",
  edited: false,
  revision: 1,
  children: [
    {
      id: "33333333-3333-3333-3333-333333333334",
      chunk_index: 2,
      content: "A paragraph someone switched off before the file changed.",
      heading_path: ["Notes"],
      enabled: false,
      disabled_reason: "inherited",
    },
  ],
};

const DOC_PATH = `/v1/knowledge/bases/support-docs/documents/${FAQ.id}`;

test.beforeEach(async ({ page }) => {
  await page.route(
    (url) => url.pathname === "/v1/knowledge/bases",
    (route) => route.fulfill({ json: { bases: [BASE], total: 1, offset: 0, limit: 20 } }),
  );
  await page.route(
    (url) => url.pathname === "/v1/knowledge/bases/support-docs",
    (route) => route.fulfill({ json: BASE }),
  );
  await page.route(
    (url) => url.pathname === "/v1/knowledge/bases/support-docs/documents",
    (route) =>
      route.fulfill({ json: { documents: [FAQ, GUIDE], total: 2, offset: 0, limit: 50 } }),
  );
  await page.route(
    (url) => url.pathname === `${DOC_PATH}/passages`,
    (route) =>
      route.fulfill({
        json: { passages: [PARENT, INHERITED], figure_ids: [], total: 2, offset: 0, limit: 20 },
      }),
  );
  await page.route(
    (url) => url.pathname === `${DOC_PATH}/unapplied-edits`,
    (route) => route.fulfill({ json: { unapplied: [], inherited: [INHERITED] } }),
  );
  await page.route(
    (url) => url.pathname === "/v1/knowledge/bases/support-docs/stale-documents",
    (route) => route.fulfill({ json: { stale: 0, chunk_params_hash: "hash-400" } }),
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

async function openDrawer(page: Page): Promise<void> {
  await page.goto("/knowledge/support-docs");
  await page.getByTestId(`doc-chunks-${FAQ.id}`).click();
  await expect(page.getByTestId(`kb-passage-card-${PARENT.id}`)).toBeVisible();
}

test("drawer: passages first, the review points to the inherited switch-off and settles it", async ({
  page,
}) => {
  const patches: unknown[] = [];
  await page.route(
    (url) => url.pathname === `${DOC_PATH}/passages/${INHERITED.id}`,
    (route) => {
      patches.push(route.request().postDataJSON());
      return route.fulfill({
        json: {
          ...INHERITED,
          enabled: true,
          disabled_reason: null,
          children: [{ ...INHERITED.children[0], enabled: true, disabled_reason: null }],
        },
      });
    },
  );

  // The row says something waits for review.
  await page.goto("/knowledge/support-docs");
  await expect(page.getByTestId(`doc-review-${FAQ.id}`)).toHaveText("1 to review");
  await page.getByTestId(`doc-chunks-${FAQ.id}`).click();
  await expect(page.getByTestId(`kb-passage-meta-${PARENT.id}`)).toContainText(
    "Passage #0 · 49 characters · 2 chunks",
  );
  await page.getByTestId(`kb-passage-children-toggle-${PARENT.id}`).click();
  await expect(page.getByTestId(`kb-passage-child-${PARENT.children[1].id}`)).toBeVisible();
  await expect(page.getByTestId("kb-review-alert")).toBeVisible();
  await expectNoA11yViolations(page, "knowledge-passage-drawer");

  await page.getByTestId("kb-review-open").click();
  const panel = page.getByTestId("kb-review-panel");
  await expect(panel).toBeVisible();
  await expect(panel.getByTestId(`kb-passage-inherited-${INHERITED.id}`)).toBeVisible();
  await panel.getByTestId(`kb-passage-toggle-${INHERITED.id}`).click();
  await expect.poll(() => patches).toEqual([{ enabled: true }]);
});

test("edit: a 409 shows the server's copy; overwrite sends its revision", async ({ page }) => {
  const sent: Array<Record<string, unknown>> = [];
  await page.route(
    (url) => url.pathname === `${DOC_PATH}/passages/${PARENT.id}`,
    (route) => {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      sent.push(body);
      if (sent.length === 1) {
        return route.fulfill({
          status: 409,
          json: {
            detail: {
              error: "revision_conflict",
              passage: { ...PARENT, content: "Their text.", revision: 3, edited: true },
            },
          },
        });
      }
      return route.fulfill({ json: { ...PARENT, content: body.content, revision: 4, edited: true } });
    },
  );

  await openDrawer(page);
  await page.getByTestId(`kb-passage-edit-${PARENT.id}`).click();
  await page.getByTestId("kb-passage-editor-content").fill("My text.");
  await page.getByTestId("kb-passage-editor-save").click();

  const conflict = page.getByTestId("kb-passage-editor-conflict");
  await expect(conflict).toContainText("revision 3");
  await expect(conflict).toContainText("Their text.");
  await expect(page.getByTestId("kb-passage-editor-save")).toHaveText("Overwrite with mine");
  await page.getByTestId("kb-passage-editor-save").click();

  await expect.poll(() => sent.length).toBe(2);
  expect(sent[0]).toEqual({ content: "My text.", revision: 1 });
  expect(sent[1]).toEqual({ content: "My text.", revision: 3 });
  await expect(page.getByTestId("kb-passage-editor")).toHaveCount(0);
});

test("documents: switch two off in one batch", async ({ page }) => {
  let posted: Request | null = null;
  await page.route(
    (url) => url.pathname === "/v1/knowledge/bases/support-docs/documents/batch",
    (route) => {
      posted = route.request();
      return route.fulfill({
        json: { action: "disable", done: [FAQ.id, GUIDE.id], skipped: [] },
      });
    },
  );

  await page.goto("/knowledge/support-docs");
  await page.getByRole("checkbox", { name: "Select faq.pdf" }).check();
  await page.getByRole("checkbox", { name: "Select guide.pdf" }).check();
  await expect(page.getByTestId("doc-batch-count")).toHaveText("2 selected");
  await expectNoA11yViolations(page, "knowledge-documents-batch");
  await page.getByTestId("doc-batch-disable").click();

  await expect(page.getByText("Switched off 2 documents.")).toBeVisible();
  expect((posted as Request | null)?.postDataJSON()).toEqual({
    action: "disable",
    document_ids: [FAQ.id, GUIDE.id],
  });
  await expect(page.getByTestId("doc-batch-count")).toHaveCount(0);
});

test("settings: saving changed chunking asks to re-ingest the stale documents", async ({ page }) => {
  // Before the save every document matches the parameters; after it two do not.
  let saved = false;
  let reingested = false;
  await page.route(
    (url) => url.pathname === "/v1/knowledge/bases/support-docs/stale-documents",
    (route) =>
      route.fulfill({
        json: { stale: saved && !reingested ? 2 : 0, chunk_params_hash: "hash-300" },
      }),
  );
  await page.route(
    (url) => url.pathname === "/v1/knowledge/bases/support-docs/reingest-stale",
    (route) => {
      reingested = true;
      return route.fulfill({
        status: 202,
        json: { queued: [FAQ.id, GUIDE.id], skipped: [], remaining: 0 },
      });
    },
  );
  await page.route(
    (url) => url.pathname === "/v1/knowledge/bases/support-docs",
    (route) => {
      if (route.request().method() !== "PATCH") return route.fallback();
      saved = true;
      return route.fulfill({ json: { ...BASE, chunk_max_tokens: 300 } });
    },
  );

  await page.goto("/knowledge/support-docs/settings");
  await expect(page.getByTestId("kb-settings-description-hint")).toBeVisible();
  await expect(page.getByTestId("kb-settings-stale")).toHaveText(
    "Every document is cut by the current chunking parameters.",
  );
  await page.getByRole("spinbutton", { name: "Chunk cap (tokens)" }).fill("300");
  await page.getByTestId("kb-settings-save").click();

  await expect(page.getByRole("dialog")).toContainText(
    "2 documents are still cut by older chunking parameters. Re-ingest them now?",
  );
  await page.getByTestId("kb-stale-confirm-ok").click();
  await expect(page.getByText("Submitted 2 document(s) for re-ingest. Their cost is being estimated — confirm it on the Documents tab to start.")).toBeVisible();
  expect(reingested).toBe(true);
});
