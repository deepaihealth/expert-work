/**
 * VariablesForm — one input row per prompt variable (调试台重设计 PR-A Task 7).
 *
 * Lifted verbatim out of ``PlaygroundTab.tsx`` (see ``playground-vars`` /
 * ``playground-var-<name>`` testids there); ``missingRequired`` is the pure
 * function the parent (Task 19) will use to compute ``Composer``'s
 * ``missingVariables`` prop.
 */
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import i18n from "../../../i18n";

import { VariablesForm, missingRequired, type PromptVariable } from "../VariablesForm";

const TEST_VARS: readonly PromptVariable[] = [
  { name: "customer_code", description: "客户编码", required: true },
  { name: "tone", required: false },
];

/** ``VariablesForm`` is a controlled component (``values`` is a prop, not
 *  internal state) — thread it through local state here the way the real
 *  parent (``PlaygroundTab`` today, ``Composer``'s consumer after Task 19)
 *  does, so typing multiple characters accumulates instead of resetting to
 *  the static prop on every antd ``Input`` re-render. */
function ControlledHarness({
  onChange,
  disabled = false,
}: {
  onChange: (name: string, value: string) => void;
  disabled?: boolean;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  return (
    <VariablesForm
      variables={TEST_VARS}
      values={values}
      onChange={(name, value) => {
        setValues((prev) => ({ ...prev, [name]: value }));
        onChange(name, value);
      }}
      disabled={disabled}
    />
  );
}

describe("VariablesForm", () => {
  // Locale-sensitive assertion below (the "必填" required mark) — pin zh-CN
  // explicitly and restore afterward so it doesn't leak into other test
  // files (the i18n singleton persists its resolved language across `it`
  // blocks / files in the same worker).
  let priorLang: string;
  beforeEach(async () => {
    priorLang = i18n.language;
    await i18n.changeLanguage("zh-CN");
  });
  afterEach(async () => {
    await i18n.changeLanguage(priorLang);
  });

  it("renders one input per variable with the required mark and forwards edits", async () => {
    const onChange = vi.fn();
    render(<ControlledHarness onChange={onChange} />);
    // BUG-7 redesign — the mark is a red asterisk carrying the localized
    // label as its hover title (one per required variable).
    expect(screen.getAllByTitle("必填")).toHaveLength(1);
    await userEvent.type(screen.getByTestId("playground-var-customer_code"), "C-1");
    expect(onChange).toHaveBeenLastCalledWith("customer_code", "C-1");
  });

  it("header shows the variable count and the missing-required count", async () => {
    render(<ControlledHarness onChange={vi.fn()} />);
    expect(screen.getByText(/Prompt 变量 \(2\)/)).toBeInTheDocument();
    expect(screen.getByTestId("playground-vars-missing")).toHaveTextContent(
      "1 项必填未填",
    );
    await userEvent.type(screen.getByTestId("playground-var-customer_code"), "C-1");
    expect(screen.queryByTestId("playground-vars-missing")).not.toBeInTheDocument();
  });

  it("starts open while required values are missing and does not fold on fill", async () => {
    render(<ControlledHarness onChange={vi.fn()} />);
    const input = screen.getByTestId("playground-var-customer_code");
    await userEvent.type(input, "C-1");
    // Filling the last required value must NOT yank the section shut.
    expect(screen.getByTestId("playground-var-customer_code")).toBeInTheDocument();
  });

  it("re-opens when values are reset in place (新建会话 without remount)", async () => {
    const vars: readonly PromptVariable[] = [{ name: "code", required: true }];
    const { rerender } = render(
      <VariablesForm variables={vars} values={{ code: "C-1" }} onChange={vi.fn()} disabled={false} />,
    );
    // Satisfied → starts folded.
    expect(screen.queryByTestId("playground-var-code")).not.toBeInTheDocument();
    // Parent resets values without remounting — required is missing again,
    // the section must re-open (inputs off-screen while send is blocked).
    rerender(
      <VariablesForm variables={vars} values={{}} onChange={vi.fn()} disabled={false} />,
    );
    expect(screen.getByTestId("playground-var-code")).toBeInTheDocument();
  });

  it("starts folded when nothing required is missing and the header toggles it", async () => {
    render(
      <VariablesForm
        variables={[{ name: "tone", required: false }]}
        values={{}}
        onChange={vi.fn()}
        disabled={false}
      />,
    );
    expect(screen.queryByTestId("playground-var-tone")).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId("playground-vars-toggle"));
    // 规格 C(必填前置)—— 选填项住在自己的折叠组里,外层打开后还要再开一层。
    expect(screen.queryByTestId("playground-var-tone")).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId("playground-vars-optional-toggle"));
    expect(screen.getByTestId("playground-var-tone")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("playground-vars-toggle"));
    expect(screen.queryByTestId("playground-var-tone")).not.toBeInTheDocument();
  });

  // 规格 C(必填前置)—— 必填组永远排在「更多 N 项(选填)」折叠之前,
  // 与 manifest 里的声明顺序无关。
  it("renders the required group before the optional fold even when declared after", () => {
    render(
      <VariablesForm
        variables={[
          { name: "tone", required: false },
          { name: "customer_code", required: true },
        ]}
        values={{}}
        onChange={vi.fn()}
        disabled={false}
      />,
    );
    const requiredCell = screen.getByTestId("playground-var-cell-customer_code");
    const optionalToggle = screen.getByTestId("playground-vars-optional-toggle");
    expect(optionalToggle).toHaveTextContent("更多 1 项（选填）");
    expect(
      requiredCell.compareDocumentPosition(optionalToggle) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    // 选填组默认收起。
    expect(screen.queryByTestId("playground-var-tone")).not.toBeInTheDocument();
    expect(screen.getByTestId("playground-vars-optional-toggle")).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });

  // 规格 C —— 「有值的选填项所在组打开」:草稿预填 / 恢复会话带回值时,
  // 选填组不能把已有值藏起来。
  it("opens the optional fold when an optional variable already has a value", async () => {
    render(
      <VariablesForm
        variables={TEST_VARS}
        values={{ customer_code: "C-1", tone: "正式" }}
        onChange={vi.fn()}
        disabled={false}
      />,
    );
    // 必填已满足 → 外层从折叠开始;打开外层后,选填组因带值直接是展开的
    // (不需要再点选填折叠)。
    await userEvent.click(screen.getByTestId("playground-vars-toggle"));
    expect(screen.getByTestId("playground-var-tone")).toBeInTheDocument();
  });

  it("opens the optional fold when an optional value arrives after mount (prefill)", async () => {
    const { rerender } = render(
      <VariablesForm variables={TEST_VARS} values={{}} onChange={vi.fn()} disabled={false} />,
    );
    // 外层因必填未填自动打开,但选填组收起。
    expect(screen.queryByTestId("playground-var-tone")).not.toBeInTheDocument();
    // 草稿预填给选填项带来值(父级 setState,不重挂载)→ 选填组展开。
    rerender(
      <VariablesForm
        variables={TEST_VARS}
        values={{ tone: "正式" }}
        onChange={vi.fn()}
        disabled={false}
      />,
    );
    expect(await screen.findByTestId("playground-var-tone")).toBeInTheDocument();
  });

  // 规格 C(说明挪位)—— 说明不再当 placeholder(输入后就看不见了),改挂
  // label 行尾的问号 tooltip;没有说明的变量不出问号。
  it("shows the description as a help tooltip instead of the placeholder", () => {
    render(<ControlledHarness onChange={vi.fn()} />);
    expect(
      screen.getByTestId("field-help-playground-var-customer_code"),
    ).toBeInTheDocument();
    // 变量 spec 没有示例字段 → placeholder 留空。
    expect(
      screen.getByTestId("playground-var-customer_code"),
    ).not.toHaveAttribute("placeholder");
    // tone 没写说明 → 不渲染问号。
    expect(
      screen.queryByTestId("field-help-playground-var-tone"),
    ).not.toBeInTheDocument();
  });

  // ④ 反馈 — 11 个变量全宽竖排吃掉大半屏:多列自适应 grid(宽屏两三列,
  // 窄屏回单列)+ 40vh 封顶内滚(同 manifest-editor 变量列表的内滚形态)。
  it("lays the open form out as an adaptive multi-column grid capped at 40vh with inner scroll", () => {
    render(<ControlledHarness onChange={vi.fn()} />);
    const grid = screen.getByTestId("playground-vars-grid");
    expect(grid.style.display).toBe("grid");
    expect(grid.style.gridTemplateColumns).toBe("repeat(auto-fill, minmax(320px, 1fr))");
    expect(grid.style.maxHeight).toBe("40vh");
    expect(grid.style.overflowY).toBe("auto");
    // label|input 的行内对齐保持:每个变量一格,格内仍是 label 在左输入在右。
    const cell = screen.getByTestId("playground-var-customer_code").closest(
      "[data-testid='playground-var-cell-customer_code']",
    );
    expect(cell).not.toBeNull();
  });

  it("renders nothing when there are no variables", () => {
    const { container } = render(
      <VariablesForm variables={[]} values={{}} onChange={vi.fn()} disabled={false} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("disables every input when disabled is true", () => {
    render(
      <VariablesForm
        variables={[{ name: "customer_code", required: true }]}
        values={{}}
        onChange={vi.fn()}
        disabled
      />,
    );
    expect(screen.getByTestId("playground-var-customer_code")).toBeDisabled();
  });

  it("missingRequired lists required vars whose value is empty/whitespace, in declaration order", () => {
    expect(
      missingRequired(
        [{ name: "a" }, { name: "b", required: false }, { name: "c", required: true }],
        { a: " ", c: "x" },
      ),
    ).toEqual(["a"]);
  });
});
