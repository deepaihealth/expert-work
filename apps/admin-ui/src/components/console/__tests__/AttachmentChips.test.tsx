/**
 * AttachmentChips — closable attachment tags (调试台重设计 PR-A Task 7).
 * Lifted verbatim out of ``PlaygroundTab.tsx`` (see ``playground-attachments``
 * / ``playground-attachment`` testids there).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import i18n from "../../../i18n";

import { AttachmentChips } from "../AttachmentChips";

describe("AttachmentChips", () => {
  // Locale-sensitive assertion below (the "移除附件" aria-label) — pin zh-CN
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

  it("renders a closable tag per attachment and calls onRemove with its id", async () => {
    const onRemove = vi.fn();
    render(
      <AttachmentChips
        attachments={[{ id: "image:a", name: "a.png", kind: "image", value: "a" }]}
        onRemove={onRemove}
      />,
    );
    await userEvent.click(screen.getByLabelText("移除附件"));
    expect(onRemove).toHaveBeenCalledWith("image:a");
  });

  it("renders nothing when there are no attachments", () => {
    const { container } = render(<AttachmentChips attachments={[]} onRemove={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });
});
