/**
 * EntryBreakdown contrast regression — Task 6 follow-up.
 *
 * The breakdown bar's segments render as
 * `color-mix(in srgb, var(--ew-trace-entry) 62%, transparent)` behind
 * `var(--ew-text-primary)` text (EntryBreakdown.tsx). An earlier version used
 * an opaque `--ew-trace-entry` fill with hardcoded white text, which was
 * unreadable in the dark theme (indigo-300 is a *light* colour — white text
 * on it measured 1.99:1, WCAG AA wants ≥3:1 for UI text). This test reads
 * the live values out of tokens.css (not a copy-pasted snapshot of them) so
 * it actually breaks if a future token edit reopens that gap, instead of
 * silently going stale next to the CSS it's meant to guard.
 *
 * Not a full axe-core DOM run: vitest.config.ts sets `css: false`, so jsdom
 * never sees tokens.css's `var()`/`color-mix()` rules and getComputedStyle
 * can't resolve them either way — a real DOM check would need synthetic
 * pixel colours injected regardless. Implementing the same WCAG relative-
 * luminance formula axe-core uses (and that this repo's reviewer
 * cross-checked with axe-core's own `commons.color.getContrast` byte-for-
 * byte against the *old* code — 1.99 / 7.90, matching) is the more honest
 * signal here: it tests tokens.css's actual values, not a mock of them.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const TOKENS_CSS_PATH = join(__dirname, "../../../../theme/tokens.css");
const ALPHA = 0.62; // matches EntryBreakdown.tsx's ENTRY_BG and TraceView.tsx's kindBarColor

type Rgb = [number, number, number];

function hexToRgb(hex: string): Rgb {
  const n = parseInt(hex.replace("#", ""), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

/** Parse every literal `--ew-color-*: #hex;` primitive in the file — these
 *  are theme-invariant, so a single global pass covers both theme blocks. */
function parsePrimitives(css: string): Map<string, string> {
  const map = new Map<string, string>();
  for (const m of css.matchAll(/(--ew-color-[\w-]+):\s*(#[0-9a-fA-F]{6});/g)) {
    map.set(m[1], m[2]);
  }
  return map;
}

/** Resolve `--ew-trace-entry` / `--ew-surface-base` / `--ew-text-primary`
 *  within one theme block (each aliases a primitive via a single `var()`
 *  hop in this file — see tokens.css's "语义层" section). */
function resolveSemanticVar(themeBlock: string, name: string, primitives: Map<string, string>): Rgb {
  const aliasMatch = themeBlock.match(new RegExp(`${name}:\\s*var\\((--ew-color-[\\w-]+)\\)`));
  if (!aliasMatch) throw new Error(`${name} not found (as a var() alias) in theme block`);
  const hex = primitives.get(aliasMatch[1]);
  if (!hex) throw new Error(`${aliasMatch[1]} (aliased by ${name}) not found in primitives`);
  return hexToRgb(hex);
}

/** WCAG 2.x relative luminance — https://www.w3.org/TR/WCAG21/#dfn-relative-luminance */
function relativeLuminance([r, g, b]: Rgb): number {
  const linear = (c: number): number => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
}

function contrastRatio(a: Rgb, b: Rgb): number {
  const [l1, l2] = [relativeLuminance(a), relativeLuminance(b)].sort((x, y) => y - x);
  return (l1 + 0.05) / (l2 + 0.05);
}

/** `color-mix(in srgb, fg <alpha*100>%, transparent)` composited over an
 *  opaque backdrop — sRGB channels blend linearly by alpha (no gamma step;
 *  that's what `color-mix(in srgb, …)` itself does). */
function compositeOverBackdrop(fg: Rgb, backdrop: Rgb, alpha: number): Rgb {
  return fg.map((c, i) => Math.round(c * alpha + backdrop[i] * (1 - alpha))) as Rgb;
}

function themeBlock(css: string, theme: "dark" | "light"): string {
  const darkStart = css.indexOf("/* Dark theme(default) */");
  const lightStart = css.indexOf("/* Light theme */");
  const reducedMotionStart = css.indexOf("10. prefers-reduced-motion");
  if (darkStart === -1 || lightStart === -1 || reducedMotionStart === -1) {
    throw new Error("tokens.css theme block markers moved — update this test's anchors");
  }
  return theme === "dark" ? css.slice(darkStart, lightStart) : css.slice(lightStart, reducedMotionStart);
}

describe("EntryBreakdown segment contrast (tokens.css, both themes)", () => {
  const css = readFileSync(TOKENS_CSS_PATH, "utf-8");
  const primitives = parsePrimitives(css);

  it.each([
    ["dark", 3] as const,
    ["light", 3] as const,
  ])("%s theme: composited entry-segment background vs --ew-text-primary clears %d:1", (theme, minRatio) => {
    const block = themeBlock(css, theme);
    const entry = resolveSemanticVar(block, "--ew-trace-entry", primitives);
    const surfaceBase = resolveSemanticVar(block, "--ew-surface-base", primitives);
    const textPrimary = resolveSemanticVar(block, "--ew-text-primary", primitives);

    const composited = compositeOverBackdrop(entry, surfaceBase, ALPHA);
    const ratio = contrastRatio(composited, textPrimary);

    expect(ratio).toBeGreaterThanOrEqual(minRatio);
  });
});
