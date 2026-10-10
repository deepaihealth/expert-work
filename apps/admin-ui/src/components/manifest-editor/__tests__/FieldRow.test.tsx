import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../../i18n";

import { FieldRow } from "../FieldRow";

describe("FieldRow", () => {
  it("always renders the label, the control, and the one-line brief", () => {
    render(
      <FieldRow
        fieldId="workflow.max_iterations"
        label="最大迭代次数"
        brief="限制单次运行的最大步数"
        isDefault
      >
        <input aria-label="max-iterations-input" />
      </FieldRow>,
    );

    expect(screen.getByText("最大迭代次数")).toBeInTheDocument();
    expect(screen.getByText("限制单次运行的最大步数")).toBeInTheDocument();
    expect(screen.getByLabelText("max-iterations-input")).toBeInTheDocument();
  });

  it("exposes data-field-id on the row root for the given fieldId", () => {
    const { container } = render(
      <FieldRow
        fieldId="workflow.timeout_seconds"
        label="超时时间"
        brief="单次运行的最长秒数"
        isDefault
      >
        <input />
      </FieldRow>,
    );

    expect(
      container.querySelector('[data-field-id="workflow.timeout_seconds"]'),
    ).toBeInTheDocument();
  });

  it("renders no ⓘ trigger when help is omitted", () => {
    render(
      <FieldRow
        fieldId="workflow.max_iterations"
        label="最大迭代次数"
        brief="限制单次运行的最大步数"
        isDefault
      >
        <input />
      </FieldRow>,
    );

    expect(
      screen.queryByTestId("field-help-workflow.max_iterations"),
    ).not.toBeInTheDocument();
  });

  it("renders a ⓘ trigger when help is given, and clicking it reveals the long explanation", async () => {
    const user = userEvent.setup();
    render(
      <FieldRow
        fieldId="workflow.max_iterations"
        label="最大迭代次数"
        brief="限制单次运行的最大步数"
        help="调大会增加单次运行时长与成本;调小可能导致任务提前中断"
        isDefault
      >
        <input />
      </FieldRow>,
    );

    const trigger = screen.getByTestId("field-help-workflow.max_iterations");
    expect(trigger).toBeInTheDocument();
    expect(
      screen.queryByText("调大会增加单次运行时长与成本;调小可能导致任务提前中断"),
    ).not.toBeInTheDocument();

    await user.click(trigger);

    expect(
      await screen.findByText("调大会增加单次运行时长与成本;调小可能导致任务提前中断"),
    ).toBeInTheDocument();
  });

  it("renders no '已自定义' tag and no reset button when isDefault is true", () => {
    render(
      <FieldRow
        fieldId="workflow.max_iterations"
        label="最大迭代次数"
        brief="限制单次运行的最大步数"
        isDefault
        onReset={vi.fn()}
      >
        <input />
      </FieldRow>,
    );

    expect(
      screen.queryByTestId("field-customized-workflow.max_iterations"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("field-reset-workflow.max_iterations"),
    ).not.toBeInTheDocument();
  });

  it("renders the '已自定义' tag and a reset button when isDefault is false, and clicking it calls onReset", async () => {
    const user = userEvent.setup();
    const onReset = vi.fn();
    render(
      <FieldRow
        fieldId="workflow.max_iterations"
        label="最大迭代次数"
        brief="限制单次运行的最大步数"
        isDefault={false}
        onReset={onReset}
      >
        <input />
      </FieldRow>,
    );

    expect(
      screen.getByTestId("field-customized-workflow.max_iterations"),
    ).toBeInTheDocument();
    const resetButton = screen.getByTestId(
      "field-reset-workflow.max_iterations",
    );
    expect(resetButton).toBeInTheDocument();

    await user.click(resetButton);

    expect(onReset).toHaveBeenCalledTimes(1);
  });

  it("renders the '已自定义' tag with no reset button when isDefault is false but onReset is omitted", () => {
    render(
      <FieldRow
        fieldId="workflow.max_iterations"
        label="最大迭代次数"
        brief="限制单次运行的最大步数"
        isDefault={false}
      >
        <input />
      </FieldRow>,
    );

    expect(
      screen.getByTestId("field-customized-workflow.max_iterations"),
    ).toBeInTheDocument();
    expect(
      screen.queryByTestId("field-reset-workflow.max_iterations"),
    ).not.toBeInTheDocument();
  });

  it("wraps the reset button in a tooltip carrying resetHint when given", async () => {
    const user = userEvent.setup();
    render(
      <FieldRow
        fieldId="workflow.max_iterations"
        label="最大迭代次数"
        brief="限制单次运行的最大步数"
        isDefault={false}
        onReset={vi.fn()}
        resetHint="30"
      >
        <input />
      </FieldRow>,
    );

    const resetButton = screen.getByTestId(
      "field-reset-workflow.max_iterations",
    );
    await user.hover(resetButton);

    expect(await screen.findByText("Reset to default: 30")).toBeInTheDocument();
  });
  it("centers the row's items by default; align=\"start\" top-aligns them for a tall control", () => {
    const { container, rerender } = render(
      <FieldRow fieldId="a.b" label="L" brief="B" isDefault>
        <input />
      </FieldRow>,
    );
    const row = (): HTMLElement => container.querySelector('[data-field-id="a.b"]') as HTMLElement;
    expect(row().style.alignItems).toBe("center");
    rerender(
      <FieldRow fieldId="a.b" label="L" brief="B" isDefault align="start">
        <input />
      </FieldRow>,
    );
    expect(row().style.alignItems).toBe("flex-start");
  });
});
