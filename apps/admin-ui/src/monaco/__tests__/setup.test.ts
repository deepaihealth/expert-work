/**
 * B-152 guard: Monaco must come from our own bundle, never the
 * ``@monaco-editor/loader`` default (script tag to cdn.jsdelivr.net).
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const configSpy = vi.hoisted(() => vi.fn());
vi.mock("@monaco-editor/loader", () => ({ default: { config: configSpy } }));

const FAKE_MONACO = vi.hoisted(() => ({ editor: {}, fake: true }));
vi.mock("../localMonaco", () => ({ default: FAKE_MONACO }));

import { configureMonacoLoader } from "../setup";

describe("configureMonacoLoader", () => {
  beforeEach(() => {
    configSpy.mockClear();
  });

  it("hands the loader a local monaco and no CDN path", async () => {
    configureMonacoLoader();

    expect(configSpy).toHaveBeenCalledTimes(1);
    const arg = configSpy.mock.calls[0][0] as Record<string, unknown>;
    expect(arg.monaco).toBeTruthy();
    expect(arg).not.toHaveProperty("paths");
    expect(JSON.stringify(arg)).not.toContain("jsdelivr");

    // The loader adopts the value via ``resolve(thenable)``; it must resolve
    // to the locally bundled module.
    const resolved = await Promise.resolve(arg.monaco);
    expect(resolved).toBe(FAKE_MONACO);
  });
});
