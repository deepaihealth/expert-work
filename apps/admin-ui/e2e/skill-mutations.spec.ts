/**
 * Skill detail dual-pane + mutation flows — Capability Uplift Sprint #3
 * PR C, Mini-ADR U-20.
 *
 * Browser-truth check of the dual-pane editor behaviour the unit tests
 * can't reliably cover (antd portals + Monaco editor + status select
 * dropdown all flake under jsdom). Each test installs its own per-route
 * skill fixture on top of the global empty-list stub from ``fixtures.ts``.
 *
 * Covers (Mini-ADRs U-15 / U-20 / U-24):
 *
 *   - File tree renders SKILL.md + grouped supporting files
 *   - High-risk badge appears on the hero
 *   - Non-admin caller sees the Active status option as disabled
 *   - Edit + Save round-trips through PUT supporting-files
 *   - Delete confirmation requires typing the file path
 */
import type { Page } from "@playwright/test";
import { test, expect, SAMPLE_JWT } from "./fixtures";

const SKILL_ID = "sk-1";

/** Folders default to collapsed (GitHub-style) — expand every level so a
 * supporting file nested under a folder is clickable. */
async function expandFileTree(page: Page): Promise<void> {
  const tree = page.getByTestId("skill-file-tree");
  // Wait for the tree to render before querying switchers.
  await tree.getByText("SKILL.md").waitFor();
  // Re-query + click the FIRST collapsed switcher each pass: clicking one
  // re-renders the tree, so a snapshotted ``.all()`` handle would go stale
  // (clicking ``.nth(1)`` then hangs). Loop until none remain.
  const closed = tree.locator(".ant-tree-switcher_close");
  for (let i = 0; i < 30; i += 1) {
    if ((await closed.count()) === 0) break;
    await closed.first().click();
  }
}

const SKILL_ROW = {
  id: SKILL_ID,
  name: "api_debug",
  status: "draft" as const,
  latest_version: 1,
  description: "Inspect HTTP / gRPC requests + run diagnostics.",
  category: "ops",
  created_at: "2026-05-20T10:00:00Z",
  updated_at: "2026-05-26T10:00:00Z",
};

const HIGH_RISK_VERSION = {
  id: "v1",
  skill_id: SKILL_ID,
  version: 1,
  prompt_fragment: "You debug APIs by running scripts/diagnose.py.",
  tool_names: ["exec_python", "http"],
  description: "First cut.",
  category: "ops",
  required_models: [],
  authored_by: "human",
  supporting_files: {
    "reference/error_codes.md": { size: 28, mime: "text/markdown" },
    "scripts/diagnose.py": { size: 120, mime: "text/x-python" },
  },
  lazy_load: true,
  high_risk: true,
  created_at: "2026-05-20T10:00:00Z",
};

const VERSION_TWO_AFTER_PUT = {
  ...HIGH_RISK_VERSION,
  id: "v2",
  version: 2,
  supporting_files: {
    ...HIGH_RISK_VERSION.supporting_files,
    "reference/error_codes.md": { size: 12, mime: "text/markdown" },
  },
};

async function login(page: import("@playwright/test").Page): Promise<void> {
  await page.goto("/login");
  await page.getByTestId("login-token").fill(SAMPLE_JWT);
  await page.getByTestId("login-submit").click();
  await expect(page).toHaveURL(/\/agents$/);
}

async function installSkillRoutes(
  page: import("@playwright/test").Page,
  options: {
    version?: typeof HIGH_RISK_VERSION;
    me?: { roles: string[]; is_system_admin?: boolean };
    onPut?: (path: string) => void;
    onDelete?: (path: string) => void;
  } = {},
): Promise<void> {
  const version = options.version ?? HIGH_RISK_VERSION;
  // Per-tenant role override — re-route /v1/me so the AuthContext sees
  // the role we want. The default global stub uses ["admin"].
  if (options.me) {
    await page.route("**/v1/me", async (route) => {
      await route.fulfill({
        json: {
          success: true,
          data: {
            subject_id: "11111111-1111-1111-1111-111111111111",
            subject_type: "user",
            tenant_id: "22222222-2222-2222-2222-222222222222",
            auth_method: "jwt",
            roles: options.me?.roles ?? [],
            scopes: [],
            is_system_admin: options.me?.is_system_admin ?? false,
            allowed_tenants: ["22222222-2222-2222-2222-222222222222"],
          },
          error: null,
        },
      });
    });
  }

  // Specific skill fetch — match before the global ``**/v1/skills*``
  // stub which the global fixture installs.
  await page.route(`**/v1/skills/${SKILL_ID}`, async (route) => {
    await route.fulfill({ json: SKILL_ROW });
  });
  await page.route(`**/v1/skills/${SKILL_ID}/versions`, async (route) => {
    await route.fulfill({ json: { items: [version] } });
  });

  // Single-file content fetch
  await page.route(
    `**/v1/skills/${SKILL_ID}/versions/*/supporting-files/**`,
    async (route) => {
      const method = route.request().method();
      const url = new URL(route.request().url());
      const filePath = url.pathname.split("/supporting-files/")[1] ?? "";

      if (method === "PUT") {
        options.onPut?.(filePath);
        await route.fulfill({ json: VERSION_TWO_AFTER_PUT });
        return;
      }
      if (method === "DELETE") {
        options.onDelete?.(filePath);
        await route.fulfill({
          json: { ...VERSION_TWO_AFTER_PUT, supporting_files: {} },
        });
        return;
      }
      // GET — return whichever file matches.
      const entry =
        (version.supporting_files as Record<string, { size: number; mime: string }>)[
          decodeURIComponent(filePath)
        ];
      if (!entry) {
        await route.fulfill({
          status: 404,
          json: { detail: "supporting file not found" },
        });
        return;
      }
      // Stable base64 of "hello world\n" — predictable for assertions.
      await route.fulfill({
        json: { content: "aGVsbG8gd29ybGQK", size: 12, mime: entry.mime },
      });
    },
  );
}

test("skill detail renders dual pane + high-risk + lazy badges", async ({ page }) => {
  await installSkillRoutes(page);
  await login(page);
  await page.goto(`/skills/${SKILL_ID}`);
  await expect(page.getByTestId("skill-detail-root")).toBeVisible();
  await expect(page.getByTestId("skill-hero-high-risk-badge")).toBeVisible();
  await expect(page.getByTestId("skill-high-risk-warning")).toBeVisible();
  await expect(page.getByTestId("skill-lazy-badge")).toBeVisible();
  await expect(page.getByTestId("skill-dual-pane")).toBeVisible();
  // File tree contents
  const tree = page.getByTestId("skill-file-tree");
  await expect(tree.getByText("SKILL.md")).toBeVisible();
  await expect(tree.getByText("scripts/")).toBeVisible();
  await expect(tree.getByText("reference/")).toBeVisible();
});

test("non-admin caller has Active status option disabled (U-24 gate)", async ({ page }) => {
  await installSkillRoutes(page, { me: { roles: ["viewer"] } });
  // Mint a fresh non-admin JWT so the OPTIMISTIC identity also lacks
  // admin — the gate computes off useAuth().identity which initially
  // hydrates from JWT before /v1/me lands.
  const viewerHeader = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const viewerPayload = btoa(
    JSON.stringify({
      sub: "11111111-1111-1111-1111-111111111111",
      sub_type: "user",
      tenant_id: "22222222-2222-2222-2222-222222222222",
      roles: ["viewer"],
      iat: Math.floor(Date.now() / 1000),
      exp: Math.floor(Date.now() / 1000) + 3600,
    }),
  );
  const viewerJwt = `${viewerHeader}.${viewerPayload}.`;
  await page.goto("/login");
  await page.getByTestId("login-token").fill(viewerJwt);
  await page.getByTestId("login-submit").click();
  await expect(page).toHaveURL(/\/agents$/);
  await page.goto(`/skills/${SKILL_ID}`);
  await expect(page.getByTestId("skill-hero-high-risk-badge")).toBeVisible();
  // Open the status select dropdown
  await page.getByTestId("skill-status-select").click();
  // The blocked "active" option is the only one rendered with a 🔒 (locale-
  // independent), so match on that rather than the now-localized label text.
  const activeOption = page.locator(".ant-select-item-option").filter({ hasText: "🔒" });
  await expect(activeOption.first()).toBeVisible();
  await expect(activeOption.first()).toHaveClass(/ant-select-item-option-disabled/);
});

test("Edit mode toggles + Save button appears (PUT round-trip covered by vitest)", async ({
  page,
}) => {
  // Mounting + typing into Monaco's nested DOM through Playwright is
  // brittle (custom textarea overlay + viewport-relative coordinates).
  // The vitest test ``SkillsList.test.tsx >> 'selecting a supporting
  // file fetches + renders content; Edit + Save calls PUT'`` already
  // exercises the PUT round-trip through the React component tree with
  // a Monaco textarea mock — we don't need to re-prove that in
  // browser. What the e2e adds here is: the mode toggle (read-only →
  // editable) actually flips in a real Chromium render.
  await installSkillRoutes(page);
  await login(page);
  await page.goto(`/skills/${SKILL_ID}`);

  await expandFileTree(page);
  await page.getByTestId("skill-file-tree").getByText("error_codes.md").click();
  await expect(page.getByTestId("skill-editor-pane")).toBeVisible();
  await expect(page.getByTestId("skill-editor-monaco")).toBeVisible();
  // View mode → Edit button present, Save button absent
  await expect(page.getByTestId("skill-editor-edit-btn")).toBeVisible();
  await expect(page.getByTestId("skill-editor-save-btn")).toHaveCount(0);
  // Click Edit → Save + Cancel appear; Save starts disabled (no dirty)
  await page.getByTestId("skill-editor-edit-btn").click();
  const save = page.getByTestId("skill-editor-save-btn");
  await expect(save).toBeVisible();
  await expect(save).toBeDisabled();
  await expect(page.getByTestId("skill-editor-cancel-btn")).toBeVisible();
  // Cancel returns to view mode
  await page.getByTestId("skill-editor-cancel-btn").click();
  await expect(page.getByTestId("skill-editor-edit-btn")).toBeVisible();
  await expect(page.getByTestId("skill-editor-save-btn")).toHaveCount(0);
});

test("Delete supporting file requires typing the path to confirm", async ({ page }) => {
  let deletePath: string | null = null;
  await installSkillRoutes(page, {
    onDelete: (path) => {
      deletePath = path;
    },
  });
  await login(page);
  await page.goto(`/skills/${SKILL_ID}`);
  await expandFileTree(page);
  await page.getByTestId("skill-file-tree").getByText("error_codes.md").click();
  await expect(page.getByTestId("skill-editor-delete-btn")).toBeVisible();
  await page.getByTestId("skill-editor-delete-btn").click();

  const submit = page.getByTestId("skill-delete-submit");
  await expect(submit).toBeDisabled();
  await page
    .getByTestId("skill-delete-confirm-input")
    .fill("reference/error_codes.md");
  await expect(submit).toBeEnabled();
  await submit.click();
  await expect.poll(() => deletePath).toBe("reference/error_codes.md");
});

test("language-service workers download only when a file of that language is opened", async ({ page }) => {
  // B-152 —— JS/TS/HTML/CSS 的语言服务各是一个独立的 worker 文件(TS 约 7 MB)。
  // 打开别的文件不该下载它们;打开 .js 时才下载 TS 那一个。
  const workerRequests: string[] = [];
  page.on("request", (req) => {
    // 只算 worker 本体:开发服务器是 ``*.worker.js?worker_file``,正式构建是
    // ``assets/*.worker-<hash>.js``;``*.worker.js?worker`` 只是几行的包装模块。
    const url = req.url();
    if (/(ts|css|html)\.worker(\.js\?worker_file|-[\w-]+\.js)/.test(url)) workerRequests.push(url);
  });
  await installSkillRoutes(page, {
    version: {
      ...HIGH_RISK_VERSION,
      supporting_files: {
        ...HIGH_RISK_VERSION.supporting_files,
        "scripts/build.js": { size: 12, mime: "text/javascript" },
      } as typeof HIGH_RISK_VERSION.supporting_files,
    },
  });
  await login(page);
  await page.goto(`/skills/${SKILL_ID}`);
  await expandFileTree(page);

  await page.getByTestId("skill-file-tree").getByText("error_codes.md").click();
  await expect(page.getByTestId("skill-editor-monaco").locator(".monaco-editor")).toBeVisible({
    timeout: 30_000,
  });
  expect(workerRequests).toEqual([]);

  await page.getByTestId("skill-file-tree").getByText("build.js").click();
  await expect.poll(() => workerRequests.some((u) => u.includes("ts.worker"))).toBe(true);
  expect(workerRequests.some((u) => u.includes("css.worker") || u.includes("html.worker"))).toBe(
    false,
  );
});
