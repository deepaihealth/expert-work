import { describe, expect, it } from "vitest";

import { formatBytes, isHiddenWorkspacePath } from "../workspace_format";

describe("formatBytes", () => {
  it("renders sub-KB sizes in plain bytes", () => {
    expect(formatBytes(512)).toBe("512 B");
  });

  it("scales up through KB/MB with one decimal place", () => {
    expect(formatBytes(2048)).toBe("2.0 KB");
    expect(formatBytes(1024 * 1024 * 1.5)).toBe("1.5 MB");
  });
});

describe("isHiddenWorkspacePath", () => {
  it("flags a path with a dotfile/dotdir segment", () => {
    expect(isHiddenWorkspacePath(".npm/_cacache/index")).toBe(true);
    expect(isHiddenWorkspacePath(".mplconfig/matplotlibrc")).toBe(true);
  });

  it("does not flag an ordinary path", () => {
    expect(isHiddenWorkspacePath("agent_report.md")).toBe(false);
  });
});
