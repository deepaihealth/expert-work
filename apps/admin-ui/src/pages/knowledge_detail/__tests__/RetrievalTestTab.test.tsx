/**
 * Retrieval test tab — B-206 K-7: when the rerank did not decide the order
 * (``rerank_outcome`` not ``ok``), the tab says why above the results.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import i18n from "../../../i18n";

import * as knowledgeSdk from "../../../api/knowledge";
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
): knowledgeSdk.RetrievalTestResponse {
  return {
    query: "deductible",
    count: 1,
    rerank_outcome,
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

async function run(rerank_outcome: knowledgeSdk.RerankOutcome | null): Promise<void> {
  vi.spyOn(knowledgeSdk, "testRetrieval").mockResolvedValue(response(rerank_outcome));
  render(
    <App>
      <RetrievalTestTab base={BASE} readScope={undefined} />
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
