/**
 * useVirtualRows — fixed-row-height windowing driven by a scroll
 * container's live geometry (``scrollTop`` / ``clientHeight``, both mocked
 * here since jsdom never lays anything out).
 */
import { describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import type { RefObject } from "react";

import { useVirtualRows } from "../use_virtual_rows";

function mockEl(clientHeight: number, scrollTop = 0): HTMLElement {
  const el = document.createElement("div");
  Object.defineProperty(el, "clientHeight", { value: clientHeight, configurable: true });
  Object.defineProperty(el, "scrollTop", { value: scrollTop, writable: true, configurable: true });
  return el;
}

function refOf(el: HTMLElement | null): RefObject<HTMLElement | null> {
  return { current: el };
}

describe("useVirtualRows", () => {
  it("clientHeight 0 → whole range", () => {
    // `count` is deliberately large relative to the default overscan (12):
    // if the zero-height case fell through to the regular
    // scrollTop/overscan math instead of a dedicated "render everything"
    // branch, it would clamp to a small window (≈ overscan-sized), not the
    // full 1000 — so this genuinely pins the special case, not a
    // coincidental overlap with it.
    // Unmounted (ref never attached) …
    const { result: unmounted } = renderHook(() =>
      useVirtualRows({ scrollRef: refOf(null), count: 1000, rowHeight: 20 }),
    );
    expect(unmounted.current).toEqual({ start: 0, end: 1000, topPad: 0, bottomPad: 0 });

    // … and attached but not yet laid out (jsdom's default clientHeight),
    // even scrolled deep into the (notional) content, both render
    // everything rather than an empty/partial window.
    const { result: zeroHeight } = renderHook(() =>
      useVirtualRows({ scrollRef: refOf(mockEl(0, 5000)), count: 1000, rowHeight: 20 }),
    );
    expect(zeroHeight.current).toEqual({ start: 0, end: 1000, topPad: 0, bottomPad: 0 });
  });

  it("windows by scrollTop with overscan and pads", () => {
    const el = mockEl(100, 205);
    const { result } = renderHook(() =>
      useVirtualRows({ scrollRef: refOf(el), count: 50, rowHeight: 20, overscan: 2 }),
    );

    // start = floor(205/20) - 2 = 8; end = ceil((205+100)/20) + 2 = 18.
    expect(result.current).toEqual({ start: 8, end: 18, topPad: 160, bottomPad: 640 });

    // Omitting `overscan` falls back to the default of 12.
    const { result: withDefault } = renderHook(() =>
      useVirtualRows({ scrollRef: refOf(el), count: 50, rowHeight: 20 }),
    );
    // start = floor(205/20) - 12 = -2 → clamped to 0; end = ceil(305/20) + 12 = 28.
    expect(withDefault.current).toEqual({ start: 0, end: 28, topPad: 0, bottomPad: 440 });
  });

  it("count shrink clamps the window", () => {
    const el = mockEl(100, 205);
    const { result, rerender } = renderHook(
      (props: { count: number }) => useVirtualRows({ scrollRef: refOf(el), count: props.count, rowHeight: 20, overscan: 2 }),
      { initialProps: { count: 50 } },
    );
    expect(result.current).toEqual({ start: 8, end: 18, topPad: 160, bottomPad: 640 });

    rerender({ count: 10 });
    // Unclamped end (18) would run past the new count — both bounds must
    // land inside [0, 10].
    expect(result.current).toEqual({ start: 8, end: 10, topPad: 160, bottomPad: 0 });
  });

  it("scroll event updates the window", async () => {
    const el = mockEl(100, 0);
    const ref = refOf(el);
    const { result } = renderHook(() => useVirtualRows({ scrollRef: ref, count: 100, rowHeight: 20, overscan: 0 }));

    expect(result.current).toEqual({ start: 0, end: 5, topPad: 0, bottomPad: 1900 });

    el.scrollTop = 200;
    await act(async () => {
      el.dispatchEvent(new Event("scroll"));
    });

    // start = floor(200/20) = 10; end = ceil(300/20) = 15.
    expect(result.current).toEqual({ start: 10, end: 15, topPad: 200, bottomPad: 1700 });
  });

  it("scrollRef.current attaching after a rerender (e.g. the container mounts behind a loading state) still gets scroll listeners", async () => {
    // One stable ref object — never replaced, only `.current` mutates —
    // matching a real `useRef` passed down as a prop. The container starts
    // unattached (`current: null`, as it would behind a loading state).
    const ref = refOf(null);
    const { result, rerender } = renderHook(() =>
      useVirtualRows({ scrollRef: ref, count: 100, rowHeight: 20, overscan: 0 }),
    );
    expect(result.current).toEqual({ start: 0, end: 100, topPad: 0, bottomPad: 0 });

    // The container mounts a frame later: same ref object, `.current` now
    // points at the element. A prior version of this hook depended on the
    // `RefObject` itself (stable, so the effect never re-ran) and so never
    // noticed this — the scroll listener was simply never attached.
    const el = mockEl(100, 0);
    ref.current = el;
    rerender();

    // The render itself already reflects the newly attached element's
    // geometry (computed straight from `scrollRef.current`, not cached
    // state) …
    expect(result.current).toEqual({ start: 0, end: 5, topPad: 0, bottomPad: 1900 });

    // … and, the real regression check, its `scroll` events are now heard.
    el.scrollTop = 200;
    await act(async () => {
      el.dispatchEvent(new Event("scroll"));
    });
    // start = floor(200/20) = 10; end = ceil(300/20) = 15.
    expect(result.current).toEqual({ start: 10, end: 15, topPad: 200, bottomPad: 1700 });
  });

  it("unmount removes the scroll listener and disconnects the observer exactly once", () => {
    const el = mockEl(100, 0);
    const removeSpy = vi.spyOn(el, "removeEventListener");
    const disconnectSpy = vi.spyOn(ResizeObserver.prototype, "disconnect");

    const { unmount } = renderHook(() =>
      useVirtualRows({ scrollRef: refOf(el), count: 100, rowHeight: 20 }),
    );
    expect(removeSpy).not.toHaveBeenCalled();
    expect(disconnectSpy).not.toHaveBeenCalled();

    unmount();

    expect(removeSpy).toHaveBeenCalledTimes(1);
    expect(disconnectSpy).toHaveBeenCalledTimes(1);
  });
});
