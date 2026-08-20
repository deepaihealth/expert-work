/**
 * CommentarySegmentLine — the de-emphasised icon + secondary-text + clamp
 * rendering shared by the live answer block, the historical-turn fallback
 * branch, and the degraded flat-message views (spec 2026-07-30,
 * Important#1/Minor#3/Minor#5: keeps every rendering site byte-identical
 * instead of hand-copied).
 *
 * Lifted verbatim out of ``components/turn/TurnCard.tsx`` (调试台重设计 PR-B
 * Task 1) — TurnCard.tsx keeps re-exporting this name so existing importers
 * don't need to change until they migrate. No logic changed by the move.
 */
import { Typography } from "antd";
import { MessageSquareText } from "lucide-react";

const { Text } = Typography;

/** Minor#5 — the commentary clamp length, previously a repeated magic
 *  number (240) at each render site. */
const COMMENTARY_CLAMP_CHARS = 240;

export function CommentarySegmentLine({
  text,
  label,
}: {
  text: string;
  label: string;
}) {
  return (
    <div
      style={{ display: "flex", gap: 6, alignItems: "flex-start", marginBottom: 6 }}
      data-testid="turn-segment-commentary"
    >
      <MessageSquareText
        size={12}
        role="img"
        style={{ marginTop: 3, flexShrink: 0, color: "var(--ew-text-tertiary)" }}
        aria-label={label}
      />
      <Text type="secondary" style={{ fontSize: 12, whiteSpace: "pre-wrap" }}>
        {text.length > COMMENTARY_CLAMP_CHARS
          ? `${text.slice(0, COMMENTARY_CLAMP_CHARS)}…`
          : text}
      </Text>
    </div>
  );
}
