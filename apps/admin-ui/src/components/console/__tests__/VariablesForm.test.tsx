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
    expect(screen.getAllByText("必填")).toHaveLength(1);
    await userEvent.type(screen.getByTestId("playground-var-customer_code"), "C-1");
    expect(onChange).toHaveBeenLastCalledWith("customer_code", "C-1");
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
