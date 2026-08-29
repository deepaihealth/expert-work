/**
 * SpecVersionField tests —— 「这次运行用的是哪一版配置」。
 *
 * 这一格存在的全部理由是 `agent_version` 回答不了这个问题(配置原地编辑,
 * 版本号编辑前后一样),所以这里主要验的是那个判断:同一个 (name, version)
 * 下,这次运行的哈希与**当前生效**的哈希是不是同一个。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import "../../i18n";

import { SpecVersionField } from "../run_detail/SpecVersionField";

vi.mock("../../api/agents", () => ({
  getAgent: vi.fn(),
  listRevisions: vi.fn(),
}));

const { getAgent, listRevisions } = await import("../../api/agents");

const CURRENT = "a".repeat(64);
const OLDER = "b".repeat(64);

function seed(currentSha: string) {
  vi.mocked(getAgent).mockResolvedValue({
    record: { spec_sha256: currentSha },
  } as unknown as Awaited<ReturnType<typeof getAgent>>);
  vi.mocked(listRevisions).mockResolvedValue({
    items: [
      { revision: 2, spec_sha256: CURRENT, actor_id: "u", created_at: "2026-08-29T00:00:00Z" },
      { revision: 1, spec_sha256: OLDER, actor_id: "u", created_at: "2026-08-28T00:00:00Z" },
    ],
  });
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("SpecVersionField", () => {
  it("marks the run as running the config that is still live", async () => {
    seed(CURRENT);
    render(<SpecVersionField sha={CURRENT} agentName="a" agentVersion="1.0.0" />);

    expect(await screen.findByText("Revision 2")).toBeInTheDocument();
    expect(await screen.findByText("Live")).toBeInTheDocument();
  });

  it("marks the run as superseded when the config was edited afterwards", async () => {
    // 运行用的是第 1 版,而现在生效的是第 2 版 —— 配置页上看到的不是这次跑的。
    seed(CURRENT);
    render(<SpecVersionField sha={OLDER} agentName="a" agentVersion="1.0.0" />);

    expect(await screen.findByText("Revision 1")).toBeInTheDocument();
    expect(await screen.findByText("Superseded")).toBeInTheDocument();
  });

  it("says not-recorded rather than inventing a version when the run has no hash", () => {
    // NULL 不是「用了空配置」,而是这条 run 早于该列上线,或在构建成功前就
    // 结束了。此时一次接口都不该发。
    render(<SpecVersionField sha={null} agentName="a" agentVersion="1.0.0" />);

    expect(screen.getByText("Not recorded")).toBeInTheDocument();
    expect(getAgent).not.toHaveBeenCalled();
    expect(listRevisions).not.toHaveBeenCalled();
  });

  it("falls back to the short hash when the version cannot be resolved", async () => {
    // 反查失败(权限/接口挂了)不该让整格空掉:短哈希认不出来源,但两条 run
    // 的哈希是不是同一个仍然肉眼可判。
    vi.mocked(getAgent).mockRejectedValue(new Error("nope"));
    vi.mocked(listRevisions).mockRejectedValue(new Error("nope"));
    render(<SpecVersionField sha={OLDER} agentName="a" agentVersion="1.0.0" />);

    expect(await screen.findByText(OLDER.slice(0, 12))).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByText("Live")).not.toBeInTheDocument();
      expect(screen.queryByText("Superseded")).not.toBeInTheDocument();
    });
  });
});
