/**
 * ReadonlyTooltip — Cross-tenant W3 (review I-2).
 *
 * The whole point of the wrapper is that a DISABLED control still gets an
 * explanatory tooltip: browsers don't dispatch mouse events on disabled form
 * controls, so hover must land on the wrapper span (child is
 * ``pointer-events: none``). jsdom can't reproduce the browser suppression
 * itself — the contract pinned here is the wrapper shape + hover-on-wrapper
 * opening the tooltip.
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Button } from "antd";
import "../../i18n";

import { ReadonlyTooltip } from "../ReadonlyTooltip";

const READONLY_TEXT =
  "Viewing another tenant (read-only) — switch back to your home tenant to make changes";

describe("ReadonlyTooltip", () => {
  it("on: hover 落在 wrapper 上,disabled 按钮也真出 tooltip", async () => {
    const user = userEvent.setup();
    render(
      <ReadonlyTooltip on>
        <Button disabled data-testid="target">
          act
        </Button>
      </ReadonlyTooltip>,
    );

    const wrapper = screen.getByTestId("readonly-tooltip");
    // child 容器 pointer-events:none — 鼠标命中的必然是 wrapper。
    expect(wrapper.firstElementChild).toHaveStyle({ pointerEvents: "none" });
    await user.hover(wrapper);

    expect(await screen.findByText(READONLY_TEXT)).toBeInTheDocument();
  });

  it("off: 原样渲染 children,无 wrapper 无 tooltip", () => {
    render(
      <ReadonlyTooltip on={false}>
        <Button data-testid="target">act</Button>
      </ReadonlyTooltip>,
    );

    expect(screen.getByTestId("target")).toBeEnabled();
    expect(screen.queryByTestId("readonly-tooltip")).not.toBeInTheDocument();
    expect(screen.queryByText(READONLY_TEXT)).not.toBeInTheDocument();
  });
});
