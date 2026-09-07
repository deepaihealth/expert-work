/**
 * ``readBuildInfo`` —— 「关于」弹窗的数据源。
 *
 * 三个值由 build-push.sh 在发版时烤进镜像;本地 dev 一个都没有。弹窗显示的
 * 就是这里读出来的,所以这里要证明:烤进去的能读出来、没烤的读成 undefined、
 * 环境值只认 test / prod。
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { readBuildInfo } from "../env";

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("readBuildInfo", () => {
  it("reads the three values baked in by build-push.sh", () => {
    vi.stubEnv("VITE_APP_VERSION", "fc042aab");
    vi.stubEnv("VITE_APP_ENV", "prod");
    vi.stubEnv("VITE_BUILD_TIME", "2026-09-07T06:20:00Z");

    expect(readBuildInfo()).toEqual({
      version: "fc042aab",
      env: "prod",
      builtAt: "2026-09-07T06:20:00Z",
    });
  });

  it("returns undefined for everything on a local dev build", () => {
    vi.stubEnv("VITE_APP_VERSION", "");
    vi.stubEnv("VITE_APP_ENV", "");
    vi.stubEnv("VITE_BUILD_TIME", "");

    expect(readBuildInfo()).toEqual({ version: undefined, env: undefined, builtAt: undefined });
  });

  it("only accepts test / prod as the environment — anything else is unknown", () => {
    vi.stubEnv("VITE_APP_ENV", "staging");

    expect(readBuildInfo().env).toBeUndefined();
  });
});
