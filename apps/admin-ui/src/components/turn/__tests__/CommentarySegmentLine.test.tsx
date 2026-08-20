/**
 * CommentarySegmentLine — split out of ``TurnCard.tsx`` (调试台重设计 PR-B
 * Task 1). Covers the >240-char clamp and, paired with ``FullTextTrigger``/
 * ``FullTextModal`` the way every call site (``TurnCard``'s fallback branch,
 * ``AnswerBubble``, ``Transcript``) wires them, that the clamp never loses
 * the reader's only way back to the full text.
 *
 * The clamp+full-text assertion below is moved verbatim from
 * ``TurnCard.test.tsx``'s (now-deleted) "fallback commentary line clamps to
 * 240 chars but its FullTextTrigger opens the full text" it — that
 * TurnCard-level integration test rendered the whole card just to reach
 * this pairing; here it's exercised directly against the three exported
 * pieces every call site actually composes.
 */
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { App } from "antd";
import { fireEvent, render, screen } from "@testing-library/react";
import "../../../i18n";

import { CommentarySegmentLine } from "../CommentarySegmentLine";
import { FullTextModal, FullTextTrigger, type FullTextState } from "../FullTextModal";

/** The exact pairing every call site uses: a clamped commentary line plus
 *  the 「查看全文」 trigger + modal that opens the unclamped text. */
function CommentaryWithFullText({ text }: { text: string }) {
  const [fullText, setFullText] = useState<FullTextState | null>(null);
  return (
    <App>
      <CommentarySegmentLine text={text} label="commentary" />
      <FullTextTrigger onClick={() => setFullText({ title: "全文", text })} />
      <FullTextModal state={fullText} onClose={() => setFullText(null)} />
    </App>
  );
}

describe("CommentarySegmentLine", () => {
  it("renders the text unclamped when at or under the 240-char limit", () => {
    const shortText = "短旁白";
    render(<CommentarySegmentLine text={shortText} label="commentary" />);
    expect(screen.getByTestId("turn-segment-commentary")).toHaveTextContent(shortText);
  });

  // Important#1 — a commentary line clamps to 240 chars with no other way
  // to read the rest; its paired FullTextTrigger must open the modal with
  // the complete, unclamped text.
  it("clamps to 240 chars but its FullTextTrigger opens the full text", () => {
    const longText = "旁白内容".repeat(80); // 320 chars, well past the 240 clamp
    render(<CommentaryWithFullText text={longText} />);

    const commentary = screen.getByTestId("turn-segment-commentary");
    expect(commentary).toHaveTextContent(`${longText.slice(0, 240)}…`);
    expect(commentary.textContent?.length ?? 0).toBeLessThan(longText.length);

    fireEvent.click(screen.getByTestId("full-text-trigger"));
    expect(screen.getByTestId("full-text-modal")).toHaveTextContent(longText);
  });
});
