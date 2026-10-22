/**
 * Trajectory panel, tool result tab — B-206 K-7: a ``knowledge_search`` call
 * whose rerank fell back to the recall order says why above its result.
 */
import { describe, expect, it, vi } from "vitest";
import { App } from "antd";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import i18n from "../../../i18n";

import type { ToolRow } from "../../../api/trajectory_rows";
import { RowDetailResult } from "../RowDetailPayloadResult";

// ToolCallCard reads useIsTenantSwitched (no Auth / TenantScope provider here).
vi.mock("../../../tenant/useIsTenantSwitched", () => ({
  useIsTenantSwitched: () => false,
}));

function knowledgeRow(over: Partial<ToolRow["entry"]> = {}): ToolRow {
  return {
    id: "tool:1:0",
    kind: "tool",
    seq: 1,
    step: 1,
    status: "ok",
    durationMs: 300,
    eventIndexes: [0],
    serverMs: 1_700_000_001_200,
    entry: {
      id: "k1",
      rawName: "knowledge_search",
      isMcp: false,
      server: null,
      toolName: "knowledge_search",
      args: { query: "deductible" },
      status: "success",
      resultPreview: "[faq.pdf#0] The deductible is 500.",
      durationMs: 300,
      ...over,
    },
  };
}

function renderResult(row: ToolRow) {
  return render(
    <MemoryRouter>
      <App>
        <RowDetailResult row={row} onOpenFullText={() => {}} />
      </App>
    </MemoryRouter>,
  );
}

describe("RowDetailResult rerank outcome (B-206 K-7)", () => {
  it("says the rerank fell back, in plain Chinese, above the result", async () => {
    const prior = i18n.language;
    await i18n.changeLanguage("zh-CN");
    try {
      renderResult(knowledgeRow({ rerankOutcome: "rate_limited" }));
      const result = screen.getByTestId("console-detail-result");
      const notice = within(result).getByTestId("console-detail-rerank-outcome");
      expect(notice).toHaveTextContent("重排模型限流，本次结果按召回顺序排列");
      expect(within(result).getByText(/The deductible is 500/)).toBeInTheDocument();
    } finally {
      await i18n.changeLanguage(prior);
    }
  });

  it("says nothing for a call whose rerank decided the order", () => {
    renderResult(knowledgeRow());
    expect(screen.queryByTestId("console-detail-rerank-outcome")).toBeNull();
  });

  it.each([
    ["no_model", "ant-typography-secondary"],
    ["model_unavailable", "ant-typography-secondary"],
    ["rate_limited", "ant-typography-warning"],
    ["partial", "ant-typography-warning"],
  ] as const)(
    "shows configuration states quietly and failures as warnings (K-7a): %s",
    (outcome, tone) => {
      renderResult(knowledgeRow({ rerankOutcome: outcome }));
      expect(screen.getByTestId("console-detail-rerank-outcome")).toHaveClass(tone);
    },
  );
});
