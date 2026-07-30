import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import "../../../../i18n";

import { TurnMeta } from "../TurnMeta";
import type { TurnSummary } from "../../../../api/turn_summary";

function summary(over: Partial<TurnSummary> = {}): TurnSummary {
  return {
    finalText: "hi",
    assistantTexts: ["hi"],
    reasoning: [],
    usage: {
      inputTokens: 100,
      outputTokens: 20,
      totalTokens: 120,
      cacheReadTokens: 0,
      cacheCreationTokens: 0,
      reasoningTokens: 0,
    },
    stepCount: 2,
    latencyMs: 1500,
    finishReason: "stop",
    modelName: "glm-5.2",
    perStepUsage: [],
    ...over,
  };
}

function renderMeta(s: TurnSummary) {
  render(
    <MemoryRouter>
      <TurnMeta summary={s} costCny={null} runId={null} threadId={null} />
    </MemoryRouter>,
  );
}

describe("TurnMeta", () => {
  it("shows the model name chip", () => {
    renderMeta(summary());
    expect(screen.getByText(/glm-5\.2/)).toBeInTheDocument();
  });

  it("hides finish_reason when it is the normal 'stop'", () => {
    renderMeta(summary({ finishReason: "stop" }));
    expect(screen.queryByTestId("playground-turn-finish")).not.toBeInTheDocument();
  });

  it("surfaces a non-stop finish_reason (e.g. length)", () => {
    renderMeta(summary({ finishReason: "length" }));
    expect(screen.getByText(/length/)).toBeInTheDocument();
  });

  it("shows a cache-write chip only when cacheCreationTokens > 0", () => {
    renderMeta(
      summary({
        usage: {
          inputTokens: 10,
          outputTokens: 2,
          totalTokens: 12,
          cacheReadTokens: 0,
          cacheCreationTokens: 7,
          reasoningTokens: 0,
        },
      }),
    );
    expect(screen.getByTestId("playground-turn-cache-write")).toBeInTheDocument();
    expect(screen.getByText(/7/)).toBeInTheDocument();
  });

  it("hides cache-write chip when cacheCreationTokens is 0", () => {
    renderMeta(summary());
    expect(screen.queryByTestId("playground-turn-cache-write")).not.toBeInTheDocument();
  });

  it("hides model chip when modelName is null", () => {
    renderMeta(summary({ modelName: null }));
    expect(screen.queryByTestId("playground-turn-model")).not.toBeInTheDocument();
  });
});
