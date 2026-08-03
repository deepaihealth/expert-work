/**
 * EvalEvidencePanel tests — Stream SE (SE-8-5).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { App } from "antd";
import "../../../i18n";

import * as sdk from "../../../api/skill-evolution";
import type { SkillEvalResult } from "../../../api/skill-evolution";
import { EvalEvidencePanel } from "../EvalEvidencePanel";

const listMock = vi.spyOn(sdk, "listEvalResults");

function evalResult(overrides: Partial<SkillEvalResult> = {}): SkillEvalResult {
  return {
    id: "ev-1",
    tenant_id: "t1",
    skill_id: "sk-1",
    skill_version: 2,
    baseline_score: 0.4,
    skill_score: 0.85,
    delta: 0.45,
    n_cases: 12,
    replay_source: "trajectory",
    verdict: "pass",
    high_risk: false,
    evolution_round: 0,
    created_at: "2026-06-08T00:00:00Z",
    ...overrides,
  };
}

// Cross-tenant W4(D2)— readScope 由 SkillDetail 下传(不再自读 ambient
// scope);默认 home 态(undefined)。
function renderPanel(readScope?: string) {
  return render(
    <App>
      <EvalEvidencePanel skillId="sk-1" readScope={readScope} />
    </App>,
  );
}

beforeEach(() => {
  listMock.mockReset();
});
afterEach(() => {
  vi.clearAllMocks();
});

describe("EvalEvidencePanel", () => {
  it("threads readScope through listEvalResults (跨租户钻取)", async () => {
    const scope = "22222222-2222-2222-2222-222222222222";
    listMock.mockResolvedValue([]);
    renderPanel(scope);
    await waitFor(() => expect(listMock).toHaveBeenCalledWith("sk-1", scope));
  });

  it("renders a paired-bar row per eval result", async () => {
    listMock.mockResolvedValue([evalResult()]);
    renderPanel();
    await waitFor(() => expect(screen.getByTestId("skill-eval-row-ev-1")).toBeInTheDocument());
    expect(screen.getByTestId("skill-eval-row-ev-1")).toHaveTextContent(/v2/);
    // delta surfaced with a sign
    expect(screen.getByTestId("skill-eval-row-ev-1")).toHaveTextContent(/\+0\.45/);
  });

  it("shows the empty state when there is no evidence", async () => {
    listMock.mockResolvedValue([]);
    renderPanel();
    await waitFor(() => expect(screen.getByTestId("skill-eval-empty")).toBeInTheDocument());
  });
});
