/**
 * TraceView tests — Batch 4b Task 4.
 *
 * Test-time i18n resolves to English (jsdom's navigator.language, per
 * src/i18n's LanguageDetector — see StepTimeline.test.tsx / TimelineFilterBar
 * .test.tsx precedent), so state-text assertions use the English copy.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import "../../../../i18n";

import { TraceView } from "../TraceView";
import { fetchRunTraceRaw, type RunTrace, type TraceSpan } from "../../../../api/trace_facade";

// Task 9's raw-view modal calls fetchRunTraceRaw — mock it so tests control
// the resolved/rejected content instead of hitting the network stub adapter.
vi.mock("../../../../api/trace_facade", () => ({ fetchRunTraceRaw: vi.fn() }));
const mockFetchRunTraceRaw = vi.mocked(fetchRunTraceRaw);

function makeSpan(
  over: Partial<TraceSpan> & Pick<TraceSpan, "id" | "parentId" | "kind" | "label">,
): TraceSpan {
  return {
    detail: null,
    startMs: 0,
    latencyMs: 0,
    model: null,
    inputTokens: null,
    outputTokens: null,
    costUsd: null,
    input: null,
    output: null,
    level: "default",
    statusMessage: null,
    ...over,
    purpose: over.purpose ?? "",
    group: over.group ?? null,
  };
}

const root = makeSpan({
  id: "r0",
  parentId: null,
  kind: "session",
  label: "会话运行",
  latencyMs: 10000,
});
const llm = makeSpan({
  id: "r1",
  parentId: "r0",
  kind: "llm",
  label: "LLM 调用",
  detail: "主推理",
  startMs: 100,
  latencyMs: 2600,
  model: "glm-4.6",
  inputTokens: 120,
  outputTokens: 340,
  costUsd: 0.0021,
  input: { kind: "text", text: "user: 帮我查天气", truncated: false, fullChars: 11 },
  output: { kind: "text", text: '{"reply":"晴天"}', truncated: false, fullChars: 14 },
});
const tool = makeSpan({
  id: "r2",
  parentId: "r0",
  kind: "tool",
  label: "工具调用",
  detail: "get_weather",
  startMs: 2800,
  latencyMs: 160,
});

// Task 8 fixtures — structured `RunTraceIo` (messages / text), kept separate
// from `llm`/`tool` above so the "empty segments not rendered" case (tool's
// null i/o) keeps covering that behavior undisturbed.
const structuredLlm = makeSpan({
  id: "sr1",
  parentId: "r0",
  kind: "llm",
  label: "LLM 调用",
  detail: "主推理",
  input: {
    kind: "messages",
    messages: [
      { role: "system", content: "sys", truncated: false, fullChars: 3, toolCalls: null },
      { role: "human", content: "现在几点", truncated: false, fullChars: 4, toolCalls: null },
      {
        role: "tool",
        content: "«UNTRUSTED nonce=ab»\n2026▁ 年\n«/UNTRUSTED nonce=ab»",
        truncated: false,
        fullChars: 10,
        toolCalls: null,
      },
    ],
  },
  output: { kind: "text", text: "晴天", truncated: false, fullChars: 2 },
});
const argsTool = makeSpan({
  id: "sr2",
  parentId: "r0",
  kind: "tool",
  label: "工具调用",
  detail: "exec_python",
  input: { kind: "text", text: '{"code":"1"}', truncated: false, fullChars: 11 },
  output: { kind: "text", text: "ok", truncated: false, fullChars: 2 },
});

// Review-fix fixtures (Task 8 batch 3 follow-up).
//
// Fix 1 regression: two llm spans, each with a single `system` message at
// index 0 (same array index → same MessageBlock fiber slot unless
// `TraceDetail` remounts on selection change) with distinct, identifiable
// content so a leak from A into B is observable.
const leakSpanA = makeSpan({
  id: "leak-a",
  parentId: "r0",
  kind: "llm",
  label: "LLM 调用",
  input: {
    kind: "messages",
    messages: [
      { role: "system", content: "system-A-secret", truncated: false, fullChars: 15, toolCalls: null },
    ],
  },
});
const leakSpanB = makeSpan({
  id: "leak-b",
  parentId: "r0",
  kind: "llm",
  label: "LLM 调用",
  input: {
    kind: "messages",
    messages: [
      { role: "system", content: "system-B-secret", truncated: false, fullChars: 15, toolCalls: null },
    ],
  },
});

// Fix 2: a tool span whose text-kind args AND result both contain UNTRUSTED
// fencing — tool i/o is exactly where untrusted content shows up in
// practice, and a tool span renders two independent `IoText` sections.
const untrustedTool = makeSpan({
  id: "sr3",
  parentId: "r0",
  kind: "tool",
  label: "工具调用",
  detail: "web_search",
  input: {
    kind: "text",
    text: "«UNTRUSTED nonce=aa»\nargs text\n«/UNTRUSTED nonce=aa»",
    truncated: false,
    fullChars: 9,
  },
  output: {
    kind: "text",
    text: "«UNTRUSTED nonce=zz»\nresult text\n«/UNTRUSTED nonce=zz»",
    truncated: false,
    fullChars: 11,
  },
});

// Fix 4a: a message with empty content + a toolCalls list — exercises
// MessageBlock's "→ called {name}" branch instead of an empty pre body.
const toolCallLlm = makeSpan({
  id: "sr4",
  parentId: "r0",
  kind: "llm",
  label: "LLM 调用",
  input: {
    kind: "messages",
    messages: [{ role: "ai", content: "", truncated: false, fullChars: 0, toolCalls: ["exec_python"] }],
  },
});

// Fix 4b: a truncated message — exercises the TruncationRow (copy + view
// raw affordances) below the message body.
const truncatedLlm = makeSpan({
  id: "sr5",
  parentId: "r0",
  kind: "llm",
  label: "LLM 调用",
  input: {
    kind: "messages",
    messages: [
      { role: "ai", content: "some long content", truncated: true, fullChars: 40000, toolCalls: null },
    ],
  },
});

// Task 9 fixture — an errored span (red waterfall row + `dt-err` detail).
// startMs after `llm`'s (100) — buildRows sorts siblings by startMs, and the
// test relies on row order [root, llm, erroredTool].
const erroredTool = makeSpan({
  id: "err1",
  parentId: "r0",
  kind: "tool",
  label: "工具调用",
  detail: "exec_python",
  startMs: 3000,
  level: "error",
  statusMessage: "SandboxTimeout",
});

function okTrace(spans: TraceSpan[] = [root, llm, tool]): RunTrace {
  return {
    status: "ok",
    trace: { name: "trace-1", latencyMs: 10000, totalCostUsd: 0.0021, spanCount: spans.length },
    spans,
  };
}

describe("TraceView", () => {
  beforeEach(() => {
    mockFetchRunTraceRaw.mockReset();
  });

  it("renders one row per span (tree order), each span's label/detail + fmtDuration, a waterfall bar per row, and a model chip only where the span has one", () => {
    render(<TraceView trace={okTrace()} />);
    expect(screen.getByTestId("trace-view")).toBeInTheDocument();

    const rows = screen.getAllByTestId("trace-row");
    expect(rows).toHaveLength(3);

    expect(screen.getByText("会话运行")).toBeInTheDocument();
    expect(screen.getByText(/LLM 调用/)).toBeInTheDocument();
    expect(screen.getByText(/主推理/)).toBeInTheDocument();
    expect(screen.getByText(/工具调用/)).toBeInTheDocument();
    expect(screen.getByText(/get_weather/)).toBeInTheDocument();

    // fmtDuration strings on each row's waterfall bar (scoped — the axis
    // header also renders "10.0s" at its 100% tick).
    const bars = screen.getAllByTestId("trace-bar");
    expect(bars).toHaveLength(3);
    expect(bars[0]).toHaveTextContent("10.0s");
    expect(bars[1]).toHaveTextContent("2.6s");
    expect(bars[2]).toHaveTextContent("160ms");

    // Only the llm span carries a model/cost — session + tool rows get no chip.
    expect(screen.getAllByTestId("trace-model-chip")).toHaveLength(1);
    expect(screen.getByTestId("trace-model-chip")).toHaveTextContent("glm-4.6");
    expect(screen.getAllByTestId("trace-cost-chip")).toHaveLength(1);
  });

  it("flags an auxiliary LLM span (purpose != main) with the aux marker, leaving the main conversation LLM unmarked", () => {
    const root = makeSpan({ id: "r0", parentId: null, kind: "session", label: "会话运行" });
    const mainLlm = makeSpan({ id: "r1", parentId: "r0", kind: "llm", label: "LLM 调用", purpose: "main" });
    const auxLlm = makeSpan({ id: "r2", parentId: "r0", kind: "llm", label: "记忆抽取", purpose: "memory" });
    const trace: RunTrace = {
      status: "ok",
      trace: { name: "t", latencyMs: 100, totalCostUsd: null, spanCount: 3 },
      spans: [root, mainLlm, auxLlm],
    };
    render(<TraceView trace={trace} />);
    expect(screen.getByText("记忆抽取")).toBeInTheDocument();
    // Exactly one span is auxiliary → one marker in the timeline (detail panel
    // is closed until a row is selected, so no second marker there).
    expect(screen.getAllByTestId("trace-aux-marker")).toHaveLength(1);
  });

  it("selecting a row opens the detail panel with that span's input/output (collapsible, empty segments not rendered); the close button clears it", () => {
    render(<TraceView trace={okTrace()} />);
    const rows = screen.getAllByTestId("trace-row");

    fireEvent.click(rows[1]); // llm row — has both input + output
    const detail = screen.getByTestId("trace-detail");
    expect(within(detail).getByTestId("trace-io-input")).toHaveTextContent("帮我查天气");
    expect(within(detail).getByTestId("trace-io-output")).toHaveTextContent('"reply":"晴天"');

    // Collapse the input section — its body should disappear from the DOM.
    const inputHead = within(within(detail).getByTestId("trace-io-input")).getByRole("button");
    fireEvent.click(inputHead);
    expect(within(detail).getByTestId("trace-io-input")).not.toHaveTextContent("帮我查天气");

    // Switch to the tool row — its input/output are both null, so neither
    // io segment renders.
    fireEvent.click(rows[2]);
    const detail2 = screen.getByTestId("trace-detail");
    expect(within(detail2).queryByTestId("trace-io-input")).not.toBeInTheDocument();
    expect(within(detail2).queryByTestId("trace-io-output")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("trace-detail-close"));
    expect(screen.queryByTestId("trace-detail")).not.toBeInTheDocument();
  });

  it.each([
    ["not_ready", "Processing", true],
    ["unavailable", "Unavailable", false],
    ["no_trace", "No trace", false],
  ] as const)("renders the %s degraded state (no tree) with its status text", (status, title, expectRefresh) => {
    // onRefresh is always supplied here — the button's presence is gated on
    // `status === "not_ready"`, not on the prop, so this also proves
    // unavailable/no_trace never render it even when a handler exists.
    render(<TraceView trace={{ status }} onRefresh={vi.fn()} />);
    expect(screen.getByTestId("trace-view")).toBeInTheDocument();
    expect(screen.getByText(title)).toBeInTheDocument();
    expect(screen.queryAllByTestId("trace-row")).toHaveLength(0);
    if (expectRefresh) {
      expect(screen.getByTestId("trace-refresh")).toBeInTheDocument();
    } else {
      expect(screen.queryByTestId("trace-refresh")).not.toBeInTheDocument();
    }
  });

  it("not_ready's refresh button calls onRefresh when clicked", () => {
    const onRefresh = vi.fn();
    render(<TraceView trace={{ status: "not_ready" }} onRefresh={onRefresh} />);
    fireEvent.click(screen.getByTestId("trace-refresh"));
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it("not_ready omits the refresh button when onRefresh isn't provided", () => {
    render(<TraceView trace={{ status: "not_ready" }} />);
    expect(screen.queryByTestId("trace-refresh")).not.toBeInTheDocument();
  });

  it("treats an ok trace with zero spans as not_ready (ingestion-in-progress), not an empty waterfall", () => {
    // Langfuse's non-atomic ingestion can hand back an ok trace whose child
    // observations haven't landed → zero spans. Without a guard, TraceTree
    // would draw the axis header with no rows (a bare ruler). Show the
    // refreshable not_ready card instead.
    render(<TraceView trace={okTrace([])} onRefresh={vi.fn()} />);
    expect(screen.queryAllByTestId("trace-row")).toHaveLength(0);
    expect(screen.getByTestId("trace-refresh")).toBeInTheDocument();
  });

  it("treats an ok trace whose spans have no root (all orphaned) as not_ready", () => {
    // Subtler ingestion window: child spans landed before their session-root
    // parent, so every parentId dangles and none is a root. The tree walk
    // starts from roots → zero rows → a bare axis. Show not_ready instead.
    const orphanA = makeSpan({ id: "o1", parentId: "root-not-ingested", kind: "tool", label: "工具调用" });
    const orphanB = makeSpan({ id: "o2", parentId: "root-not-ingested", kind: "llm", label: "LLM 调用" });
    render(<TraceView trace={okTrace([orphanA, orphanB])} onRefresh={vi.fn()} />);
    expect(screen.queryAllByTestId("trace-row")).toHaveLength(0);
    expect(screen.getByTestId("trace-refresh")).toBeInTheDocument();
  });

  it("llm span renders structured messages: system collapsed, human/tool visible, untrusted cleaned+badged", () => {
    render(<TraceView trace={okTrace([root, structuredLlm, argsTool])} />);
    fireEvent.click(screen.getAllByTestId("trace-row")[1]);
    const detail = screen.getByTestId("trace-detail");
    // system 默认收起:内容 "sys" 不在 DOM,role 标签在
    expect(within(detail).queryByText("sys")).not.toBeInTheDocument();
    expect(within(detail).getByText("现在几点")).toBeInTheDocument();
    // 不可信清洗:▁ 与 UNTRUSTED 不出现,badge 出现
    expect(within(detail).queryByText(/UNTRUSTED|▁/)).not.toBeInTheDocument();
    expect(within(detail).getByTestId("msg-untrusted")).toBeInTheDocument();
  });

  it("tool span uses 参数/结果 labels (Arguments/Result), not the llm span's messages/reply labels", () => {
    // Test-time i18n resolves to English (see file header) — assert the
    // English copy for the (kind-aware, translated) io-section titles;
    // Chinese "参数"/"结果" are the zh-CN values for the same keys.
    render(<TraceView trace={okTrace([root, structuredLlm, argsTool])} />);
    fireEvent.click(screen.getAllByTestId("trace-row")[2]);
    const detail = screen.getByTestId("trace-detail");
    expect(within(detail).getByText("Arguments")).toBeInTheDocument();
    expect(within(detail).getByText("Result")).toBeInTheDocument();
    expect(within(detail).queryByText(/^Messages$|^Reply$/)).not.toBeInTheDocument();
  });

  it("switching the selected span remounts the detail panel — no cross-span MessageBlock expand-state leak", () => {
    // Regression test for the missing `key={selected.id}` on `TraceDetail`:
    // without it, span B's system MessageBlock reuses span A's fiber (same
    // array index) and inherits A's manually-expanded state.
    render(<TraceView trace={okTrace([root, leakSpanA, leakSpanB])} />);
    const rows = screen.getAllByTestId("trace-row");

    fireEvent.click(rows[1]); // select span A
    const detailA = screen.getByTestId("trace-detail");
    const messageA = within(detailA).getByTestId("trace-message");
    // system starts collapsed — expand it by clicking its header.
    expect(within(messageA).queryByText("system-A-secret")).not.toBeInTheDocument();
    fireEvent.click(within(messageA).getByRole("button"));
    expect(within(messageA).getByText("system-A-secret")).toBeInTheDocument();

    fireEvent.click(rows[2]); // select span B directly, without closing
    const detailB = screen.getByTestId("trace-detail");
    const messageB = within(detailB).getByTestId("trace-message");
    // If the leak were present, B's system message would already be
    // expanded (inheriting A's toggle) and show its content.
    expect(within(messageB).queryByText("system-B-secret")).not.toBeInTheDocument();
    expect(within(messageB).queryByText("system-A-secret")).not.toBeInTheDocument();
  });

  it("IoText (tool span text i/o) shows the untrusted badge for both args and result when each contains UNTRUSTED fencing", () => {
    render(<TraceView trace={okTrace([root, untrustedTool])} />);
    fireEvent.click(screen.getAllByTestId("trace-row")[1]);
    const detail = screen.getByTestId("trace-detail");
    // A tool span has two independent IoText sections (args + result), so
    // multiple badges can coexist — assert with getAllByTestId, not
    // getByTestId.
    expect(within(detail).getAllByTestId("msg-untrusted")).toHaveLength(2);
    expect(within(detail).getByText("args text")).toBeInTheDocument();
    expect(within(detail).getByText("result text")).toBeInTheDocument();
    expect(within(detail).queryByText(/UNTRUSTED|▁/)).not.toBeInTheDocument();
  });

  it("message header shows the localized size hint ('N chars'), not a hardcoded '字'", () => {
    render(<TraceView trace={okTrace([root, structuredLlm, argsTool])} />);
    fireEvent.click(screen.getAllByTestId("trace-row")[1]);
    const detail = screen.getByTestId("trace-detail");
    // structuredLlm's human message has fullChars: 4.
    expect(within(detail).getByText("4 chars")).toBeInTheDocument();
    expect(within(detail).queryByText(/字/)).not.toBeInTheDocument();
  });

  it("a message with empty content + toolCalls renders the tool-call text, not an empty body", () => {
    render(<TraceView trace={okTrace([root, toolCallLlm])} />);
    fireEvent.click(screen.getAllByTestId("trace-row")[1]);
    const detail = screen.getByTestId("trace-detail");
    expect(within(detail).getByText("→ called exec_python")).toBeInTheDocument();
  });

  it("a truncated message renders the truncated-size text plus copy + view-raw affordances", () => {
    render(<TraceView trace={okTrace([root, truncatedLlm])} />);
    fireEvent.click(screen.getAllByTestId("trace-row")[1]);
    const detail = screen.getByTestId("trace-detail");
    expect(within(detail).getByText("Truncated 40000 chars")).toBeInTheDocument();
    expect(within(detail).getByText("Copy")).toBeInTheDocument();
    expect(within(detail).getByText("View raw")).toBeInTheDocument();
  });

  it("an error span's row is red-marked (data-error) and its detail shows the statusMessage; other spans get neither", () => {
    render(<TraceView trace={okTrace([root, llm, erroredTool])} />);
    const rows = screen.getAllByTestId("trace-row");
    expect(rows).toHaveLength(3);
    expect(rows[0]).not.toHaveAttribute("data-error");
    expect(rows[1]).not.toHaveAttribute("data-error");
    expect(rows[2]).toHaveAttribute("data-error", "true");

    fireEvent.click(rows[2]);
    const detail = screen.getByTestId("trace-detail");
    expect(within(detail).getByTestId("trace-detail-error")).toHaveTextContent("SandboxTimeout");

    // A non-error span's detail carries no error block.
    fireEvent.click(rows[1]);
    const detail2 = screen.getByTestId("trace-detail");
    expect(within(detail2).queryByTestId("trace-detail-error")).not.toBeInTheDocument();
  });

  it("查看原文 fetches the field-scoped raw content via fetchRunTraceRaw and shows it un-cleaned in a modal", async () => {
    mockFetchRunTraceRaw.mockResolvedValue("RAW FULL CONTENT «UNTRUSTED nonce=x» …");
    render(<TraceView trace={okTrace([root, truncatedLlm])} threadId="t1" runId="r1" />);
    fireEvent.click(screen.getAllByTestId("trace-row")[1]);
    const detail = screen.getByTestId("trace-detail");
    fireEvent.click(within(detail).getByText("View raw"));

    const modal = await screen.findByTestId("trace-raw-modal");
    // Un-cleaned: the UNTRUSTED fence marker survives verbatim (the raw
    // layer must NOT run cleanUntrusted — that's its whole purpose).
    expect(modal.textContent).toContain("RAW FULL CONTENT");
    expect(modal.textContent).toContain("«UNTRUSTED nonce=x»");
    expect(mockFetchRunTraceRaw).toHaveBeenCalledWith("t1", "r1", "sr5", "input");
  });

  it("查看原文 shows an error message in the modal when the raw fetch fails, without crashing", async () => {
    mockFetchRunTraceRaw.mockRejectedValueOnce(new Error("network down"));
    render(<TraceView trace={okTrace([root, truncatedLlm])} threadId="t1" runId="r1" />);
    fireEvent.click(screen.getAllByTestId("trace-row")[1]);
    fireEvent.click(within(screen.getByTestId("trace-detail")).getByText("View raw"));

    expect(await screen.findByText("Failed to load raw content")).toBeInTheDocument();
  });

  it("查看原文 no-ops without threadId/runId (no fetch call, no modal, no crash)", () => {
    render(<TraceView trace={okTrace([root, truncatedLlm])} />);
    fireEvent.click(screen.getAllByTestId("trace-row")[1]);
    fireEvent.click(within(screen.getByTestId("trace-detail")).getByText("View raw"));

    expect(screen.queryByTestId("trace-raw-modal")).not.toBeInTheDocument();
    expect(mockFetchRunTraceRaw).not.toHaveBeenCalled();
  });
});
