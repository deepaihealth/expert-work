import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import i18n from "../../../i18n";

import type { SseEvent } from "../../../api/sessions";
import { trajectoryRowsOf } from "../../../api/trajectory_rows";
import { laneModelOf } from "../lane_strip_model";
import { LaneStrip } from "../LaneStrip";

const BASE_MS = 1_700_000_000_000;
let seqCounter = 0;

function ev(event: string, data: unknown, ms: number | null): SseEvent {
  seqCounter += 1;
  return {
    id: ms === null ? null : `${BASE_MS + ms}-${seqCounter}`,
    event,
    data,
    rawData: "",
    receivedAt: "2026-01-01T00:00:00.000Z",
  };
}
function upd(node: string, channels: Record<string, unknown>, ms: number | null): SseEvent {
  return ev("updates", { [node]: channels }, ms);
}

const INPUT = { text: "帮我看看这个客户", attachmentNames: [], inputs: {} };

function threeLaneEvents(): SseEvent[] {
  return [
    upd("agent", {
      step_count: 1,
      _duration_ms: 500,
      messages: [
        {
          type: "ai",
          content: "",
          additional_kwargs: { reasoning_content: "先查客户档案" },
          tool_calls: [{ id: "c1", name: "query_crm", args: { id: "C-1" } }],
        },
      ],
    }, 1000),
    upd("tools", {
      messages: [{ type: "tool", tool_call_id: "c1", name: "query_crm", content: "3 条记录", status: "success" }],
    }, 2000),
    upd("memory_recall", {
      recalled_memories: [{ id: "m1", kind: "fact", content: "x", importance: 0.5, confidence: 0.5 }],
    }, 3000),
  ];
}

describe("LaneStrip", () => {
  let priorLang: string;
  beforeEach(async () => {
    priorLang = i18n.language;
    await i18n.changeLanguage("zh-CN");
  });
  afterEach(async () => {
    await i18n.changeLanguage(priorLang);
  });

  it("renders three lanes with blocks positioned by percentage; clicking a block selects its row", async () => {
    const events = threeLaneEvents();
    const rows = trajectoryRowsOf(events, INPUT, null, "done");
    const model = laneModelOf(events, rows, { running: false, nowMs: Date.now() });
    const onSelectRow = vi.fn();

    const { rerender } = render(
      <LaneStrip events={events} rows={rows} running={false} selectedRowId={null} onSelectRow={onSelectRow} />,
    );

    const blocks = screen.getAllByTestId("console-lane-block");
    expect(blocks).toHaveLength(3);
    expect(blocks.map((b) => b.getAttribute("data-lane")).sort()).toEqual(["input", "model", "tools"]);

    const modelBlock = model.blocks.find((b) => b.lane === "model");
    if (!modelBlock) throw new Error("expected a model-lane block");
    const domModelBlock = blocks.find((b) => b.getAttribute("data-lane") === "model");
    if (!domModelBlock) throw new Error("expected a model-lane DOM block");

    const expectedLeft = (modelBlock.startMs / model.totalMs) * 100;
    expect(domModelBlock.style.left).toBe(`${expectedLeft}%`);
    expect(domModelBlock.getAttribute("data-row-id")).toBe(modelBlock.rowId);
    expect(domModelBlock.getAttribute("aria-pressed")).toBe("false");

    await userEvent.click(domModelBlock);
    expect(onSelectRow).toHaveBeenCalledWith(modelBlock.rowId);

    rerender(
      <LaneStrip events={events} rows={rows} running={false} selectedRowId={modelBlock.rowId} onSelectRow={onSelectRow} />,
    );
    const reselected = screen.getAllByTestId("console-lane-block").find((b) => b.getAttribute("data-lane") === "model");
    expect(reselected?.getAttribute("aria-pressed")).toBe("true");
  });

  it("a block with no resolvable row is a non-interactive span", async () => {
    const events = threeLaneEvents();
    const onSelectRow = vi.fn();
    // rows deliberately left empty — resolveGanttKey can never match, so
    // every block's rowId comes back null.
    render(<LaneStrip events={events} rows={[]} running={false} selectedRowId={null} onSelectRow={onSelectRow} />);

    const blocks = screen.getAllByTestId("console-lane-block");
    expect(blocks.length).toBeGreaterThan(0);
    for (const b of blocks) {
      expect(b.tagName).toBe("SPAN");
      expect(b.getAttribute("aria-pressed")).toBeNull();
    }

    await userEvent.click(blocks[0]);
    expect(onSelectRow).not.toHaveBeenCalled();
  });

  it("shows the degraded hint when the model is degraded", () => {
    const events: SseEvent[] = [
      upd("agent", { step_count: 1, messages: [{ type: "ai", content: "", tool_calls: [] }] }, null),
    ];
    const rows = trajectoryRowsOf(events, INPUT, null, "done");
    render(<LaneStrip events={events} rows={rows} running={false} selectedRowId={null} onSelectRow={vi.fn()} />);

    expect(screen.getByText("时间轴按事件顺序近似(缺服务端时戳)")).toBeInTheDocument();
  });
});
