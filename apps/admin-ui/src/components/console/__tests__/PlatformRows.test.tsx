/** B-73 ② —— 平台自动生成的隐藏行在审计视图里可见,且不被说成用户说的话。 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PlatformRows } from "../PlatformRows";

describe("PlatformRows", () => {
  it("renders nothing when there are no hidden rows", () => {
    // 同租户视图恒空 —— 不能平白多出一段折叠块。
    const { container } = render(<PlatformRows lines={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders each hidden row verbatim under a platform-generated tag", () => {
    const block = "[本轮输入]（平台自动生成）\n- org_logo：文件 $EXPERT_WORK_INPUTS_DIR/org_logo.png";
    render(<PlatformRows lines={[block, "<recovery-advisory>…</recovery-advisory>"]} />);
    expect(screen.getByTestId("platform-rows")).toBeInTheDocument();
    const rows = screen.getAllByTestId("platform-row");
    expect(rows).toHaveLength(2);
    // 原文,不截断 —— 审计要的就是忠实。
    expect(rows[0]).toHaveTextContent("EXPERT_WORK_INPUTS_DIR/org_logo.png");
    expect(rows[1]).toHaveTextContent("recovery-advisory");
  });

  it("is collapsed by default", () => {
    // 它进模型但不是对话内容:默认不占版面,展开才看。
    render(<PlatformRows lines={["x"]} />);
    expect(screen.getByTestId("platform-rows")).not.toHaveAttribute("open");
  });
});
