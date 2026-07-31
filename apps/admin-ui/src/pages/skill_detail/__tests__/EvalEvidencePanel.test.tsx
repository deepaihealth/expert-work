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

let mockScope: string | undefined;
vi.mock("../../../tenant/TenantScopeContext", () => ({
  SCOPE_ALL: "*",
  useTenantScope: () => ({ scope: mockScope, apiTenantScope: mockScope }),
}));

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

function renderPanel() {
  return render(
    <App>
      <EvalEvidencePanel skillId="sk-1" />
    </App>,
  );
}

beforeEach(() => {
  listMock.mockReset();
});
afterEach(() => {
  mockScope = undefined;
  vi.clearAllMocks();
});

describe("EvalEvidencePanel", () => {
  it("threads the tenant scope through listEvalResults (跨租户钻取)", async () => {
    mockScope = "22222222-2222-2222-2222-222222222222";
    listMock.mockResolvedValue([]);
    renderPanel();
    await waitFor(() => expect(listMock).toHaveBeenCalledWith("sk-1", mockScope));
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
