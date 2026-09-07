/**
 * 「关于」弹窗 —— 线上跑的是哪个版本、哪个环境、什么时候发的。
 *
 * 数据全部来自构建时烤进去的三个 VITE_ 值(config/env.ts readBuildInfo)。
 * 要证明的是:烤了就显示、生产要醒目、本地 dev 不装作是线上。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import "../../i18n";
import { AboutModal } from "../AboutModal";

// 测试环境下 i18n 走 navigator.language(jsdom = en-US),与 Sidebar.test 同款:断英文文案。

afterEach(() => {
  vi.unstubAllEnvs();
});

function stubBuild(version: string, env: string, builtAt: string): void {
  vi.stubEnv("VITE_APP_VERSION", version);
  vi.stubEnv("VITE_APP_ENV", env);
  vi.stubEnv("VITE_BUILD_TIME", builtAt);
}

describe("AboutModal", () => {
  it("shows version, environment and build time from the baked build info", () => {
    stubBuild("fc042aab", "test", "2026-09-07T06:20:00Z");

    render(<AboutModal open onClose={() => {}} />);

    expect(screen.getByText("fc042aab")).toBeInTheDocument();
    expect(screen.getByText("Test")).toBeInTheDocument();
    // 绝对时间按本地时区渲染;只断言日期部分,避免测试机时区把小时数带偏。
    expect(screen.getByText(/2026-09-07/)).toBeInTheDocument();
  });

  it("links the version to the commit on GitHub", () => {
    stubBuild("fc042aab", "test", "2026-09-07T06:20:00Z");

    render(<AboutModal open onClose={() => {}} />);

    const link = screen.getByRole("link", { name: "fc042aab" });
    expect(link).toHaveAttribute("href", "https://github.com/deepaihealth/expert-work/commit/fc042aab");
  });

  it("marks production distinctly — the whole point is not mistaking prod for test", () => {
    stubBuild("fc042aab", "prod", "2026-09-07T06:20:00Z");

    render(<AboutModal open onClose={() => {}} />);

    expect(screen.getByText("Production")).toBeInTheDocument();
    expect(screen.queryByText("Test")).not.toBeInTheDocument();
  });

  it("says 'local dev' instead of pretending when nothing was baked in", () => {
    stubBuild("", "", "");

    render(<AboutModal open onClose={() => {}} />);

    expect(screen.getByText("Local dev")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});
