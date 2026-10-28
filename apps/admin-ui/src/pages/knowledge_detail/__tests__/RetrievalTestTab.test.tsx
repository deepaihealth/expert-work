/**
 * Retrieval test tab — B-206 K-7: when the rerank did not decide the order
 * (``rerank_outcome`` not ``ok``), the tab says why above the results.
 * B-206 K-1: a parent result shows the whole parent with its hit chunk marked.
 * B-175 P2-PR4: four scores, the document, time taken and the record, how the
 * search ran, a document filter, the rerank-floor override, 「存为库默认参数」.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import i18n from "../../../i18n";

import TEST_FIXTURE from "../../../api/__fixtures__/knowledge_retrieval_test.json";
import { ApiError } from "../../../api/client";
import * as knowledgeSdk from "../../../api/knowledge";
import { typedFixture } from "../../../test-utils/typedFixture";
import { RetrievalTestTab } from "../RetrievalTestTab";

const BASE: knowledgeSdk.KnowledgeBase = {
  id: "11111111-1111-1111-1111-111111111111",
  name: "support-docs",
  chunk_max_tokens: 512,
  chunk_overlap_tokens: 64,
  created_at: "2026-06-12T00:00:00Z",
  description: "FAQ",
  retrieval_config: { top_k: 5, score_threshold: null, method: "hybrid", rerank_enabled: true },
  embedding_provider: "qwen",
  embedding_model: "text-embedding-v4",
  needs_reindex: false,
  reindexing: false,
  stats: { document_count: 1, chunk_count: 3 },
};

function response(
  rerank_outcome: knowledgeSdk.RerankOutcome | null,
  extra: Partial<knowledgeSdk.RetrievalTestResponse> = {},
): knowledgeSdk.RetrievalTestResponse {
  return {
    query: "deductible",
    count: 1,
    rerank_outcome,
    ...extra,
    results: [
      {
        content: "The deductible is 500.",
        source: "faq.pdf#0",
        filename: "faq.pdf",
        chunk_index: 0,
        score: 0.91,
        recall_source: "both",
      },
    ],
  };
}

async function run(
  rerank_outcome: knowledgeSdk.RerankOutcome | null,
  extra: Partial<knowledgeSdk.RetrievalTestResponse> = {},
): Promise<void> {
  vi.spyOn(knowledgeSdk, "testRetrieval").mockResolvedValue(response(rerank_outcome, extra));
  render(
    <App>
      <RetrievalTestTab base={BASE} readScope={undefined} readonly={false} />
    </App>,
  );
  await userEvent.type(screen.getByTestId("kb-test-query"), "deductible");
  await userEvent.click(screen.getByTestId("kb-test-run"));
  await waitFor(() => expect(screen.getByTestId("kb-test-results")).toBeInTheDocument());
}

afterEach(() => vi.restoreAllMocks());

describe("RetrievalTestTab rerank outcome (B-206 K-7)", () => {
  it.each([
    ["rate_limited", "重排模型限流"],
    ["model_error", "重排模型调用失败"],
    ["model_unavailable", "重排模型的凭据不可用"],
    ["model_unsupported", "当前重排模型平台暂不支持调用"],
    ["no_model", "平台没有配置重排模型"],
  ] as const)("says why the order is the recall order: %s", async (outcome, words) => {
    const prior = i18n.language;
    await i18n.changeLanguage("zh-CN");
    try {
      await run(outcome);
      const notice = screen.getByTestId("kb-test-rerank-outcome");
      expect(notice).toHaveTextContent(words);
      expect(notice).toHaveTextContent("按召回顺序排列");
    } finally {
      await i18n.changeLanguage(prior);
    }
  });

  it.each([["ok"], [null]] as const)("says nothing when the rerank decided it: %s", async (outcome) => {
    await run(outcome);
    expect(screen.queryByTestId("kb-test-rerank-outcome")).toBeNull();
  });

  it("says a partly completed rerank ordered by the pass that completed (K-7a)", async () => {
    const prior = i18n.language;
    await i18n.changeLanguage("zh-CN");
    try {
      await run("partial");
      expect(screen.getByTestId("kb-test-rerank-outcome")).toHaveTextContent(
        "重排只完成一部分（另一路失败），结果按已完成的那一路排序。",
      );
    } finally {
      await i18n.changeLanguage(prior);
    }
  });

  it.each([
    ["no_model", "ant-alert-info"],
    ["model_unavailable", "ant-alert-info"],
    ["model_unsupported", "ant-alert-info"],
    ["rate_limited", "ant-alert-warning"],
    ["model_error", "ant-alert-warning"],
    ["partial", "ant-alert-warning"],
  ] as const)(
    "shows configuration states as info and failures as warnings (K-7a): %s",
    async (outcome, tone) => {
      await run(outcome);
      expect(screen.getByTestId("kb-test-rerank-outcome")).toHaveClass(tone);
    },
  );
});

async function inChinese(body: () => Promise<void>): Promise<void> {
  const prior = i18n.language;
  await i18n.changeLanguage("zh-CN");
  try {
    await body();
  } finally {
    await i18n.changeLanguage(prior);
  }
}

describe("RetrievalTestTab rerank-score threshold (B-206 K-7b)", () => {
  it("says a threshold set under another rerank model was left out, naming this search's models", async () => {
    // The page's base (loaded before a platform switch) knows neither model.
    await inChinese(async () => {
      await run("ok", {
        rerank_threshold_paused: true,
        rerank_threshold_model: "qwen/qwen3-vl-rerank",
        rerank_model: "qwen/qwen3-rerank",
      });
      const notice = screen.getByTestId("kb-test-rerank-threshold-paused");
      expect(notice).toHaveTextContent(
        "重排分数下限是在 qwen/qwen3-vl-rerank 下标定的，当前重排模型是 qwen/qwen3-rerank",
      );
      expect(notice).toHaveTextContent("已暂停生效，请重新标定");
      expect(notice).toHaveClass("ant-alert-warning");
    });
  });

  it("says nothing about the threshold when it was in force", async () => {
    await run("ok");
    expect(screen.queryByTestId("kb-test-rerank-threshold-paused")).toBeNull();
  });
});

/** A parent result: the parent row #2 of hit chunks #5 (best) and #4. */
const PARENT_TEXT = "Who is covered.\n\nThe deductible is 500.\n\nHow to claim.";

function parentResult(
  extra: Partial<knowledgeSdk.RetrievalTestResult> = {},
): knowledgeSdk.RetrievalTestResult {
  return {
    content: PARENT_TEXT,
    source: "faq.pdf#5",
    filename: "faq.pdf",
    chunk_index: 5,
    score: 0.91,
    recall_source: "both",
    heading_path: ["Benefits"],
    parent_index: 2,
    hit_chunk_indexes: [5, 4],
    hit_content: "The deductible is 500.",
    hit_heading_path: ["Benefits", "Deductible"],
    ...extra,
  };
}

async function runWith(results: knowledgeSdk.RetrievalTestResult[]): Promise<void> {
  vi.spyOn(knowledgeSdk, "testRetrieval").mockResolvedValue({
    query: "deductible",
    count: results.length,
    rerank_outcome: "ok",
    results,
  });
  render(
    <App>
      <RetrievalTestTab base={BASE} readScope={undefined} readonly={false} />
    </App>,
  );
  await userEvent.type(screen.getByTestId("kb-test-query"), "deductible");
  await userEvent.click(screen.getByTestId("kb-test-run"));
  await waitFor(() => expect(screen.getByTestId("kb-test-results")).toBeInTheDocument());
}

describe("RetrievalTestTab parent results (B-206 K-1)", () => {
  it("shows the whole parent with the hit chunk highlighted, naming the parent and its hits", async () => {
    await inChinese(async () => {
      await runWith([parentResult()]);
      expect(screen.getByTestId("kb-test-parent-0")).toHaveTextContent(
        "整块 #2 · 命中小段 #5、#4",
      );
      const mark = screen.getByTestId("kb-test-hit-mark-0");
      expect(mark.tagName).toBe("MARK");
      expect(mark.textContent).toBe("The deductible is 500.");
      // The mark sits inside the parent's full text, in place — never clipped,
      // so the highlight is always in view.
      expect(mark.parentElement?.textContent).toBe(PARENT_TEXT);
      expect(mark.parentElement).not.toHaveClass("ant-typography-ellipsis");
      expect(screen.getByTestId("kb-test-heading-0")).toHaveTextContent("Benefits");
      expect(screen.queryByTestId("kb-test-hit-0")).toBeNull();
    });
  });

  it("names a single hit in English", async () => {
    await runWith([parentResult({ hit_chunk_indexes: [5] })]);
    expect(screen.getByTestId("kb-test-parent-0")).toHaveTextContent(
      "Passage #2 · matched chunk #5",
    );
  });

  it("shows the hit chunk on its own above the parent when its text is not in the parent", async () => {
    await inChinese(async () => {
      await runWith([parentResult({ hit_content: "Deductible: 500 per year." })]);
      const block = screen.getByTestId("kb-test-hit-0");
      expect(block).toHaveTextContent("命中小段");
      expect(block).toHaveTextContent("Deductible: 500 per year.");
      expect(screen.queryByTestId("kb-test-hit-mark-0")).toBeNull();
      expect(screen.getByText(/Who is covered\./)).toHaveTextContent("How to claim.");
      // The block comes before the parent's text.
      const parentText = screen.getByText(/Who is covered\./);
      expect(
        block.compareDocumentPosition(parentText) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
    });
  });

  it("shows a result without a parent as before", async () => {
    await runWith([
      {
        content: "The deductible is 500.",
        source: "faq.pdf#0",
        filename: "faq.pdf",
        chunk_index: 0,
        score: 0.91,
        recall_source: "both",
        heading_path: ["Benefits"],
        parent_index: null,
        hit_chunk_indexes: [0],
        hit_content: null,
        hit_heading_path: [],
      },
    ]);
    // Clipped to four lines with an expander, as before.
    expect(screen.getByText("The deductible is 500.")).toHaveClass("ant-typography-ellipsis");
    expect(screen.getByTestId("kb-test-heading-0")).toHaveTextContent("Benefits");
    expect(screen.queryByTestId("kb-test-parent-0")).toBeNull();
    expect(screen.queryByTestId("kb-test-hit-mark-0")).toBeNull();
    expect(screen.queryByTestId("kb-test-hit-0")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// B-175 P2-PR4 — four scores, the document, time taken, the record, how the
// search ran, a per-document filter, the rerank-floor override and
// 「存为库默认参数」. The response is the shared contract fixture.
// ---------------------------------------------------------------------------

const UPGRADED = typedFixture<knowledgeSdk.RetrievalTestResponse>(TEST_FIXTURE);
const [FIXTURE_RESULT] = UPGRADED.results;

interface MountOptions {
  base?: knowledgeSdk.KnowledgeBase;
  readonly?: boolean;
  onBaseUpdated?: (next: knowledgeSdk.KnowledgeBase) => void;
  onOpenLogs?: () => void;
}

function mount(options: MountOptions = {}): void {
  render(
    <App>
      <RetrievalTestTab
        base={options.base ?? BASE}
        readScope={undefined}
        readonly={options.readonly ?? false}
        onBaseUpdated={options.onBaseUpdated}
        onOpenLogs={options.onOpenLogs}
      />
    </App>,
  );
}

async function search(
  answer: Partial<knowledgeSdk.RetrievalTestResponse> = {},
  options: MountOptions = {},
): Promise<ReturnType<typeof vi.spyOn>> {
  const spy = vi
    .spyOn(knowledgeSdk, "testRetrieval")
    .mockResolvedValue({ ...UPGRADED, ...answer });
  mount(options);
  await userEvent.type(screen.getByTestId("kb-test-query"), "deductible");
  await userEvent.click(screen.getByTestId("kb-test-run"));
  await waitFor(() => expect(spy).toHaveBeenCalled());
  return spy;
}

describe("RetrievalTestTab scores and documents (B-175 P2-PR4)", () => {
  it("shows the four scores of the best hit chunk, a dash for a path that missed it", async () => {
    await search({
      results: [FIXTURE_RESULT, { ...FIXTURE_RESULT, keyword_score: null, source: "guide.md#9" }],
    });
    const scores = screen.getByTestId("kb-test-scores-0");
    expect(scores).toHaveTextContent("Vector 0.8234");
    expect(scores).toHaveTextContent("Keyword 0.0607");
    expect(scores).toHaveTextContent("Fused 0.0328");
    expect(scores).toHaveTextContent("Rerank 0.9112");
    expect(screen.getByTestId("kb-test-scores-1")).toHaveTextContent("Keyword —");
  });

  it("falls back to the vector score from an older backend without sub-scores", async () => {
    await search({
      results: [
        { content: "x", source: "faq.pdf#0", filename: "faq.pdf", chunk_index: 0, score: 0.5, recall_source: "vector" },
      ],
    });
    expect(screen.getByTestId("kb-test-scores-0")).toHaveTextContent("Vector 0.5000");
    expect(screen.queryByTestId("kb-test-doc-0")).toBeNull();
    expect(screen.queryByTestId("kb-test-doc-0-deleted")).toBeNull();
  });

  it("names the document by its short id, title and file, or says it was deleted", async () => {
    await search({
      results: [FIXTURE_RESULT, { ...FIXTURE_RESULT, source: "gone.md#1", document: null }],
    });
    const doc = screen.getByTestId("kb-test-doc-0");
    expect(doc).toHaveTextContent("D12");
    expect(doc).toHaveTextContent("Guide");
    expect(doc).toHaveTextContent("guide.md");
    expect(doc).toHaveTextContent("uploaded");
    expect(screen.getByTestId("kb-test-doc-1-deleted")).toHaveTextContent("Document deleted");
  });

  it("says how long it took and that it was saved to the records, with a way there", async () => {
    const onOpenLogs = vi.fn();
    await search({}, { onOpenLogs });
    expect(screen.getByTestId("kb-test-elapsed")).toHaveTextContent("Took 412 ms");
    expect(screen.getByTestId("kb-test-logged")).toHaveTextContent(
      "Saved to the retrieval records",
    );
    await userEvent.click(screen.getByTestId("kb-test-open-logs"));
    expect(onOpenLogs).toHaveBeenCalledTimes(1);
  });

  it("says when the search was not saved to the records", async () => {
    await search({ log_id: null });
    expect(screen.getByTestId("kb-test-not-logged")).toBeInTheDocument();
    expect(screen.queryByTestId("kb-test-logged")).toBeNull();
  });

  it("says how the search was degraded, and nothing when it was not", async () => {
    await search({
      embedding_degraded: true,
      keyword_degraded: true,
      duplicates_dropped: 2,
      rerank_dropped: 3,
    });
    expect(screen.getByTestId("kb-test-embedding-degraded")).toHaveClass("ant-alert-warning");
    expect(screen.getByTestId("kb-test-keyword-degraded")).toHaveClass("ant-alert-warning");
    expect(screen.getByTestId("kb-test-duplicates-dropped")).toHaveTextContent(
      "2 duplicate passages were left out.",
    );
    expect(screen.getByTestId("kb-test-rerank-dropped")).toHaveTextContent("3 candidates");
  });

  it("says nothing about degradation for a clean search", async () => {
    await search();
    for (const id of ["embedding-degraded", "keyword-degraded", "duplicates-dropped", "rerank-dropped"]) {
      expect(screen.queryByTestId(`kb-test-${id}`)).toBeNull();
    }
  });

  it("says every candidate fell under the rerank floor instead of a bare 'no results'", async () => {
    await search({ status: "below_threshold", candidates: 7, count: 0, results: [] });
    expect(screen.getByTestId("kb-test-below-threshold")).toHaveTextContent(
      "All 7 candidates scored below the rerank floor",
    );
  });
});

/** Replace a number input's text (a plain ``clear`` on an unfocused antd
 *  InputNumber leaves the old value to type after). */
async function retype(input: HTMLElement, value: string): Promise<void> {
  await userEvent.tripleClick(input);
  await userEvent.keyboard(value);
}

/** Antd Select renders each option twice in jsdom; click the real one. */
const optionContent =
  (label: string) => (_content: string, el: Element | null) =>
    el?.classList.contains("ant-select-item-option-content") === true &&
    el.textContent === label;

describe("RetrievalTestTab document filter (B-175 P2-PR4)", () => {
  it("narrows the results to one document, and offers no filter for a single one", async () => {
    const other = {
      ...FIXTURE_RESULT,
      source: "rules.md#0",
      filename: "rules.md",
      content: "Rules text.",
      document: { ...FIXTURE_RESULT.document!, id: "doc-rules", doc_seq: 13, title: "Rules", filename: "rules.md" },
    };
    await search({ results: [FIXTURE_RESULT, other], count: 2 });
    expect(screen.getByText("Rules text.")).toBeInTheDocument();
    const filter = screen.getByTestId("kb-test-doc-filter");
    await userEvent.click(within(filter).getByRole("combobox"));
    await userEvent.click(await screen.findByText(optionContent("D12 · Guide")));
    await waitFor(() => expect(screen.queryByText("Rules text.")).toBeNull());
    expect(screen.getByText("guide.md#3")).toBeInTheDocument();
  });

  it("has no document filter when every result is from one document", async () => {
    await search();
    expect(screen.queryByTestId("kb-test-doc-filter")).toBeNull();
  });
});

describe("RetrievalTestTab rerank-floor override (B-175 P2-PR4)", () => {
  it("sends a floor typed for this search, and none when it is left empty", async () => {
    const spy = await search();
    expect(spy.mock.calls[0][1]).not.toHaveProperty("rerank_score_threshold");

    await userEvent.type(screen.getByTestId("kb-test-rerank-threshold"), "0.4");
    await userEvent.click(screen.getByTestId("kb-test-run"));
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(2));
    expect(spy.mock.calls[1][1]).toEqual(expect.objectContaining({ rerank_score_threshold: 0.4 }));
  });

  it("shows the base's own floor as the placeholder", () => {
    mount({ base: { ...BASE, retrieval_config: { ...BASE.retrieval_config!, rerank_score_threshold: 0.7 } } });
    expect(screen.getByTestId("kb-test-rerank-threshold")).toHaveAttribute(
      "placeholder",
      "Empty = the base's (0.7)",
    );
  });

  it("warns that a floor calibrated with more than 25 results is off", async () => {
    mount();
    const topK = screen.getByRole("spinbutton", { name: "Top-k" });
    await retype(topK, "30");
    // Without a floor to calibrate there is nothing to warn about.
    expect(screen.queryByTestId("kb-test-calibration-warning")).toBeNull();
    await userEvent.type(screen.getByTestId("kb-test-rerank-threshold"), "0.4");
    expect(screen.getByTestId("kb-test-calibration-warning")).toHaveTextContent(
      "more than 25 results",
    );
    await retype(topK, "25");
    expect(topK).toHaveValue("25");
    expect(screen.queryByTestId("kb-test-calibration-warning")).toBeNull();
  });
});

describe("RetrievalTestTab 存为库默认参数 (B-175 P2-PR4)", () => {
  async function confirmSave(): Promise<void> {
    await userEvent.click(screen.getByTestId("kb-test-save-defaults"));
    const buttons = await screen.findAllByRole("button", { name: "Save as base defaults" });
    await userEvent.click(buttons[buttons.length - 1]);
  }

  it("is off while the test runs the base's defaults", () => {
    mount();
    expect(screen.getByTestId("kb-test-save-defaults")).toBeDisabled();
  });

  it("saves only the changed parameters and hands the saved base back", async () => {
    const saved = { ...BASE, retrieval_config: { ...BASE.retrieval_config!, top_k: 8, rerank_score_threshold: 0.4 } };
    const update = vi.spyOn(knowledgeSdk, "updateBase").mockResolvedValue(saved);
    const onBaseUpdated = vi.fn();
    mount({ onBaseUpdated });
    const topK = screen.getByRole("spinbutton", { name: "Top-k" });
    await userEvent.clear(topK);
    await userEvent.type(topK, "8");
    await userEvent.type(screen.getByTestId("kb-test-rerank-threshold"), "0.4");

    await userEvent.click(screen.getByTestId("kb-test-save-defaults"));
    const changes = await screen.findByTestId("kb-test-save-defaults-changes");
    expect(changes).toHaveTextContent("Results (top-k): 5 → 8");
    expect(changes).toHaveTextContent("Rerank score floor: none → 0.4");
    expect(changes).toHaveTextContent("bound to the current rerank model");
    const buttons = screen.getAllByRole("button", { name: "Save as base defaults" });
    await userEvent.click(buttons[buttons.length - 1]);

    await waitFor(() => expect(onBaseUpdated).toHaveBeenCalledWith(saved));
    expect(update).toHaveBeenCalledWith("support-docs", {
      retrieval_top_k: 8,
      rerank_score_threshold: 0.4,
    });
  });

  it("is off, with the read-only hint, when the base is another tenant's", async () => {
    mount({ readonly: true });
    const topK = screen.getByRole("spinbutton", { name: "Top-k" });
    await userEvent.clear(topK);
    await userEvent.type(topK, "8");
    expect(screen.getByTestId("kb-test-save-defaults")).toBeDisabled();
    expect(screen.getByTestId("readonly-tooltip")).toBeInTheDocument();
  });

  it("shows a refusal (a viewer's 403) as an error", async () => {
    vi.spyOn(knowledgeSdk, "updateBase").mockRejectedValue(
      new ApiError("principal lacks required role", "FORBIDDEN", 403),
    );
    const onBaseUpdated = vi.fn();
    mount({ onBaseUpdated });
    const topK = screen.getByRole("spinbutton", { name: "Top-k" });
    await userEvent.clear(topK);
    await userEvent.type(topK, "8");
    await confirmSave();
    expect(await screen.findByText(/principal lacks required role/)).toBeInTheDocument();
    expect(onBaseUpdated).not.toHaveBeenCalled();
  });
});
