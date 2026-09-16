/**
 * Agent-form MCP picker E2E — Stream V-G, selectors migrated to the
 * group-nav + detail-pane layout (agent-config-page redesign PR1).
 *
 * Proves an admin can:
 *   (a) open the Create-Agent modal, enable the MCP toggle, see the
 *       server checkbox list (from ``GET /v1/mcp-servers/available``),
 *       check ``github``, expand its tool collapse
 *       (``GET /v1/mcp-servers/github/tools``), check ``create_issue``,
 *       submit — and the ``POST /v1/agents`` body contains
 *       ``tools: [{type:"mcp", servers:["github"], allow_tools:["create_issue"]}]``.
 *   (b) the open form with the MCP picker passes the axe a11y audit.
 *
 * Mirrors ``manifest-editor.spec.ts``:
 *   - same login flow (SAMPLE_JWT, login-card, optional login-dev-toggle)
 *   - same schema stub registered after the fixture's agents glob
 *     so it wins (LIFO ordering)
 *   - same embedding-config stub
 *   - ``page.route`` mocks for ``/v1/mcp-servers/available`` and
 *     ``/v1/mcp-servers/github/tools``
 *   - POST body intercepted via ``page.waitForRequest`` (mirrors the PUT
 *     intercept in ``manifest-edit.spec.ts``)
 *
 * The shared ``installControlPlaneStub`` fixture auto-registers
 * ``**​/v1/agents*`` (returns a list with customer-support-bot) so the
 * Agents page renders.  We do NOT assert on that agent name — the test
 * only needs the Create button to appear.
 *
 * For the POST we register a ``**​/v1/agents`` route that intercepts only
 * POST requests and calls ``route.fallback()`` for everything else so the
 * fixture's broader glob still handles the GET list.
 */
import { test, expect, expectNoA11yViolations, SAMPLE_JWT } from "./fixtures";

// ── Stub data ─────────────────────────────────────────────────────────────

const SCHEMA_ENVELOPE = {
  success: true,
  error: null,
  data: {
    type: "object",
    properties: {
      metadata: {
        type: "object",
        properties: {
          name: { type: "string", title: "Name" },
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

const AVAILABLE_SERVERS = {
  success: true,
  data: [
    { name: "github", source: "tenant", enabled: true },
    { name: "fs", source: "platform", enabled: true },
  ],
  error: null,
};

// B-61 —— 这两个桩工具带上 input_schema,子弹窗里的「参数绑定」界面才会渲染。
// 不带的话,新增的 <label htmlFor> + Select id + 展开按钮那一层在 e2e / axe 里
// 从来不会出现,等于裸着上船(复评 L3)。
const GITHUB_TOOLS = {
  success: true,
  data: [
    {
      name: "create_issue",
      description: "Create a new GitHub issue",
      input_schema: {
        type: "object",
        properties: { repo: {}, title: {}, body: {} },
        required: ["repo", "title"],
      },
    },
    {
      name: "list_repos",
      description: "List repositories",
      input_schema: { type: "object", properties: { owner: {} } },
    },
  ],
  error: null,
};

// POST /v1/agents returns a minimal success envelope so the modal closes.
const CREATE_AGENT_OK = {
  success: true,
  data: {
    record: {
      id: "aaaaaaaa-0000-0000-0000-000000000099",
      tenant_id: "22222222-2222-2222-2222-222222222222",
      name: "mcp-agent",
      version: "1.0.0",
      status: "active",
      spec_sha256: "a".repeat(64),
      created_by: "alice@acme.com",
      created_at: "2026-06-03T10:00:00Z",
      updated_at: "2026-06-03T10:00:00Z",
      spec: {},
    },
  },
  error: null,
};

// ── Common route/login setup ───────────────────────────────────────────────

test.beforeEach(async ({ page }) => {
  // Schema stub — registered after the fixture's ``**/v1/agents*`` glob
  // so it takes precedence (LIFO).
  await page.route("**/v1/agents/schema", async (route) => {
    await route.fulfill({ json: SCHEMA_ENVELOPE });
  });

  // Model catalog — needed by the form's ModelSelect field on open.
  await page.route("**/v1/model-catalog", async (route) => {
    await route.fulfill({ json: CATALOG_ENVELOPE });
  });

  // Embedding-status stub — modal renders the editor only when configured.
  await page.route("**/v1/platform/embedding-config/status", (route) =>
    route.fulfill({
      json: { success: true, data: { configured: true }, error: null },
    }),
  );

  // MCP available-servers list.
  await page.route("**/v1/mcp-servers/available", async (route) => {
    await route.fulfill({ json: AVAILABLE_SERVERS });
  });

  // github tools (also catches any other server name via the wildcard).
  await page.route("**/v1/mcp-servers/github/tools", async (route) => {
    await route.fulfill({ json: GITHUB_TOOLS });
  });

  // POST /v1/agents — stub so the modal can close after submit.
  // Uses route.fallback() for non-POST requests so the fixture's broader
  // ``**/v1/agents*`` GET stub still handles the agents-list fetch.
  await page.route("**/v1/agents", async (route) => {
    if (route.request().method() === "POST") {
      await route.fulfill({ json: CREATE_AGENT_OK });
      return;
    }
    await route.fallback();
  });

  // Login.
  await page.goto("/login");
  await expect(page.getByTestId("login-card")).toBeVisible();
  const tokenField = page.getByTestId("login-token");
  if (!(await tokenField.isVisible())) {
    await page.getByTestId("login-dev-toggle").click();
  }
  await tokenField.fill(SAMPLE_JWT);
  await page.getByTestId("login-submit").click();
  await expect(page).toHaveURL(/\/agents$/);
  // Wait for the agents page to settle (list rendered) — assert on the
  // create button, not a specific agent name, so no pre-existing agent is
  // required.
  await expect(page.getByTestId("agents-create")).toBeVisible();
});

// ── Tests ─────────────────────────────────────────────────────────────────

test("(a) create-agent: enable MCP, pick server+tool, submit — POST body contains mcp entry", async ({
  page,
}) => {
  // Open the Create Agent modal.
  await page.getByTestId("agents-create").click();
  await expect(page.getByTestId("create-agent-modal")).toBeVisible();
  await expect(page.getByTestId("manifest-form-view")).toBeVisible();

  // Give the agent a name so the manifest is valid enough for the backend stub.
  const nameInput = page.getByTestId("af-name").locator("input");
  await nameInput.clear();
  await nameInput.fill("mcp-agent");

  // MCP lives behind the capabilities group's [MCP] sub-tab (config-page
  // redesign v2, Task 5) — open the group, then the tab; the server list
  // shows there and selecting a server enables MCP.
  await page.getByTestId("cfg-nav-capabilities").click();
  await page.getByRole("tab", { name: "MCP" }).click();

  // The McpToolPicker mounts and fetches /v1/mcp-servers/available.
  await expect(page.getByTestId("af-mcp-server-github")).toBeVisible();
  await expect(page.getByTestId("af-mcp-server-fs")).toBeVisible();

  // Check the github server (= enable MCP with github selected).
  await page.getByTestId("af-mcp-server-github").click();

  // Click the gear to open the tool-selection sub-modal.
  await page.getByTestId("af-mcp-choose-github").click();

  // In the sub-modal: wait for + check the create_issue tool, then close it.
  await expect(page.getByTestId("af-mcp-tool-create_issue")).toBeVisible();
  await page.getByTestId("af-mcp-tool-create_issue").click();
  await page
    .getByTestId("af-mcp-tool-modal")
    .getByRole("button", { name: /完成|Done/ })
    .click();

  // Intercept the POST and grab its body.
  const postPromise = page.waitForRequest(
    (req) => req.method() === "POST" && req.url().includes("/v1/agents"),
  );

  // Submit.
  await page.getByTestId("create-agent-submit").click();

  const postReq = await postPromise;
  const body = postReq.postDataJSON() as { manifest_yaml: string };

  // The manifest is submitted as YAML.  Parse the tools list from it.
  // We assert structurally: the YAML must contain the mcp entry with
  // servers: [github] and allow_tools: [create_issue].
  const yaml = body.manifest_yaml;
  expect(yaml).toContain("type: mcp");
  expect(yaml).toContain("github");
  expect(yaml).toContain("create_issue");
});

test("(b) create modal with MCP picker passes axe (serious + critical)", async ({
  page,
}) => {
  await page.getByTestId("agents-create").click();
  await expect(page.getByTestId("manifest-form-view")).toBeVisible();

  // MCP lives behind the capabilities group's [MCP] sub-tab (config-page
  // redesign v2, Task 5).
  await page.getByTestId("cfg-nav-capabilities").click();
  await page.getByRole("tab", { name: "MCP" }).click();

  // Wait for the picker to load so axe sees the full DOM.
  await expect(page.getByTestId("af-mcp-server-github")).toBeVisible();

  await expectNoA11yViolations(page, "create-agent-modal-mcp");
});

// B-61 —— 绑定编辑器(参数名 <label htmlFor> + antd Select 的 id + 展开按钮的
// aria-expanded/aria-controls)是这次新增的一层 markup,而唯一会打开工具子弹窗的
// 用例 (a) 之前用的桩工具没有 input_schema,所以这层在 Playwright / axe 里从没被
// 渲染过。本仓库有过「vitest 绿而 Playwright/axe 红」的先例,所以让 axe 真扫一遍。
test("(c) MCP per-parameter binding editor passes axe (serious + critical)", async ({
  page,
}) => {
  await page.getByTestId("agents-create").click();
  await expect(page.getByTestId("manifest-form-view")).toBeVisible();

  // 先声明一个提示词变量 —— 没有声明变量时绑定区只给一句提示,下拉根本不渲染。
  await page.getByTestId("cfg-nav-prompt").click();
  await page.getByTestId("af-prompt-jinja").click();
  await page.getByTestId("af-prompt-var-add").click();
  await page.getByTestId("af-prompt-var-name-0").fill("project_code");

  // 再到 MCP:勾服务器 → 开工具子弹窗 → 展开 create_issue 的参数绑定。
  await page.getByTestId("cfg-nav-capabilities").click();
  await page.getByRole("tab", { name: "MCP" }).click();
  await page.getByTestId("af-mcp-server-github").click();
  await page.getByTestId("af-mcp-choose-github").click();
  await expect(page.getByTestId("af-mcp-tool-create_issue")).toBeVisible();
  await page.getByTestId("af-mcp-bind-toggle-create_issue").click();

  // 逐参数一行都在(必填的 repo/title + 选填的 body)。
  await expect(page.getByTestId("af-mcp-bind-row-create_issue-repo")).toBeVisible();
  await expect(page.getByTestId("af-mcp-bind-row-create_issue-body")).toBeVisible();
  // aria-controls 指得着真实的展开区。
  const panelId = await page
    .getByTestId("af-mcp-bind-toggle-create_issue")
    .getAttribute("aria-controls");
  expect(panelId).toBeTruthy();
  await expect(page.locator(`#${panelId}`)).toBeVisible();

  await expectNoA11yViolations(page, "create-agent-modal-mcp-bindings");
});

// B-61 修复轮 2 / N1 —— 复评是在真浏览器里逮到这条的:声明变量 → 绑参数 →
// 点一下 Jinja 开关,提交的 manifest_yaml 就带着 arg_bindings 却没有 variables,
// 喂给真的协议层校验器直接 REJECTED。jsdom 那几条钉的是写入器,这一条钉的是
// **真的提交出去的那份 YAML**,也就是复评当时的原始复现路径。
test("(d) turning Jinja off never submits bindings without variables", async ({
  page,
}) => {
  await page.getByTestId("agents-create").click();
  await expect(page.getByTestId("manifest-form-view")).toBeVisible();

  const nameInput = page.getByTestId("af-name").locator("input");
  await nameInput.clear();
  await nameInput.fill("jinja-off-agent");

  // 声明变量。
  await page.getByTestId("cfg-nav-prompt").click();
  await page.getByTestId("af-prompt-jinja").click();
  await page.getByTestId("af-prompt-var-add").click();
  await page.getByTestId("af-prompt-var-name-0").fill("project_code");

  // 把 create_issue.repo 绑到它。
  await page.getByTestId("cfg-nav-capabilities").click();
  await page.getByRole("tab", { name: "MCP" }).click();
  await page.getByTestId("af-mcp-server-github").click();
  await page.getByTestId("af-mcp-choose-github").click();
  // 勾上工具本身,这样下面还能验「allow_tools 也没被牵连」。
  await expect(page.getByTestId("af-mcp-tool-create_issue")).toBeVisible();
  await page.getByTestId("af-mcp-tool-create_issue").click();
  await page.getByTestId("af-mcp-bind-toggle-create_issue").click();
  // 真浏览器里带 id 的是 antd Select 内部那个 readonly input,点不动 ——
  // 点外面的 .ant-select 容器(jsdom 里事件会冒泡,真浏览器里不会)。
  await page
    .getByTestId("af-mcp-bind-row-create_issue-repo")
    .locator(".ant-select")
    .click();
  // 选项在 DOM 里出现两遍(可点的 item + 隐藏的 ARIA 镜像)—— 认可点的那个,
  // 与仓库里 vitest 侧同一个判据(.ant-select-item-option-content)。
  await page
    .locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden)")
    .locator(".ant-select-item-option-content", { hasText: /^project_code$/ })
    .click();
  await page
    .getByTestId("af-mcp-tool-modal")
    .getByRole("button", { name: /完成|Done/ })
    .click();

  // 回提示词页关掉 Jinja —— 复评当时这一下零提示就产出了孤儿。
  await page.getByTestId("cfg-nav-prompt").click();
  await page.getByTestId("af-prompt-jinja").click();
  // 现在必须先问,而且说清会删掉几条。
  const confirm = page.locator(".ant-modal-confirm");
  await expect(confirm).toBeVisible();
  await expect(confirm.getByRole("listitem")).toHaveCount(1);
  await confirm.getByRole("button", { name: /关掉并删除|Turn off and delete/ }).click();
  await expect(confirm).toBeHidden();

  const postPromise = page.waitForRequest(
    (req) => req.method() === "POST" && req.url().includes("/v1/agents"),
  );
  await page.getByTestId("create-agent-submit").click();
  const yaml = (
    (await postPromise).postDataJSON() as { manifest_yaml: string }
  ).manifest_yaml;

  // 这就是复评喂给 AgentSpec.model_validate 的那份东西:不能再出现
  // 「有 arg_bindings、没有 variables」。
  expect(yaml).not.toContain("arg_bindings");
  expect(yaml).not.toContain("project_code");
  // MCP 那一侧的选择本身不受牵连。
  expect(yaml).toContain("type: mcp");
  expect(yaml).toContain("create_issue");
});

// B-61 修复轮 3 / NEW-1 —— 被绑变量那一行的**几何**。
//
// 上一轮为了讲清楚「为什么锁着 / 怎么解锁」,把一句 ~140 字符的常驻说明塞进了
// 和五个控件同一个 flex 行、又去掉了 nowrap:变量名被挤成 "project_c"(这一行
// 最该读出来的东西反而看不见了),Trusted/Required 塌成一列一个字母的竖排。
// 纯 CSS 回归 vitest 一点都看不见 —— jsdom 不做布局,盒子全是 0×0。所以这条
// 在真浏览器里**量尺寸**:名字输入框不许被压缩、说明必须换到控件下面自己一行、
// 两个标签不许竖排。肉眼看一眼不算数。
test("(e) the bound-variable row stays readable — the note gets its own line", async ({
  page,
}) => {
  await page.getByTestId("agents-create").click();
  await expect(page.getByTestId("manifest-form-view")).toBeVisible();

  await page.getByTestId("cfg-nav-prompt").click();
  await page.getByTestId("af-prompt-jinja").click();
  await page.getByTestId("af-prompt-var-add").click();
  await page.getByTestId("af-prompt-var-name-0").fill("project_code");

  await page.getByTestId("cfg-nav-capabilities").click();
  await page.getByRole("tab", { name: "MCP" }).click();
  await page.getByTestId("af-mcp-server-github").click();
  await page.getByTestId("af-mcp-choose-github").click();
  await expect(page.getByTestId("af-mcp-tool-create_issue")).toBeVisible();
  await page.getByTestId("af-mcp-tool-create_issue").click();
  await page.getByTestId("af-mcp-bind-toggle-create_issue").click();
  await page
    .getByTestId("af-mcp-bind-row-create_issue-repo")
    .locator(".ant-select")
    .click();
  await page
    .locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden)")
    .locator(".ant-select-item-option-content", { hasText: /^project_code$/ })
    .click();
  await page
    .getByTestId("af-mcp-tool-modal")
    .getByRole("button", { name: /完成|Done/ })
    .click();

  // 回到提示词页:这一行现在是「被绑」状态,常驻说明出现。
  await page.getByTestId("cfg-nav-prompt").click();
  const nameInput = page.getByTestId("af-prompt-var-name-0");
  const note = page.getByTestId("af-prompt-var-bound-0");
  await expect(note).toBeVisible();

  const nameBox = (await nameInput.boundingBox())!;
  const noteBox = (await note.boundingBox())!;
  // 参照物是**行容器**,不是名字输入框 —— 输入框自己就是会被挤扁的那一个,
  // 拿它当基准的话断言是循环的:挤坏之后 "比名字栏宽两倍" 这种条件反而更容易满足。
  // 实测(healthy / 完全还原成缺陷态):
  //   row   438,628 / 438,628      ← 不动,可以当尺子
  //   name  w=160   / w=83.7
  //   note  x=438 w=628 / x=736.8 w=329.2
  const rowBox = (await page.getByTestId("af-prompt-var-row-0").boundingBox())!;
  const trustedLabel = page
    .getByTestId("af-prompt-var-row-0")
    .locator("span", { hasText: /^(可信|Trusted)$/ })
    .first();

  // 这一组几何断言用 expect.soft:布局坏掉时往往同时踩中好几条,硬断言会停在
  // 第一条,剩下的到底是"也坏了"还是"其实拦不住"就看不出来了 —— 而"看起来像
  // 守卫、其实永远不会失败的断言"正是本轮要清掉的东西。soft 让每次跑都把所有
  // 违反项一次报全。
  // 1) 变量名那一栏没有被压缩(声明宽度 160)。挤坏的那一版量到的是 83.7。
  expect.soft(nameBox.width).toBeGreaterThanOrEqual(150);

  // 2) 说明从**行的左边缘**起头 —— 也就是它真的另起了一行,而不是排在五个控件
  //    后面。缺陷态量到 x=736.8(排在删除按钮右边),healthy 是 438 = 行左边缘。
  expect.soft(noteBox.x).toBeLessThanOrEqual(rowBox.x + 1);

  // 3) 说明横跨整行。缺陷态量到 329.2 / 628(被挤成一个窄高列),healthy 是 628。
  expect.soft(noteBox.width).toBeGreaterThanOrEqual(rowBox.width - 1);

  // 4) 标签没有被压成一列一个字母的竖排 —— 竖排时高度会是行高的好几倍。
  const trustedBox = (await trustedLabel.boundingBox())!;
  expect.soft(trustedBox.height).toBeLessThan(40);

  // 5) 那句说明必须真的把「怎么解锁」讲出来,不只是一个数字。
  const noteText = (await note.textContent()) ?? "";
  expect.soft(noteText).toContain("MCP");

  // 6) 「独占一行」不许靠「文案碰巧够长」。把它临时改成一个字符再量一次。
  //    这一条是**唯一**杀得死 flexBasis:100% 的断言:只去掉 flexBasis(留着
  //    flexWrap)时,长文案照样会换行,上面 1~5 量到的几何与 healthy 逐个数字
  //    相同 —— 实测过。短文案下它才会滑回行内,于是下一次文案一改短,这一行
  //    又开始和五个控件抢地方。
  // e2e 的 tsconfig 不带 dom lib,所以这里显式窄化到「有 textContent 的东西」。
  await note.evaluate((el) => {
    (el as unknown as { textContent: string }).textContent = "x";
  });
  const shortNoteBox = (await note.boundingBox())!;
  const nameBoxAfter = (await nameInput.boundingBox())!;
  expect.soft(shortNoteBox.y).toBeGreaterThanOrEqual(
    nameBoxAfter.y + nameBoxAfter.height - 1,
  );
});
