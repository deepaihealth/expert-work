/**
 * B-61 Task 8 —— 逐参数绑定的配置 UI。
 *
 * 绑定住在「选择工具」弹窗里:每个在范围内的工具行下面一个「参数绑定」展开区,
 * 按工具自己的 ``input_schema.properties`` 逐参数一行,右边是「自动（模型填）+
 * 本 agent 声明过的变量」。选中即写进 ``arg_bindings``,选回「自动」就删掉该参数,
 * 该工具的 ``args`` 空了就整条删除。
 *
 * 另外钉住两件事:
 *  - 下拉只列**声明过**的变量(自由文本变量名 = 悄悄谁也匹配不上);
 *  - 任何会把已有绑定删掉的动作(取消勾选服务器 = 关掉 MCP 首当其冲)先弹确认,
 *    并把要删的那几条逐条列出来 —— 绑定在今天之前是看不见的,看不见的东西被
 *    静默删掉才是问题所在。
 */
import { useState } from "react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import i18n from "../../../../i18n";

import { McpToolPicker } from "../McpToolPicker";
import { FormView } from "../../FormView";
import { readTools, type AgentManifest, type ArgBindingFields } from "../../form_model";
import * as serversSdk from "../../../../api/mcp-servers";
import * as catalogSdk from "../../../../api/mcp-catalog";
import * as modelCatalog from "../../catalog";

// Cross-tenant W3 — the picker reads the ambient tenant scope; these tests
// don't mount a TenantScopeProvider, so mock it (home state: no scope).
const scopeRef = vi.hoisted(() => ({ current: undefined as string | undefined }));
vi.mock("../../../../tenant/TenantScopeContext", async (importOriginal) => {
  const { mockTenantScopeModule } = await import("../../../../test-utils/tenantScopeMock");
  return mockTenantScopeModule(
    await importOriginal<typeof import("../../../../tenant/TenantScopeContext")>(),
    scopeRef,
  );
});

const availableMock = vi.spyOn(serversSdk, "listAvailableMcpServers");
const toolsMock = vi.spyOn(serversSdk, "listMcpServerTools");
const platformCatalogMock = vi.spyOn(catalogSdk, "listPlatformCatalog");
const catalogToolsMock = vi.spyOn(catalogSdk, "listCatalogTools");

beforeEach(async () => {
  availableMock.mockReset();
  toolsMock.mockReset();
  platformCatalogMock.mockReset();
  catalogToolsMock.mockReset();
  scopeRef.current = undefined;
  // 断言里写的是中文文案(「自动（模型填）」等),语言不钉住就随 jsdom 的
  // navigator.language 漂 —— 本机与 CI 解析成 en 时整片假红。
  await i18n.changeLanguage("zh-CN");
});

afterEach(() => {
  // 确认框 portal 到 RTL 容器外面。
  document.body.innerHTML = "";
});

const T1: serversSdk.McpTool = {
  name: "t1",
  description: "",
  input_schema: {
    properties: { project_code: {}, keyword: {} },
    required: ["project_code"],
  },
};

const T2: serversSdk.McpTool = {
  name: "t2",
  description: "",
  input_schema: { properties: { note: {} } },
};

const BINDING: ArgBindingFields = {
  server: "deepcare",
  tool: "t1",
  args: { project_code: "project_code" },
};

interface PickerOpts {
  tools?: serversSdk.McpTool[];
  promptVariables?: string[];
  argBindings?: ArgBindingFields[];
  allowTools?: string[];
  onChange?: (
    servers: string[],
    allowTools: string[],
    argBindings: ArgBindingFields[],
  ) => void;
}

function renderPicker(opts: PickerOpts = {}) {
  availableMock.mockResolvedValue([{ name: "deepcare", source: "tenant" }]);
  toolsMock.mockResolvedValue(opts.tools ?? [T1]);
  const user = userEvent.setup();
  render(
    <App>
      <McpToolPicker
        servers={["deepcare"]}
        allowTools={opts.allowTools ?? []}
        argBindings={opts.argBindings ?? []}
        promptVariables={opts.promptVariables ?? ["project_code"]}
        onChange={opts.onChange ?? (() => {})}
      />
    </App>,
  );
  return user;
}

/**
 * antd 的 Select 在 jsdom 里把每个选项渲染两遍:可见可点的
 * ``.ant-select-item-option`` div,和一个只装「当前+下一个」两条的隐藏 ARIA
 * 镜像 —— 所以 ``getAllByRole("option")`` 既数不全也点不动。整个仓库统一按
 * ``.ant-select-item-option-content`` 取真选项(同 ModelSelect/SettingsSearch
 * 的测试)。
 */
/** 关掉的下拉仍然留在 DOM 里(只是加了 ``-hidden``),所以两次开不同的下拉后
 *  全局按 class 找会撞上一堆旧选项 —— 一律只认当前展开的那一个。 */
const openDropdown = (): HTMLElement => {
  const all = Array.from(
    document.querySelectorAll<HTMLElement>(".ant-select-dropdown"),
  ).filter((el) => !el.classList.contains("ant-select-dropdown-hidden"));
  if (all.length !== 1) {
    throw new Error(`expected exactly one open Select dropdown, got ${all.length}`);
  }
  return all[0];
};

const optionLabels = (): (string | null)[] =>
  Array.from(
    openDropdown().querySelectorAll(".ant-select-item-option-content"),
  ).map((el) => el.textContent);

async function pickOption(
  user: ReturnType<typeof userEvent.setup>,
  label: string,
): Promise<void> {
  const item = await within(openDropdown()).findByText(
    (_content, el) =>
      el?.classList.contains("ant-select-item-option-content") === true &&
      el.textContent === label,
  );
  await user.click(item);
}

/** 绑定 UI 住在「选择工具」弹窗里 —— 先开到那儿,再展开 t1 的参数绑定。 */
async function openBindings(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByTestId("af-mcp-choose-deepcare"));
  await screen.findByTestId("af-mcp-tool-t1");
  await user.click(await screen.findByRole("button", { name: /t1/ }));
}

describe("McpToolPicker 参数绑定", () => {
  it("展开的工具按 input_schema 逐参数给出「自动 / 绑定变量」", async () => {
    await openBindings(renderPicker({ tools: [T1] }));
    expect(screen.getByLabelText("project_code")).toBeInTheDocument();
    expect(screen.getByLabelText("keyword")).toBeInTheDocument();
  });

  it("必填参数带星号,选填的不带", async () => {
    await openBindings(renderPicker({ tools: [T1] }));
    // T1 的 required 只有 project_code。星号挂在参数名旁边,不进 label 文本
    // (进了 ``getByLabelText("project_code")`` 就找不到了)。
    expect(screen.getByTestId("af-mcp-bind-row-t1-project_code").textContent)
      .toContain("project_code*");
    expect(
      screen.getByTestId("af-mcp-bind-row-t1-keyword").textContent,
    ).not.toContain("*");
  });

  it("下拉只列本 agent 声明过的变量", async () => {
    const user = renderPicker({
      promptVariables: ["project_code", "employee_code"],
    });
    await openBindings(user);
    await user.click(screen.getByLabelText("project_code"));
    await waitFor(() => expect(optionLabels().length).toBeGreaterThan(0));
    expect(optionLabels()).toEqual([
      "自动（模型填）",
      "project_code",
      "employee_code",
    ]);
  });

  it("选中变量后 onChange 带出 manifest 形状的 arg_bindings", async () => {
    const onChange = vi.fn();
    const user = renderPicker({ onChange, promptVariables: ["project_code"] });
    await openBindings(user);
    await user.click(screen.getByLabelText("project_code"));
    await pickOption(user, "project_code");
    await waitFor(() =>
      expect(onChange).toHaveBeenLastCalledWith(["deepcare"], expect.anything(), [
        { server: "deepcare", tool: "t1", args: { project_code: "project_code" } },
      ]),
    );
  });

  it("取消绑定会把该条从 arg_bindings 里移掉,空了整条删除", async () => {
    const onChange = vi.fn();
    const user = renderPicker({
      onChange,
      promptVariables: ["project_code"],
      argBindings: [BINDING],
    });
    await openBindings(user);
    await user.click(screen.getByLabelText("project_code"));
    await pickOption(user, "自动（模型填）");
    await waitFor(() =>
      expect(onChange).toHaveBeenLastCalledWith(
        ["deepcare"],
        expect.anything(),
        [],
      ),
    );
  });

  it("声明变量为空时给出提示而不是一个空下拉", async () => {
    await openBindings(renderPicker({ promptVariables: [] }));
    const hint = screen.getByTestId("af-mcp-bind-no-vars-t1");
    expect(hint.textContent).toContain("声明变量");
    // 提示必须指得着**页面上真实存在的**那一节。直接取那两个 i18n 键:谁把分组
    // 或小节改了名,这条就红 —— 这正是我们想要的信号,而不是让提示悄悄过期。
    expect(hint.textContent).toContain(i18n.t("manifest_editor.group_prompt"));
    expect(hint.textContent).toContain(i18n.t("agent_form.section_prompt_vars"));
    expect(screen.queryByLabelText("project_code")).not.toBeInTheDocument();
  });

  // ── 追加要求:删绑定前先说清楚删的是哪几条 ───────────────────────────────
  //
  // 取消勾选服务器 = 关掉 MCP,工具条目连同 arg_bindings 一起没了 —— 这个语义
  // 是对的(用户就是要关 MCP),问题在于绑定在配置页上一直是看不见的,用户不知道
  // 自己刚丢了什么。所以确认框不是拦误操作,是把「即将被静默删掉的东西」摆出来。

  it("取消勾选服务器会先列出要删的绑定再确认;确认后才真的写回去", async () => {
    const onChange = vi.fn();
    const user = renderPicker({
      onChange,
      promptVariables: ["project_code"],
      argBindings: [BINDING],
    });
    await user.click(await screen.findByTestId("af-mcp-server-deepcare"));
    expect(onChange).not.toHaveBeenCalled();

    const dialog = await screen.findByRole("dialog");
    // 追加要求原文:「告诉他这会同时删掉 N 条绑定」——标题必须报出条数。
    expect(dialog.textContent).toContain("这会同时删掉 1 条参数绑定");
    // 逐条摆出来:哪个工具的哪个参数、绑的是哪个变量。
    expect(dialog.textContent).toContain("t1");
    expect(dialog.textContent).toContain("project_code");
    // N 与用户能数到的行数同源:1 条 → 1 行。
    expect(within(dialog).getAllByRole("listitem")).toHaveLength(1);

    await user.click(within(dialog).getByRole("button", { name: /删除/ }));
    await waitFor(() => expect(onChange).toHaveBeenCalledTimes(1));
    expect(onChange).toHaveBeenLastCalledWith([], [], []);
  });

  it("要删多条时条数跟着变,行数与条数对得上", async () => {
    const onChange = vi.fn();
    const user = renderPicker({
      onChange,
      tools: [T1, T2],
      promptVariables: ["project_code", "employee_code"],
      argBindings: [
        {
          server: "deepcare",
          tool: "t1",
          args: { project_code: "project_code", keyword: "employee_code" },
        },
        { server: "deepcare", tool: "t2", args: { note: "employee_code" } },
      ],
    });
    await user.click(await screen.findByTestId("af-mcp-server-deepcare"));
    const dialog = await screen.findByRole("dialog");
    // 两个条目一共绑了三个参数 —— 报的是参数数,和下面三行对得上。
    expect(dialog.textContent).toContain("这会同时删掉 3 条参数绑定");
    expect(within(dialog).getAllByRole("listitem")).toHaveLength(3);
  });

  it("取消确认则什么都不写", async () => {
    const onChange = vi.fn();
    const user = renderPicker({
      onChange,
      promptVariables: ["project_code"],
      argBindings: [BINDING],
    });
    await user.click(await screen.findByTestId("af-mcp-server-deepcare"));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /取\s*消/ }));
    expect(onChange).not.toHaveBeenCalled();
  });

  it("没有绑定时取消勾选服务器不弹确认,直接写回去", async () => {
    const onChange = vi.fn();
    const user = renderPicker({ onChange, argBindings: [] });
    await user.click(await screen.findByTestId("af-mcp-server-deepcare"));
    await waitFor(() => expect(onChange).toHaveBeenLastCalledWith([], [], []));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("绑一个参数不会碰到别的工具上的绑定", async () => {
    const onChange = vi.fn();
    const other: ArgBindingFields = {
      server: "deepcare",
      tool: "t2",
      args: { note: "employee_code" },
    };
    const user = renderPicker({
      onChange,
      tools: [T1, T2],
      promptVariables: ["project_code", "employee_code"],
      argBindings: [other],
    });
    await openBindings(user);
    await user.click(screen.getByLabelText("project_code"));
    await pickOption(user, "project_code");
    await waitFor(() => expect(onChange).toHaveBeenCalled());
    const written = onChange.mock.calls.at(-1)?.[2] as ArgBindingFields[];
    expect(written).toContainEqual(other);
    expect(written).toContainEqual({
      server: "deepcare",
      tool: "t1",
      args: { project_code: "project_code" },
    });
  });

  // 受控回灌:父组件把 onChange 的结果喂回 props。绑第二个参数时第一个必须还在
  // —— 「受控控件 resync 把用户输入吃掉」在本仓库是高发 bug,只用 spy 断言
  // onChange 是验不出来的(spy 从不回灌)。
  it("受控回灌下连绑两个参数,先绑的那个不会被后绑的冲掉", async () => {
    availableMock.mockResolvedValue([{ name: "deepcare", source: "tenant" }]);
    toolsMock.mockResolvedValue([T1]);
    const user = userEvent.setup();
    function Harness() {
      const [bindings, setBindings] = useState<ArgBindingFields[]>([]);
      return (
        <App>
          <McpToolPicker
            servers={["deepcare"]}
            allowTools={[]}
            argBindings={bindings}
            promptVariables={["project_code", "employee_code"]}
            onChange={(_s, _a, next) => setBindings(next)}
          />
        </App>
      );
    }
    render(<Harness />);
    await openBindings(user);

    await user.click(screen.getByLabelText("project_code"));
    await pickOption(user, "project_code");
    await user.click(screen.getByLabelText("keyword"));
    await pickOption(user, "employee_code");

    // 两个下拉都停在各自绑的变量上(不是一个被冲成「自动」)。
    await waitFor(() =>
      expect(
        screen.getByTestId("af-mcp-bind-row-t1-project_code").textContent,
      ).toContain("project_code"),
    );
    expect(
      screen.getByTestId("af-mcp-bind-row-t1-keyword").textContent,
    ).toContain("employee_code");
    // 计数徽标也认得两条。
    expect(
      screen.getByTestId("af-mcp-bind-toggle-t1").textContent,
    ).toContain("2");
  });
});

// ── 平台连接器(source="catalog")也得能绑 ───────────────────────────────────
//
// catalog 分支 map 工具时曾经只带 name/description,把 input_schema 丢了 ——
// 这个 bug 在租户自己注册的服务器上完全看不出来,只有用平台连接器的人会撞上
// 「展开了却一个参数都没有」。
describe("McpToolPicker 参数绑定(平台连接器)", () => {
  it("catalog 探针的 input_schema 要透传,参数才列得出来", async () => {
    platformCatalogMock.mockResolvedValue([
      {
        id: "c1",
        name: "deepcare",
        display_name: "DeepCare",
        enabled: true,
      },
    ] as never);
    catalogToolsMock.mockResolvedValue({
      status: "ok",
      tool_count: 1,
      error: null,
      tools: [
        {
          name: "t1",
          description: "",
          input_schema: { properties: { project_code: {}, keyword: {} } },
        },
      ],
    });
    const user = userEvent.setup();
    render(
      <App>
        <McpToolPicker
          source="catalog"
          servers={["deepcare"]}
          allowTools={[]}
          argBindings={[]}
          promptVariables={["project_code"]}
          onChange={() => {}}
        />
      </App>,
    );
    await openBindings(user);
    expect(screen.getByLabelText("project_code")).toBeInTheDocument();
    expect(screen.getByLabelText("keyword")).toBeInTheDocument();
  });
});

// ── 调用点:FormView 真的把声明变量和绑定接上了吗 ────────────────────────────
//
// 组件本身跑通不代表页面跑通 —— 这一段走真的 FormView,变量取自 manifest 自己的
// system_prompt.variables,写回去的也是真的 manifest。
describe("FormView 把 MCP 绑定接到 manifest 上", () => {
  const SEED: AgentManifest = {
    apiVersion: "expert_work/v1",
    kind: "Agent",
    metadata: { name: "bot" },
    spec: {
      model: { provider: "openai", name: "gpt-4o" },
      system_prompt: {
        template: "hi {{ project_code }}",
        jinja: true,
        variables: [{ name: "project_code" }, { name: "" }],
      },
      tools: [{ type: "mcp", servers: ["deepcare"], allow_tools: [] }],
    },
  };

  it("下拉列的是 manifest 声明的变量,选中后写进 tools[].arg_bindings", async () => {
    vi.spyOn(modelCatalog, "loadModelCatalog").mockResolvedValue({
      providers: [],
    });
    availableMock.mockResolvedValue([{ name: "deepcare", source: "tenant" }]);
    toolsMock.mockResolvedValue([T1]);
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <App>
        <FormView section="mcp" formData={SEED} onChange={onChange} />
      </App>,
    );
    await openBindings(user);

    await user.click(screen.getByLabelText("project_code"));
    await waitFor(() => expect(optionLabels().length).toBeGreaterThan(0));
    // 没起名字的变量行不是一个可绑的变量,不该进下拉。
    expect(optionLabels()).toEqual(["自动（模型填）", "project_code"]);

    await pickOption(user, "project_code");
    await waitFor(() => expect(onChange).toHaveBeenCalled());
    const next = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(readTools(next).mcpArgBindings).toEqual([
      { server: "deepcare", tool: "t1", args: { project_code: "project_code" } },
    ]);
    // 兄弟字段原样保留。
    expect(readTools(next).mcpServers).toEqual(["deepcare"]);
  });

  it("manifest 里已有的绑定,一打开就显示在对应参数上", async () => {
    vi.spyOn(modelCatalog, "loadModelCatalog").mockResolvedValue({
      providers: [],
    });
    availableMock.mockResolvedValue([{ name: "deepcare", source: "tenant" }]);
    toolsMock.mockResolvedValue([T1]);
    const user = userEvent.setup();
    const seeded: AgentManifest = {
      ...SEED,
      spec: {
        ...SEED.spec,
        tools: [
          {
            type: "mcp",
            servers: ["deepcare"],
            allow_tools: [],
            arg_bindings: [BINDING],
          },
        ],
      },
    };
    render(
      <App>
        <FormView section="mcp" formData={seeded} onChange={vi.fn()} />
      </App>,
    );
    await openBindings(user);
    // 已绑的那一行停在变量上,没绑的那一行停在「自动」。
    expect(
      screen.getByTestId("af-mcp-bind-row-t1-project_code").textContent,
    ).toContain("project_code");
    expect(
      screen.getByTestId("af-mcp-bind-row-t1-keyword").textContent,
    ).toContain("自动（模型填）");
    // 收起状态下也看得出「这个工具配过绑定」。
    expect(screen.getByTestId("af-mcp-bind-toggle-t1").textContent).toContain("1");
  });
});
