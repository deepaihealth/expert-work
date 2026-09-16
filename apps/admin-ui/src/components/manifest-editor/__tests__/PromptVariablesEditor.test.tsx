import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import i18n from "../../../i18n";

import { PromptVariablesEditor } from "../PromptVariablesEditor";
import type { AgentManifest, ArgBindingFields, PromptVariableFields } from "../form_model";

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

  it("删被绑的变量:拦住、报数、列出是哪几个工具参数在用,且一个字都不写回去", async () => {
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
    // 走 i18n 值而不是中文字面量 —— 这个文件不钉语言(jsdom 解析成 en),
    // 钉字面量等于把断言绑在 runner 的 navigator.language 上。
    expect(dialog.textContent).toContain(
      i18n.t("agent_form.prompt_var_remove_blocked", { count: 2 }),
    );
    expect(within(dialog).getAllByRole("listitem")).toHaveLength(2);
    expect(dialog.textContent).toContain("customer_search");
    expect(dialog.textContent).toContain("project_code");
    expect(dialog.textContent).toContain("keyword");
    // 拒删 = 真的没删(design §5.4:这里没有「继续」那一档)。
    expect(onChange).not.toHaveBeenCalled();
  });

  it("改被绑变量的名字:输入框锁住,敲不进去也发不出 onChange", async () => {
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
    const input = screen.getByTestId("af-prompt-var-name-0");
    expect(input).toBeDisabled();
    await user.type(input, "x");
    expect(onChange).not.toHaveBeenCalled();
    // 锁住的理由就在旁边,不是一个没解释的灰框。
    expect(screen.getByTestId("af-prompt-var-bound-0").textContent).toContain("2");
  });

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
