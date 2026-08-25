/** TurnPlanCard — 轮内计划快照卡(BUG-13 修订:计划是轮级产物)。 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ThreadPlan } from "../../../api/plan";
import { planProgress } from "../PlanStepList";
import { TurnPlanCard } from "../TurnPlanCard";

const PLAN: ThreadPlan = {
  goal: "为浩然2生成1周健康管理方案",
  steps: [
    { id: "1", description: "拉取客户档案", status: "completed" },
    { id: "2", description: "写方案JSON并登记", status: "in_progress" },
    { id: "3", description: "渲染 pptx", status: "pending" },
  ],
};

describe("TurnPlanCard", () => {
  it("renders nothing when the turn produced no plan", () => {
    const { container } = render(<TurnPlanCard plan={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders the goal, every step and the per-status progress", () => {
    render(<TurnPlanCard plan={PLAN} />);
    expect(screen.getByTestId("turn-plan-card")).toBeInTheDocument();
    expect(screen.getByText("为浩然2生成1周健康管理方案")).toBeInTheDocument();
    expect(screen.getByText("拉取客户档案")).toBeInTheDocument();
    expect(screen.getByText("渲染 pptx")).toBeInTheDocument();
    // 进度值本身由 planProgress 的单测钉住(此处 i18n 只回 key,断不了文本)。
    expect(screen.getByTestId("turn-plan-progress")).toBeInTheDocument();
  });

  it("counts progress per status", () => {
    expect(planProgress(PLAN)).toEqual({ completed: 1, inProgress: 1, pending: 1 });
    expect(planProgress({ goal: "g", steps: [] })).toEqual({
      completed: 0,
      inProgress: 0,
      pending: 0,
    });
  });

  it("carries no edit affordance — a past turn's snapshot is not editable", () => {
    render(<TurnPlanCard plan={PLAN} />);
    expect(screen.queryByTestId("plan-edit")).not.toBeInTheDocument();
  });

  it("does not reuse the session-level card's testid or DOM id", () => {
    const { container } = render(<TurnPlanCard plan={PLAN} />);
    // 与会话级 PlanCard 同时出现在一页时,重复 testid 会让查询报「多个匹配」,
    // 重复 id 会让 aria-controls 指向歧义(axe 违规)。
    expect(screen.queryByTestId("console-plan-card")).not.toBeInTheDocument();
    expect(container.querySelector("#console-plan-body")).toBeNull();
  });
});
