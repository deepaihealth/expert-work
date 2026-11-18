/**
 * B-318 —— 工具给参数声明了可选值(``enum`` / ``oneOf`` / ``anyOf`` 的 ``const``)时,
 * 「固定值」是下拉不是文本框:显示标题、写回值;老配置里的错值显示出来、标红、列出
 * 可选值,不清掉。没有可选值的参数照旧文本框(B-127 那批用例在
 * ``McpToolPicker.bindings.test.tsx``)。
 *
 * 另起一个文件:那个文件已经 900 多行。下面几个 helper 与它同款(antd Select 在
 * jsdom 里的取法见那边的注释)。
 */
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import i18n from "../../../../i18n";

import { McpToolPicker } from "../McpToolPicker";
import type { ArgBindingFields } from "../../form_model";
import * as serversSdk from "../../../../api/mcp-servers";

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

const HANDOFF: serversSdk.McpTool = {
  name: "handoff_to_human",
  description: "",
  input_schema: {
    type: "object",
    properties: {
      this_turn: {
        type: "string",
        oneOf: [
          { const: "reply", title: "本轮回复" },
          { const: "silent", title: "本轮不回" },
        ],
      },
      level: { type: "string", enum: ["brief", "full"] },
      note: { type: "string" },
    },
    required: ["this_turn"],
  },
};

const FIXED_ID = (param: string) => `af-mcp-bind-fixed-handoff_to_human-${param}`;
const HINT_ID = (param: string) => `af-mcp-bind-fixed-hint-handoff_to_human-${param}`;

let langBefore = i18n.language;

beforeEach(async () => {
  availableMock.mockReset();
  toolsMock.mockReset();
  scopeRef.current = undefined;
  langBefore = i18n.language;
  await i18n.changeLanguage("zh-CN");
});

afterEach(async () => {
  await i18n.changeLanguage(langBefore);
});

function ControlledPicker({ initial }: { initial: ArgBindingFields[] }) {
  const [bindings, setBindings] = useState<ArgBindingFields[]>(initial);
  return (
    <App>
      <McpToolPicker
        servers={["deepcare"]}
        allowTools={[]}
        argBindings={bindings}
        promptVariables={[]}
        onChange={(_s, _a, next) => setBindings(next)}
      />
      <pre data-testid="bindings-json">{JSON.stringify(bindings)}</pre>
    </App>
  );
}

async function renderAndOpen(initial: ArgBindingFields[] = []) {
  availableMock.mockResolvedValue([{ name: "deepcare", source: "tenant" }]);
  toolsMock.mockResolvedValue([HANDOFF]);
  const user = userEvent.setup();
  render(<ControlledPicker initial={initial} />);
  await user.click(await screen.findByTestId("af-mcp-choose-deepcare"));
  await screen.findByTestId("af-mcp-tool-handoff_to_human");
  await user.click(screen.getByTestId("af-mcp-bind-toggle-handoff_to_human"));
  return user;
}

const writtenBindings = (): unknown =>
  JSON.parse(screen.getByTestId("bindings-json").textContent ?? "[]");

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
  Array.from(openDropdown().querySelectorAll(".ant-select-item-option-content")).map(
    (el) => el.textContent,
  );

async function pickOption(user: ReturnType<typeof userEvent.setup>, label: string) {
  const item = await within(openDropdown()).findByText(
    (_content, el) =>
      el?.classList.contains("ant-select-item-option-content") === true &&
      el.textContent === label,
  );
  await user.click(item);
}

/** 选「固定值」那一档(参数行左边的那个下拉,按参数名的 label 找)。 */
async function chooseFixed(user: ReturnType<typeof userEvent.setup>, param: string) {
  await user.click(screen.getByLabelText(param));
  await pickOption(user, "固定值");
}

/** 固定值下拉的输入框(antd 把 aria-label 同时挂在输入框和弹出列表上,按 role 取)。 */
const fixedCombobox = (param: string): HTMLElement =>
  screen.getByRole("combobox", { name: `${param} 的固定值` });

/** 固定值下拉的根节点(状态 class 挂在这里)。 */
const fixedSelect = (param: string): HTMLElement => {
  const root = screen.getByTestId(FIXED_ID(param));
  return root.classList.contains("ant-select") ? root : (root.closest(".ant-select") as HTMLElement);
};

describe("McpToolPicker 固定值的可选值(B-318)", () => {
  it("声明了可选值的参数选「固定值」出下拉、不出文本框,选项显示标题", async () => {
    const user = await renderAndOpen();
    await chooseFixed(user, "this_turn");
    const root = fixedSelect("this_turn");
    expect(root).toBeInTheDocument();
    expect(root.tagName).not.toBe("INPUT");
    expect(root.querySelector("input")?.getAttribute("role")).toBe("combobox");
    // 还没选:标红、提示「选一个值」。
    expect(root.className).toContain("ant-select-status-error");
    expect(screen.getByTestId(HINT_ID("this_turn")).textContent).toBe("选一个值");

    await user.click(fixedCombobox("this_turn"));
    expect(optionLabels()).toEqual(["本轮回复", "本轮不回"]);
  });

  it("选「本轮不回」写回的是值 silent,不是标题", async () => {
    const user = await renderAndOpen();
    await chooseFixed(user, "this_turn");
    await user.click(fixedCombobox("this_turn"));
    await pickOption(user, "本轮不回");
    await waitFor(() =>
      expect(writtenBindings()).toEqual([
        { server: "deepcare", tool: "handoff_to_human", fixed: { this_turn: "silent" } },
      ]),
    );
    expect(fixedSelect("this_turn").className).not.toContain("ant-select-status-error");
    expect(screen.queryByTestId(HINT_ID("this_turn"))).not.toBeInTheDocument();
  });

  it("enum 没有标题时选项显示值本身", async () => {
    const user = await renderAndOpen();
    await chooseFixed(user, "level");
    await user.click(fixedCombobox("level"));
    expect(optionLabels()).toEqual(["brief", "full"]);
  });

  it("没有可选值的参数照旧是文本框", async () => {
    const user = await renderAndOpen();
    await chooseFixed(user, "note");
    const input = screen.getByTestId(FIXED_ID("note"));
    expect(input.tagName).toBe("INPUT");
    expect(input.closest(".ant-select")).toBeNull();
    await user.type(input, "hi");
    await waitFor(() =>
      expect(writtenBindings()).toEqual([
        { server: "deepcare", tool: "handoff_to_human", fixed: { note: "hi" } },
      ]),
    );
  });

  it("老配置里的错值:显示原值、标红、列出可选值,配置不被改", async () => {
    const initial: ArgBindingFields[] = [
      { server: "deepcare", tool: "handoff_to_human", fixed: { this_turn: "slient" } },
    ];
    const user = await renderAndOpen(initial);
    const root = fixedSelect("this_turn");
    expect(root.textContent).toContain("slient");
    expect(root.className).toContain("ant-select-status-error");
    expect(screen.getByTestId(HINT_ID("this_turn")).textContent).toBe(
      "当前值 slient 不在工具允许的值里，可选：本轮回复 reply / 本轮不回 silent",
    );
    expect(writtenBindings()).toEqual(initial);

    // 重新选一个合法值才写回。
    await user.click(fixedCombobox("this_turn"));
    await pickOption(user, "本轮回复");
    await waitFor(() =>
      expect(writtenBindings()).toEqual([
        { server: "deepcare", tool: "handoff_to_human", fixed: { this_turn: "reply" } },
      ]),
    );
    expect(screen.queryByTestId(HINT_ID("this_turn"))).not.toBeInTheDocument();
  });
});

describe.each([
  ["zh-CN", "选一个值", "当前值 slient 不在工具允许的值里，可选：本轮回复 reply / 本轮不回 silent"],
  ["en", "Pick a value", "slient is not a value this tool allows. Allowed: 本轮回复 reply / 本轮不回 silent"],
] as const)("固定值下拉的提示文案 (%s)", (lang, pick, notAllowed) => {
  beforeEach(async () => {
    await i18n.changeLanguage(lang);
  });

  it("空值与错值的提示按本 locale 渲染", async () => {
    await renderAndOpen([
      {
        server: "deepcare",
        tool: "handoff_to_human",
        fixed: { this_turn: "slient", level: "" },
      },
    ]);
    expect(screen.getByTestId(HINT_ID("this_turn")).textContent).toBe(notAllowed);
    expect(screen.getByTestId(HINT_ID("level")).textContent).toBe(pick);
  });
});
