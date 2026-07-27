/**
 * Pre-first-token breakdown bar — 一期 Task 6。
 *
 * Sits above the waterfall and answers the one question the waterfall makes
 * you hunt for: where did the time before the first token go? Clicking a
 * segment selects the matching span in the tree below (shared `selectedId`).
 */
import { Typography } from "antd";
import { useTranslation } from "react-i18next";

import type { TraceSpan } from "../../../api/trace_facade";
import { buildBreakdown } from "./entry_breakdown";
import { fmtDuration } from "./duration_format";

/** Below this share of the bar a segment shows colour only — its label would
 *  overflow into its neighbours. */
const LABEL_MIN_SHARE = 0.06;

// Same 62%-over-transparent blend TraceView.tsx's kindBarColor uses for the
// waterfall's own entry-chain bars (so the breakdown and the waterfall read
// as the same colour) — and, unlike a flat opaque fill, it composites with
// whatever surface sits behind it instead of imposing one fixed background.
// A solid `--ew-trace-entry` fill paired with hardcoded white text failed
// dark-theme contrast (indigo-300 is light — 1.99:1 against white, WCAG AA
// wants ≥3:1 for UI text). Pairing the translucent fill with
// `--ew-text-primary` (also theme-aware) fixes it in both directions instead
// of just swapping which theme breaks.
const ENTRY_BG = "color-mix(in srgb, var(--ew-trace-entry, #7c8cff) 62%, transparent)";

interface EntryBreakdownProps {
  spans: readonly TraceSpan[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}

export function EntryBreakdown({ spans, selectedId, onSelect }: EntryBreakdownProps) {
  const { t } = useTranslation();
  const segments = buildBreakdown(spans);
  if (segments.length === 0) return null;

  const total = segments.reduce((sum, s) => sum + s.latencyMs, 0);
  if (total <= 0) return null;

  return (
    <div style={{ marginBottom: 12 }} data-testid="entry-breakdown">
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {t("trace.breakdown_title", { d: fmtDuration(total) })}
      </Typography.Text>
      <div style={{ display: "flex", gap: 2, marginTop: 4, height: 22 }}>
        {segments.map((s) => {
          const share = s.latencyMs / total;
          return (
            <button
              key={s.id}
              type="button"
              onClick={() => onSelect(s.id)}
              title={`${s.label} · ${fmtDuration(s.latencyMs)}`}
              style={{
                flex: `${share} 1 0`,
                minWidth: 4,
                border: selectedId === s.id ? "1px solid var(--ew-text-primary)" : "none",
                borderRadius: 3,
                background: ENTRY_BG,
                color: "var(--ew-text-primary)",
                fontSize: 11,
                overflow: "hidden",
                whiteSpace: "nowrap",
                cursor: "pointer",
                padding: 0,
              }}
            >
              {share >= LABEL_MIN_SHARE ? `${s.label} ${fmtDuration(s.latencyMs)}` : ""}
            </button>
          );
        })}
      </div>
    </div>
  );
}
