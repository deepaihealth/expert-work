import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import i18n from "../../../i18n";

import { PromptVariablesEditor } from "../PromptVariablesEditor";
import { readPromptVariables, readTools } from "../form_model";
import type { AgentManifest, ArgBindingFields, PromptVariableFields } from "../form_model";

/**
 * 「这份 manifest 里有几条孤儿绑定」—— 即绑定指向的变量名不在
 * ``system_prompt.variables`` 里。这正是 ``_check_arg_bindings`` 规则 (4) 拒绝
 * 保存的条件,所以下面的断言判的是**产没产出孤儿 manifest**,而不是某个控件
 * 禁没禁用(控件属性坏掉最多是难看,孤儿才是 422)。
 */
function orphansIn(m: unknown): string[] {
  const declared = new Set(
    readPromptVariables(m)
      .map((v) => v.name)
      .filter((n): n is string => (n ?? "") !== ""),
  );
  return readTools(m).mcpArgBindings.flatMap((b) =>
    Object.entries(b.args)
      .filter(([, variable]) => !declared.has(variable))
      .map(([param, variable]) => `${b.server}/${b.tool}.${param}=${variable}`),
  );
}

const SEED: AgentManifest = {
  apiVersion: "expert_work/v1",
  kind: "Agent",
  metadata: { name: "bot" },
  spec: { system_prompt: { template: "hi {{ persona }}" } },
};

function jinjaSeed(variables: PromptVariableFields[]): AgentManifest {
  return {
    ...SEED,
    spec: {
      system_prompt: { template: "hi {{ persona }}", jinja: true, variables },
    },
  };
}

describe("PromptVariablesEditor", () => {
  it("renders the jinja toggle; variable rows hidden until enabled", () => {
    render(
      <App>
        <PromptVariablesEditor formData={SEED} onChange={vi.fn()} />
      </App>,
    );
    expect(screen.getByTestId("af-prompt-jinja")).toBeInTheDocument();
    expect(screen.queryByTestId("af-prompt-var-add")).not.toBeInTheDocument();
  });

  it("toggling jinja on emits jinja:true", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <App>
        <PromptVariablesEditor formData={SEED} onChange={onChange} />
      </App>,
    );
    await user.click(screen.getByTestId("af-prompt-jinja"));
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.system_prompt?.jinja).toBe(true);
  });

  it("shows the variable editor + add button when jinja is on", () => {
    render(
      <App>
        <PromptVariablesEditor formData={jinjaSeed([])} onChange={vi.fn()} />
      </App>,
    );
    expect(screen.getByTestId("af-prompt-var-add")).toBeInTheDocument();
  });

  it("adding a variable appends a trusted+required row", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <App>
        <PromptVariablesEditor formData={jinjaSeed([])} onChange={onChange} />
      </App>,
    );
    await user.click(screen.getByTestId("af-prompt-var-add"));
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.system_prompt?.variables).toEqual([
      { name: "", trusted: true, required: true, description: "" },
    ]);
  });

  it("editing a variable name patches that row", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <App>
        <PromptVariablesEditor
        formData={jinjaSeed([{ name: "", trusted: true, required: true }])}
        onChange={onChange}
      />
      </App>,
    );
    await user.type(screen.getByTestId("af-prompt-var-name-0"), "p");
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.system_prompt?.variables?.[0].name).toBe("p");
  });

  it("toggling trusted off patches the row", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <App>
        <PromptVariablesEditor
        formData={jinjaSeed([
          { name: "profile", trusted: true, required: true },
        ])}
        onChange={onChange}
      />
      </App>,
    );
    await user.click(screen.getByTestId("af-prompt-var-trusted-0"));
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.system_prompt?.variables?.[0].trusted).toBe(false);
  });
});

describe("PromptVariablesEditor capacity (BUG-4)", () => {
  it("wraps variable rows in an internal scroll container with a count", () => {
    const variables: PromptVariableFields[] = Array.from(
      { length: 12 },
      (_, i) => ({ name: `v${i}` }),
    );
    render(
      <App>
        <PromptVariablesEditor formData={jinjaSeed(variables)} onChange={vi.fn()} />
      </App>,
    );
    const scroll = screen.getByTestId("af-prompt-vars-scroll");
    expect(scroll.style.overflowY).toBe("auto");
    expect(scroll.style.maxHeight).toBe("40vh");
    // 12 行都在滚动容器内,添加按钮在容器外恒可见。
    expect(scroll.querySelectorAll("[data-testid^='af-prompt-var-row-']")).toHaveLength(12);
    expect(screen.getByTestId("af-prompt-vars-count").textContent).toContain("12");
    const add = screen.getByTestId("af-prompt-var-add");
    expect(scroll.contains(add)).toBe(false);
  });
});

describe("PromptVariablesEditor add-in-scroll (终审第二轮)", () => {
  it("focuses the new row's name input after add (visible feedback)", async () => {
    const user = userEvent.setup();
    // 受控回环:onChange 后重渲染出新行,才轮到 rAF 聚焦。
    function Harness() {
      const [data, setData] = useState<unknown>(
        jinjaSeed([{ name: "a" }, { name: "b" }]),
      );
      return (
        <App>
          <PromptVariablesEditor formData={data} onChange={setData} />
        </App>
      );
    }
    render(<Harness />);
    await user.click(screen.getByTestId("af-prompt-var-add"));
    await new Promise((r) => requestAnimationFrame(() => r(null)));
    const newInput = screen.getByTestId("af-prompt-var-name-2");
    expect(document.activeElement).toBe(newInput);
  });
});

// ── B-61 修复轮 1:被工具参数绑着的声明变量,删不得也改不得名 ────────────────
//
// 两条路都会留下一条指向不存在变量的绑定,而协议层规则 (4) 对这个是**直接拒绝
// 保存**:用户拿到一个 422,页面上却没有任何东西解释得了 —— antd 的 Select 把
// 不在 options 里的 value 原样当 label 显示,那条孤儿看上去完全正常。
// 改名比删除更隐蔽:用户以为只是改个名字。
describe("PromptVariablesEditor 绑定守卫(B-61)", () => {
  const BOUND: ArgBindingFields[] = [
    {
      server: "deepcare",
      tool: "customer_search",
      args: { project_code: "project_code", keyword: "project_code" },
    },
  ];

  const boundSeed = (
    variables: PromptVariableFields[],
    argBindings: ArgBindingFields[] = BOUND,
  ): AgentManifest => ({
    ...SEED,
    spec: {
      system_prompt: { template: "hi", jinja: true, variables },
      tools: [
        {
          type: "mcp",
          servers: ["deepcare"],
          allow_tools: ["customer_search"],
          arg_bindings: argBindings,
        },
      ],
    },
  });

  // 两个 locale 都跑:文案里的 server / 条数任一 locale 掉了都要红。
  it.each(["zh-CN", "en"])(
    "删被绑的变量:拦住、报数、列出是哪几个工具参数在用,且一个字都不写回去(%s)",
    async (lang) => {
      const before = i18n.language;
      await i18n.changeLanguage(lang);
      try {
        const user = userEvent.setup();
        const onChange = vi.fn();
        render(
          <App>
            <PromptVariablesEditor
              formData={boundSeed([{ name: "project_code" }])}
              onChange={onChange}
            />
          </App>,
        );
        await user.click(screen.getByTestId("af-prompt-var-remove-0"));
        const dialog = await screen.findByRole("dialog");
        // 与 MCP 那边确认框同一口径:报数 + 逐条列出来。
        expect(dialog.textContent).toContain(
          i18n.t("agent_form.prompt_var_remove_blocked", { count: 2 }),
        );
        expect(within(dialog).getAllByRole("listitem")).toHaveLength(2);
        expect(dialog.textContent).toContain("customer_search");
        expect(dialog.textContent).toContain("project_code");
        expect(dialog.textContent).toContain("keyword");
        // 带上 server —— 两台服务器暴露同名工具时,不带就是两行看起来一样的条目。
        expect(dialog.textContent).toContain("deepcare");
        // 拒删 = 真的没删(design §5.4:这里没有「继续」那一档)。
        expect(onChange).not.toHaveBeenCalled();
      } finally {
        await i18n.changeLanguage(before);
      }
    },
  );

  // 修复轮 2 / N2 —— 拦在**写入口**上,不是拦在控件的 disabled 上。
  // disabled 只压住浏览器自己派发的交互事件;程序化赋值(扩展 / 密码管理器 /
  // devtools)照样能把值送进 React 的 onChange。所以这条真的走那条绕过路径,
  // 而不是打字 —— 打字测的是可供性,不是不变式。
  it("改被绑变量的名字:即使绕过 disabled 直接派 input 事件,也产不出孤儿 manifest", async () => {
    const onChange = vi.fn();
    render(
      <App>
        <PromptVariablesEditor
          formData={boundSeed([{ name: "project_code" }])}
          onChange={onChange}
        />
      </App>,
    );
    const input = screen.getByTestId("af-prompt-var-name-0") as HTMLInputElement;
    // native setter + 派发 input:React 的根监听器照收,disabled 拦不住。
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype,
      "value",
    )?.set;
    setter?.call(input, "programmatic_rename");
    fireEvent.input(input, { target: { value: "programmatic_rename" } });

    // 判据是「有没有产出孤儿 manifest」,不是「按钮禁没禁用」。
    expect(onChange).not.toHaveBeenCalled();
    expect(orphansIn(boundSeed([{ name: "project_code" }]))).toEqual([]);
  });

  it("改被绑变量的非名字字段照常放行(只挡 name)", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <App>
        <PromptVariablesEditor
          formData={boundSeed([{ name: "project_code" }])}
          onChange={onChange}
        />
      </App>,
    );
    await user.click(screen.getByTestId("af-prompt-var-trusted-0"));
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.system_prompt?.variables).toEqual([
      { name: "project_code", trusted: false },
    ]);
    expect(orphansIn(last)).toEqual([]);
  });

  // 修复轮 2 / N3 —— 「怎么解锁」必须在常驻可见的文字里。上一版放在挂着
  // disabled <input> 的 Tooltip 上,antd v5 + Chromium 合起来永远不显示。
  // 两个语言都验:只验当前语言的话,另一个 locale 退化成「只剩一个数字」没人
  // 知道 —— 本轮第一遍变异就是这么活下来的(改了 zh,而测试跑在 en)。
  it.each(["zh-CN", "en"])(
    "锁住的那一行,常驻小字里同时写着为什么和怎么办(%s)",
    async (lang) => {
      const before = i18n.language;
      await i18n.changeLanguage(lang);
      try {
        render(
          <App>
            <PromptVariablesEditor
              formData={boundSeed([{ name: "project_code" }])}
              onChange={vi.fn()}
            />
          </App>,
        );
        const note = screen.getByTestId("af-prompt-var-bound-0");
        // 「为什么」:有数。
        expect(note.textContent).toContain("2");
        // 「怎么办」:指得着 MCP,并且点名要改回「自动（模型填）」那一档。
        // 光靠 toBe(i18n.t(...)) 是重言式 —— 文案缩水时两边一起缩,断言不会红。
        expect(note.textContent).toContain("MCP");
        expect(note.textContent).toContain(i18n.t("agent_form.mcp_bind_auto"));
      } finally {
        await i18n.changeLanguage(before);
      }
    },
  );

  it("没被绑的变量:删得掉、改得了名,一如从前", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <App>
        <PromptVariablesEditor
          formData={boundSeed([{ name: "project_code" }, { name: "unused" }])}
          onChange={onChange}
        />
      </App>,
    );
    const input = screen.getByTestId("af-prompt-var-name-1");
    expect(input).not.toBeDisabled();
    expect(screen.queryByTestId("af-prompt-var-bound-1")).not.toBeInTheDocument();

    await user.type(input, "2");
    expect(onChange).toHaveBeenCalled();
    onChange.mockClear();

    await user.click(screen.getByTestId("af-prompt-var-remove-1"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.system_prompt?.variables).toEqual([{ name: "project_code" }]);
  });

  // 修复轮 2 / N1 —— 关掉 Jinja 一次就把 variables 整块删掉,于是**每一条**绑定
  // 都成孤儿;上一版这条路上一个闸都没有,一次点击零提示产出 422。
  //
  // 它不是「拒绝」而是「确认」:关掉动态提示词是用户的真实意图(和取消勾最后一个
  // MCP 服务器完全同构),连带清掉绑定是符合预期的语义。要挡的是「看不见的东西
  // 被无声删掉」,所以口径与 MCP 那个确认框一致:报数 + 列出来。
  it("关 Jinja:先报数再列出来,确认之后绑定跟着一起清,产不出孤儿", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <App>
        <PromptVariablesEditor
          formData={boundSeed([{ name: "project_code" }])}
          onChange={onChange}
        />
      </App>,
    );
    await user.click(screen.getByTestId("af-prompt-jinja"));
    expect(onChange).not.toHaveBeenCalled();

    const dialog = await screen.findByRole("dialog");
    expect(dialog.textContent).toContain(
      i18n.t("agent_form.prompt_jinja_off_title", { count: 2 }),
    );
    expect(within(dialog).getAllByRole("listitem")).toHaveLength(2);
    expect(dialog.textContent).toContain("deepcare");
    expect(dialog.textContent).toContain("customer_search");

    await user.click(
      within(dialog).getByRole("button", {
        name: new RegExp(i18n.t("agent_form.prompt_jinja_off_ok").replace(/\s+/g, "\\s*")),
      }),
    );
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    // 判据是孤儿数,不是「弹没弹框」。
    expect(orphansIn(last)).toEqual([]);
    expect(last.spec?.system_prompt?.variables).toBeUndefined();
    expect(readTools(last).mcpArgBindings).toEqual([]);
    // 兄弟字段没被顺手动掉。
    expect(readTools(last).mcpServers).toEqual(["deepcare"]);
    expect(readTools(last).mcpAllowTools).toEqual(["customer_search"]);
  });

  it("关 Jinja:取消确认则一个字都不写", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <App>
        <PromptVariablesEditor
          formData={boundSeed([{ name: "project_code" }])}
          onChange={onChange}
        />
      </App>,
    );
    await user.click(screen.getByTestId("af-prompt-jinja"));
    const dialog = await screen.findByRole("dialog");
    await user.click(
      within(dialog).getByRole("button", {
        name: new RegExp(i18n.t("agent_form.prompt_jinja_off_cancel").replace(/\s+/g, "\\s*")),
      }),
    );
    expect(onChange).not.toHaveBeenCalled();
  });

  it("关 Jinja:没有绑定时不弹框,直接关", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <App>
        <PromptVariablesEditor
          formData={boundSeed([{ name: "unused" }], [])}
          onChange={onChange}
        />
      </App>,
    );
    await user.click(screen.getByTestId("af-prompt-jinja"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.system_prompt?.jinja).toBeUndefined();
    expect(orphansIn(last)).toEqual([]);
  });

  it("绑的是别的变量时,这一行不受影响", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <App>
        <PromptVariablesEditor
          formData={boundSeed([{ name: "project_code" }], [
            {
              server: "deepcare",
              tool: "customer_search",
              args: { project_code: "employee_code" },
            },
          ])}
          onChange={onChange}
        />
      </App>,
    );
    // 参数名恰好也叫 project_code,但绑的是 employee_code —— 别按参数名误判。
    expect(screen.getByTestId("af-prompt-var-name-0")).not.toBeDisabled();
    await user.click(screen.getByTestId("af-prompt-var-remove-0"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(onChange).toHaveBeenCalled();
  });
});
