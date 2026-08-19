/**
 * useVirtualRows — a fixed-row-height virtualization window over the
 * ledger table's scroll container: which record indexes to actually
 * render (``[start, end)``), plus the two spacer heights that keep the
 * scrollbar honest. Listens for ``scroll`` and container resize (falls
 * back to ``window``'s ``resize`` when ``ResizeObserver`` isn't
 * available); an unmeasured/zero-height container renders everything.
 * 虚拟化模型参照 deepseek-harness ui-trajectory(MIT)重写. See
 * .superpowers/sdd/2026-08-19-debug-console-pr-a2-trajectory/task-4-brief.md.
 */
import { useEffect, useMemo, useState, type RefObject } from "react";

export interface VirtualWindow {
  start: number;
  end: number;
  topPad: number;
  bottomPad: number;
}

const DEFAULT_OVERSCAN = 12;

function windowOf(
  scrollTop: number,
  clientHeight: number,
  count: number,
  rowHeight: number,
  overscan: number,
): { start: number; end: number } {
  if (clientHeight === 0) return { start: 0, end: count };
  const rawStart = Math.floor(scrollTop / rowHeight) - overscan;
  const rawEnd = Math.ceil((scrollTop + clientHeight) / rowHeight) + overscan;
  const start = Math.min(Math.max(rawStart, 0), count);
  const end = Math.min(Math.max(rawEnd, start), count);
  return { start, end };
}

export function useVirtualRows(args: {
  scrollRef: RefObject<HTMLElement | null>;
  count: number;
  rowHeight: number;
  overscan?: number;
}): VirtualWindow {
  const { scrollRef, count, rowHeight, overscan = DEFAULT_OVERSCAN } = args;
  // A pure render trigger — scroll/resize just need to force a re-render
  // so the window below is recomputed off the live DOM geometry; there's
  // no separate "geometry state" to keep in sync with `count`.
  const [, setTick] = useState(0);

  // Read *now*, at render time — not inside the effect. `scrollRef` (the
  // RefObject itself) never changes identity, so depending on it (as an
  // earlier version of this hook did) attaches listeners once at mount and
  // never again: a container that mounts a frame late (e.g. behind a
  // loading state) is never noticed, and the hook silently stops updating
  // except when some unrelated prop happens to change. `el` captured here
  // and used as the effect's dependency instead makes React's own
  // dependency comparison do the "did the attached element change"
  // bookkeeping — the effect (re)runs, tearing down the previous
  // listeners and attaching fresh ones, exactly when `scrollRef.current`
  // differs from what it was on the last render.
  const el = scrollRef.current;

  useEffect(() => {
    if (!el) return undefined;
    const onChange = () => setTick((n) => n + 1);
    el.addEventListener("scroll", onChange, { passive: true });
    let observer: ResizeObserver | null = null;
    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(onChange);
      observer.observe(el);
    } else {
      window.addEventListener("resize", onChange);
    }
    return () => {
      el.removeEventListener("scroll", onChange);
      if (observer) observer.disconnect();
      else window.removeEventListener("resize", onChange);
    };
  }, [el]);

  const { start, end } = windowOf(el?.scrollTop ?? 0, el?.clientHeight ?? 0, count, rowHeight, overscan);

  return useMemo(
    () => ({ start, end, topPad: start * rowHeight, bottomPad: Math.max(count - end, 0) * rowHeight }),
    [start, end, count, rowHeight],
  );
}
