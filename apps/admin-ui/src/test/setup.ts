/**
 * Vitest setup — Stream H CI infra + H.1b PR 2a refresh.
 *
 * Loads ``@testing-library/jest-dom`` matchers and ships a minimal
 * ``matchMedia`` polyfill required by Antd 5's responsive observers
 * (jsdom doesn't implement the API by default).
 *
 * The axios stub adapter prevents the shared ``apiClient`` from ever
 * hitting the network during tests: every request resolves to a
 * generic ``success=false`` envelope, which ``unwrap()`` converts to an
 * :class:`ApiError`. AuthContext catches non-401 errors silently and
 * keeps its optimistic identity, so existing tests that seed a JWT
 * still observe the JWT-derived identity. Tests that need richer
 * fixtures can override the adapter per-file via ``apiClient.defaults
 * .adapter = …``.
 */
import "@testing-library/jest-dom/vitest";

import { apiClient } from "../api/client";

if (typeof window !== "undefined" && !window.matchMedia) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}

// jsdom 29 ships the AnimationEvent/TransitionEvent constructors AND
// cssstyle now recognizes the (Webkit-prefixed) transition/animation style
// properties — jsdom 25 had neither. rc-motion feature-detects motion
// support at module load (``'TransitionEvent' in window`` for the
// unprefixed event, ``'WebkitTransition' in style`` for the prefixed
// fallback); with either present it waits for transition/animation end
// events that jsdom never fires, so every antd motion hangs: Tree expand
// locks (rc-tree ignores further expands while a motion is pending, and
// antd's collapse motion has no deadline), stale tooltips pile up in the
// DOM. Strip both detection surfaces to restore the jsdom-25 behavior:
// motion unsupported → completes synchronously.
delete (window as { AnimationEvent?: unknown }).AnimationEvent;
delete (window as { TransitionEvent?: unknown }).TransitionEvent;
{
  const styleProto = Object.getPrototypeOf(
    document.createElement("div").style,
  ) as Record<string, unknown>;
  for (const prop of [
    "WebkitTransition",
    "MozTransition",
    "msTransition",
    "OTransition",
    "WebkitAnimation",
    "MozAnimation",
    "msAnimation",
    "OAnimation",
  ]) {
    delete styleProto[prop];
  }
}

if (typeof globalThis.ResizeObserver === "undefined") {
  class ResizeObserverPolyfill {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  globalThis.ResizeObserver =
    ResizeObserverPolyfill as unknown as typeof ResizeObserver;
}

// jsdom implements ``getComputedStyle(elt)`` but throws "Not implemented" on the
// two-arg pseudo-element form that antd's rc-util scrollbar measurement
// (Table / Modal scroll-lock) calls. That error is noisy and intermittently
// tips Table-heavy tests into failure. Delegate the pseudo form to the
// supported one so it never throws.
if (typeof window !== "undefined") {
  const realGetComputedStyle = window.getComputedStyle.bind(window);
  window.getComputedStyle = ((elt: Element, _pseudo?: string | null) =>
    realGetComputedStyle(elt)) as typeof window.getComputedStyle;
}

apiClient.defaults.adapter = (config) =>
  Promise.resolve({
    data: {
      success: false,
      data: null,
      error: { code: "TEST_STUB", message: "no network in tests" },
    },
    status: 200,
    statusText: "OK",
    headers: {},
    config,
    request: {},
  });
