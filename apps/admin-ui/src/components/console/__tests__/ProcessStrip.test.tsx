/**
 * ProcessStrip — the console's per-turn 过程条 (PR-A.1 Task 3, spec §八.3):
 * auto-expanded while the turn runs (last 3 rows + a 「还有 N 步…」 button),
 * collapsed to one headline once settled, a manual toggle winning over both.
 * Rows come from the real ``compactRowsOf`` projection, same ``upd()`` fixture
 * style as TurnBlock.test.tsx.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "antd";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import i18n from "../../../i18n";

import type { SseEvent } from "../../../api/sessions";
import { compactRowsOf } from "../../../api/trajectory_rows";
import { modelCallsOf } from "../live_rows";
import { ProcessStrip, type ProcessStripProps } from "../ProcessStrip";

function ev(event: string, data: unknown): SseEvent {
  return { id: null, event, data, rawData: "", receivedAt: "" };
}
function upd(node: string, channels: Record<string, unknown>): SseEvent {
  return ev("updates", { [node]: channels });
}

/** `n` agent steps, each with reasoning + one `web_search` call → 2n rows. */
function stepEvents(n: number): SseEvent[] {
  const out: SseEvent[] = [];
  for (let i = 1; i <= n; i += 1) {
    out.push(
      upd("agent", {
        step_count: i,
        messages: [
          {
            type: "ai",
            content: "",
            additional_kwargs: { reasoning_content: `想第 ${i} 步` },
            tool_calls: [{ id: `c${i}`, name: "web_search", args: { q: `q${i}` } }],
          },
        ],
      }),
      upd("tools", {
        messages: [
          { type: "tool", tool_call_id: `c${i}`, name: "web_search", content: "ok", status: "success" },
        ],
      }),
    );
  }
  return out;
}

/** TurnBlock 同款:紧凑行 + 这一轮的全部模型调用。 */
function fromEvents(events: readonly SseEvent[]): Pick<ProcessStripProps, "rows" | "modelCalls"> {
  return {
    rows: compactRowsOf(events).map((row) => ({ row })),
    modelCalls: modelCallsOf(events, undefined),
  };
}

function makeProps(over: Partial<ProcessStripProps> = {}): ProcessStripProps {
  return {
    ...fromEvents(stepEvents(1)),
    running: false,
    expandedRowIds: new Set<string>(),
    onToggleRow: vi.fn(),
    onInspectRow: vi.fn(),
    ...over,
  };
}

function renderStrip(props: ProcessStripProps) {
  return render(
    <App>
      <ProcessStrip {...props} />
    </App>,
  );
}

/** Every rendered compact row head (`console-row-<kind>`), body only. */
function visibleRowCount(): number {
  return screen.getAllByTestId(/^console-row-(think|tool)$/).length;
}

describe("ProcessStrip", () => {
  let priorLang: string;
  beforeEach(async () => {
    priorLang = i18n.language;
    await i18n.changeLanguage("zh-CN");
  });
  afterEach(async () => {
    await i18n.changeLanguage(priorLang);
  });

  it("collapses by default when settled and shows only the headline", () => {
    renderStrip(makeProps());

    const head = screen.getByTestId("console-process-head");
    expect(head).toHaveTextContent("模型调用 1 次 · 工具 1 次（web_search ×1）");
    expect(head).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("console-process-steps")).not.toBeInTheDocument();
    expect(screen.queryByTestId("console-process-spinner")).not.toBeInTheDocument();
  });

  it("renders the turn's skill chips even while collapsed", () => {
    const events: SseEvent[] = [
      upd("agent", {
        step_count: 1,
        messages: [
          {
            type: "ai",
            content: "",
            tool_calls: [
              { id: "s1", name: "skill_view", args: { skill_name: "pptx", path: "SKILL.md" } },
              { id: "s2", name: "skill_view", args: { skill_name: "pptx", path: "ref/a.md" } },
            ],
          },
        ],
      }),
      upd("tools", {
        messages: [
          { type: "tool", tool_call_id: "s1", name: "skill_view", content: "body", status: "success" },
          { type: "tool", tool_call_id: "s2", name: "skill_view", content: "ref", status: "success" },
        ],
      }),
    ];
    renderStrip(makeProps(fromEvents(events)));

    // 折叠态(settled 默认收起)chips 仍在 —— 「用了哪些技能」不该要求展开。
    expect(screen.queryByTestId("console-process-steps")).not.toBeInTheDocument();
    const chips = screen.getAllByTestId("console-skill-chip");
    expect(chips).toHaveLength(1);
    expect(chips[0]).toHaveTextContent("pptx");
    expect(chips[0]).toHaveTextContent("×2");
  });

  it("renders no skill chip row when the turn read no skill", () => {
    renderStrip(makeProps());
    expect(screen.queryByTestId("console-process-skills")).not.toBeInTheDocument();
  });

  it("the head button's accessible name is the headline itself — no aria-label masking it", () => {
    renderStrip(makeProps(fromEvents(stepEvents(3))));

    // 头部原来挂着 `aria-label="展开过程"`,它把摘要整段盖掉了 —— 屏幕阅读器
    // 只听得到「展开过程」,听不到「模型调用 3 次 · 工具 3 次…」。名字必须来自
    // 可见文本,开合状态交给 `aria-expanded`。
    const head = screen.getByRole("button", { name: /模型调用 3 次/ });
    expect(head).toBe(screen.getByTestId("console-process-head"));
    expect(head).not.toHaveAttribute("aria-label");
    expect(head).toHaveAttribute("aria-expanded", "false");
  });

  it("expands automatically while running and shows the last 3 rows + a 'more' button", async () => {
    renderStrip(makeProps({ ...fromEvents(stepEvents(4)), running: true }));

    expect(screen.getByTestId("console-process-head")).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("console-process-spinner")).toBeInTheDocument();
    expect(screen.getByTestId("console-process-steps")).toBeInTheDocument();
    expect(visibleRowCount()).toBe(3);
    // The *last* 3 of the 8 rows — the newest step, not the oldest.
    const steps = screen.getByTestId("console-process-steps");
    expect(steps).toHaveTextContent("想第 4 步");
    expect(steps).not.toHaveTextContent("想第 1 步");
    // 8 rows total, 3 shown → 5 folded away.
    expect(screen.getByTestId("console-process-more")).toHaveTextContent("5");

    await userEvent.click(screen.getByTestId("console-process-more"));
    expect(visibleRowCount()).toBe(8);
    expect(screen.getByTestId("console-process-steps")).toHaveTextContent("想第 1 步");
    expect(screen.queryByTestId("console-process-more")).not.toBeInTheDocument();
  });

  it("a manual toggle wins over the automatic state and is kept when running flips", async () => {
    const fixture = fromEvents(stepEvents(1));
    const { rerender } = renderStrip(makeProps({ ...fixture, running: true }));
    expect(screen.getByTestId("console-process-steps")).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("console-process-head"));
    expect(screen.queryByTestId("console-process-steps")).not.toBeInTheDocument();

    // The turn settles: the automatic value flips to "collapsed" too, but the
    // manual choice is what's remembered — and re-expanding stays possible.
    rerender(
      <App>
        <ProcessStrip {...makeProps({ ...fixture, running: false })} />
      </App>,
    );
    expect(screen.queryByTestId("console-process-steps")).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId("console-process-head"));
    expect(screen.getByTestId("console-process-steps")).toBeInTheDocument();
  });

  it("clicking a row's 轨迹 link calls onInspectRow with that row id", async () => {
    const onInspectRow = vi.fn();
    renderStrip(makeProps({ onInspectRow }));

    await userEvent.click(screen.getByTestId("console-process-head"));
    const link = screen.getAllByTestId("console-row-inspect")[1];
    // §八.3 — the row-level link reads 「轨迹」 now, not the old 「检查」.
    expect(link).toHaveTextContent("轨迹");
    await userEvent.click(link);
    expect(onInspectRow).toHaveBeenCalledWith("tool:0:0");
  });

  it("puts the failure count in its own red tail, and omits it when nothing failed", () => {
    // One step, one failing tool — the think row inherits that error status,
    // so the tail must still read 「1 次失败」.
    const failing: SseEvent[] = [
      upd("agent", {
        step_count: 1,
        messages: [
          {
            type: "ai",
            content: "",
            additional_kwargs: { reasoning_content: "想第 1 步" },
            tool_calls: [{ id: "c1", name: "http", args: { q: "q1" } }],
          },
        ],
      }),
      upd("tools", {
        messages: [
          { type: "tool", tool_call_id: "c1", name: "http", content: "boom", status: "error" },
        ],
      }),
    ];
    const { container, unmount } = renderStrip(makeProps(fromEvents(failing)));
    const tail = container.querySelector(".ew-process__failed");
    expect(tail).not.toBeNull();
    expect(tail).toHaveTextContent("1 次失败");
    expect(screen.getByTestId("console-process-head")).toHaveTextContent(
      "模型调用 1 次 · 工具 1 次（http ×1） · 1 次失败",
    );
    unmount();

    // Same shape, nothing failed → no tail at all.
    const clean = renderStrip(makeProps());
    expect(clean.container.querySelector(".ew-process__failed")).toBeNull();
  });

  it("B-202 — counts and times a step that only called tools (no reasoning, so no think row)", () => {
    // 第 1 步直接调工具、没写思考 —— 紧凑投影不给它出 think 行。原来时长按行
    // 相加,这 4s 模型耗时整段丢了,标题也少数一次。
    const events: SseEvent[] = [
      upd("agent", {
        step_count: 1,
        _duration_ms: 4_000,
        messages: [
          {
            type: "ai",
            content: "",
            tool_calls: [{ id: "c1", name: "web_search", args: { q: "q1" } }],
          },
        ],
      }),
      upd("tools", {
        messages: [
          {
            type: "tool", tool_call_id: "c1", name: "web_search", content: "ok", status: "success",
            additional_kwargs: { duration_ms: 1_000 },
          },
        ],
      }),
      upd("agent", { step_count: 2, _duration_ms: 2_000, messages: [{ type: "ai", content: "答复" }] }),
    ];
    renderStrip(makeProps(fromEvents(events)));
    const head = screen.getByTestId("console-process-head");
    expect(head).toHaveTextContent("模型调用 2 次 · 工具 1 次（web_search ×1）");
    // 4s + 1s + 2s = 7.0s,不是只数工具行的 1.0s。
    expect(head).toHaveTextContent("7.0s");
  });

  it("renders nothing for a turn without process rows", () => {
    renderStrip(makeProps({ rows: [] }));
    expect(screen.queryByTestId("console-process-head")).not.toBeInTheDocument();
    expect(screen.queryByTestId("console-process-steps")).not.toBeInTheDocument();
  });
});
