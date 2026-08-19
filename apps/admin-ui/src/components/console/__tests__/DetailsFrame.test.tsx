/**
 * DetailsFrame 测试(PR-A.2 Task 9)—— 可拖宽壳:手柄拖动 clamp、双击复位、
 * 键盘步进、tab 条与关闭。壳是受控的(width 从 prop 来,回调往上抛),所以
 * 每条断言看的都是 `onWidthChange` 收到什么。
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import "../../../i18n";

import {
  DETAILS_MIN_WIDTH,
  DETAILS_RESIZE_STEP,
  DetailsFrame,
  type DetailsFrameProps,
} from "../DetailsFrame";

// jsdom 25 没有 `PointerEvent`,testing-library 会退回基类 `Event` —— 那会把
// `clientX` 悄悄丢掉,拖动就永远是 0 像素。`MouseEvent` 派生的替身保住坐标
// (同 LaneStrip.test.tsx 的做法);`setPointerCapture` 同样缺席,组件里按可选
// 调用写。
class PointerEventPolyfill extends MouseEvent {
  readonly pointerId: number;
  constructor(type: string, init: PointerEventInit = {}) {
    super(type, init);
    this.pointerId = init.pointerId ?? 1;
  }
}
if (!("PointerEvent" in window)) {
  (window as unknown as { PointerEvent: unknown }).PointerEvent = PointerEventPolyfill;
}

const TABS = [
  { key: "summary", label: "概要", testId: "console-detail-tab-summary" },
  { key: "payload", label: "载荷", testId: "console-detail-tab-payload" },
];

function renderFrame(over: Partial<DetailsFrameProps> = {}): DetailsFrameProps {
  const props: DetailsFrameProps = {
    width: 420,
    onWidthChange: vi.fn(),
    splitWidth: 1200,
    header: <span data-testid="frame-header">头部</span>,
    tabs: TABS,
    activeTab: "summary",
    onTabChange: vi.fn(),
    onClose: vi.fn(),
    children: <div data-testid="frame-body">正文</div>,
    ...over,
  };
  render(<DetailsFrame {...props} />);
  return props;
}

describe("DetailsFrame", () => {
  it("拖动手柄:宽度按 min(720, splitWidth - 280) 收口", () => {
    const props = renderFrame({ width: 420, splitWidth: 1200 });
    const handle = screen.getByTestId("console-detail-resize");
    // 往左拖 400px → 420 + 400 = 820,上限 min(720, 1200-280=920) = 720。
    fireEvent.pointerDown(handle, { clientX: 800, pointerId: 1, button: 0 });
    fireEvent.pointerMove(handle, { clientX: 400, pointerId: 1 });
    expect(props.onWidthChange).toHaveBeenCalledWith(720);
  });

  it("拖动手柄:窄容器下上限退成 splitWidth - 280", () => {
    const props = renderFrame({ width: 420, splitWidth: 800 });
    const handle = screen.getByTestId("console-detail-resize");
    fireEvent.pointerDown(handle, { clientX: 800, pointerId: 1, button: 0 });
    fireEvent.pointerMove(handle, { clientX: 400, pointerId: 1 });
    expect(props.onWidthChange).toHaveBeenCalledWith(520);
  });

  it("拖动手柄:不低于 320 下限", () => {
    const props = renderFrame({ width: 420, splitWidth: 1200 });
    const handle = screen.getByTestId("console-detail-resize");
    // 往右拖 200px → 420 - 200 = 220,夹到 320。
    fireEvent.pointerDown(handle, { clientX: 800, pointerId: 1, button: 0 });
    fireEvent.pointerMove(handle, { clientX: 1000, pointerId: 1 });
    expect(props.onWidthChange).toHaveBeenCalledWith(DETAILS_MIN_WIDTH);
  });

  it("双击手柄 → onWidthChange(null)(复位默认)", () => {
    const props = renderFrame({ width: 600 });
    fireEvent.doubleClick(screen.getByTestId("console-detail-resize"));
    expect(props.onWidthChange).toHaveBeenCalledWith(null);
  });

  it("手柄键盘步进:ArrowLeft 加宽 16、ArrowRight 收窄 16", () => {
    const props = renderFrame({ width: 420, splitWidth: 1200 });
    const handle = screen.getByTestId("console-detail-resize");
    fireEvent.keyDown(handle, { key: "ArrowLeft" });
    expect(props.onWidthChange).toHaveBeenLastCalledWith(420 + DETAILS_RESIZE_STEP);
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(props.onWidthChange).toHaveBeenLastCalledWith(420 - DETAILS_RESIZE_STEP);
  });

  it("tab 条:aria-selected 跟 activeTab,点击抛 onTabChange;✕ 抛 onClose", () => {
    const props = renderFrame({ activeTab: "payload" });
    expect(screen.getByTestId("console-detail-tab-payload")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("console-detail-tab-summary")).toHaveAttribute("aria-selected", "false");
    expect(screen.getByTestId("frame-header")).toBeInTheDocument();
    expect(screen.getByTestId("frame-body")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("console-detail-tab-summary"));
    expect(props.onTabChange).toHaveBeenCalledWith("summary");
    fireEvent.click(screen.getByTestId("console-detail-close"));
    expect(props.onClose).toHaveBeenCalledTimes(1);
  });

  it("aside 用受控宽度渲染", () => {
    renderFrame({ width: 505 });
    expect(screen.getByTestId("console-detail-aside")).toHaveStyle({ width: "505px" });
  });
});
