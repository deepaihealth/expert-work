/**
 * RowDetailSystem — the SYSTEM row's 「原文」panel(PR-A.3 §十.1 Task 12):
 * the run's `system_prompt` frame text, verbatim, with a copy button.
 * `<pre>` styling copied from `RowDetailPayloadResult.tsx:89-103`'s private
 * `Pre` (not imported — that component isn't exported).
 */
import { CopyButton } from "../CopyButton";

export function SystemPromptPanel({ text }: { text: string }) {
  return (
    <div data-testid="console-detail-system-prompt" style={{ position: "relative" }}>
      <span style={{ position: "absolute", right: 0, top: -4 }}>
        <CopyButton text={text} testId="console-detail-system-copy" />
      </span>
      <pre
        style={{
          margin: 0,
          fontSize: 11.5,
          fontFamily: "var(--ew-font-mono)",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          color: "var(--ew-text-secondary)",
        }}
      >
        {text}
      </pre>
    </div>
  );
}
