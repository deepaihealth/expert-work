/**
 * Composer — the input box + send/attach/stop controls (调试台重设计 PR-A
 * Task 7). Lifted verbatim out of ``PlaygroundTab.tsx`` (see
 * ``playground-input`` / ``playground-run`` / ``playground-attach`` /
 * ``playground-attach-doc`` / ``playground-stop`` testids there), plus NEW
 * ruling-R5 behaviour: Enter sends, Shift+Enter inserts a newline, and Enter
 * during IME composition does nothing.
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../../i18n";

import { Composer, type ComposerProps } from "../Composer";

const base: ComposerProps = {
  value: "",
  onChange: vi.fn(),
  onSend: vi.fn(),
  onStop: vi.fn(),
  running: false,
  uploading: false,
  readOnly: false,
  missingVariables: [],
  onAttachImage: vi.fn(),
  onAttachDocument: vi.fn(),
};

describe("Composer", () => {
  it("Enter sends, Shift+Enter inserts a newline, Enter during IME composition does nothing", () => {
    const onSend = vi.fn();
    render(<Composer {...base} value="hi" onSend={onSend} />);
    const ta = screen.getByTestId("playground-input");
    fireEvent.keyDown(ta, { key: "Enter", shiftKey: true });
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.keyDown(ta, { key: "Enter", isComposing: true }); // fireEvent 把 isComposing 放进 nativeEvent
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSend).toHaveBeenCalledTimes(1);
  });

  it("send is disabled with a required variable missing and the tooltip names it", async () => {
    render(<Composer {...base} value="hi" missingVariables={["customer_code"]} />);
    const btn = screen.getByTestId("playground-run");
    expect(btn).toBeDisabled();
    await userEvent.hover(btn.parentElement ?? btn);
    expect(await screen.findByText(/customer_code/)).toBeInTheDocument();
  });

  it("send disabled on empty input; enabled with text; readOnly disables send/attach", () => {
    const { rerender } = render(<Composer {...base} value="" />);
    expect(screen.getByTestId("playground-run")).toBeDisabled();
    rerender(<Composer {...base} value="x" />);
    expect(screen.getByTestId("playground-run")).toBeEnabled();
    rerender(<Composer {...base} value="x" readOnly />);
    expect(screen.getByTestId("playground-run")).toBeDisabled();
    expect(screen.getByTestId("playground-attach")).toBeDisabled();
  });

  it("shows the stop button only while running, and calls onStop", async () => {
    const onStop = vi.fn();
    const { rerender } = render(<Composer {...base} value="hi" onStop={onStop} />);
    expect(screen.queryByTestId("playground-stop")).not.toBeInTheDocument();
    rerender(<Composer {...base} value="hi" running onStop={onStop} />);
    const stopBtn = screen.getByTestId("playground-stop");
    await userEvent.click(stopBtn);
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("calls onAttachImage / onAttachDocument from their buttons", async () => {
    const onAttachImage = vi.fn();
    const onAttachDocument = vi.fn();
    render(
      <Composer
        {...base}
        value="hi"
        onAttachImage={onAttachImage}
        onAttachDocument={onAttachDocument}
      />,
    );
    await userEvent.click(screen.getByTestId("playground-attach"));
    await userEvent.click(screen.getByTestId("playground-attach-doc"));
    expect(onAttachImage).toHaveBeenCalledTimes(1);
    expect(onAttachDocument).toHaveBeenCalledTimes(1);
  });
});
