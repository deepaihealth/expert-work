/**
 * EvalRunDetail tests — P1-S2.5-FE.
 *
 * Both reads (run + cases) are stubbed; the page renders under a routed
 * MemoryRouter so ``useParams`` resolves ``:runId``. Terminal status keeps
 * the live-poll timer off.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { AuthProvider } from "../../auth/AuthContext";
import { TenantScopeProvider } from "../../tenant/TenantScopeContext";
import "../../i18n";

import * as evalSdk from "../../api/eval_runs";
import { EvalRunDetail } from "../EvalRunDetail";
import type { EvalCaseResult, EvalRunRecord } from "../../api/eval_runs";

const RUN_ID = "11111111-1111-1111-1111-111111111111";

const RUN: EvalRunRecord = {
  id: RUN_ID,
  suite: "m0_baseline",
  status: "failed",
  triggered_by: "manual",
  summary: { pass_count: 1, total: 2 },
  created_at: "2026-06-14T08:00:00Z",
  started_at: "2026-06-14T08:00:05Z",
  finished_at: "2026-06-14T08:02:30Z",
};

const CASES: EvalCaseResult[] = [
  {
    id: 1,
    capability: "J.1_plan_execute",
    case_id: "J.1_plan_execute",
    passed: true,
    session_id: null,
    scores: { pass_rate: 1.0 },
    session_metrics: null,
  },
  {
    id: 2,
    capability: "J.2_reflect",
    case_id: "J.2_reflect",
    passed: false,
    session_id: null,
    scores: null,
    session_metrics: null,
  },
];

function renderPage(initial = `/eval-runs/${RUN_ID}`) {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <AuthProvider>
        <TenantScopeProvider>
          <Routes>
            <Route path="/eval-runs/:runId" element={<EvalRunDetail />} />
          </Routes>
        </TenantScopeProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

afterEach(() => vi.restoreAllMocks());

describe("EvalRunDetail", () => {
  it("renders run metadata and per-case results", async () => {
    vi.spyOn(evalSdk, "getEvalRun").mockResolvedValue(RUN);
    vi.spyOn(evalSdk, "getEvalRunCases").mockResolvedValue({ cases: CASES });

    renderPage();

    await waitFor(() => expect(screen.getByTestId("eval-detail-root")).toBeInTheDocument());
    expect(screen.getByText(RUN_ID)).toBeInTheDocument();
    expect(screen.getByText("1/2")).toBeInTheDocument();
    expect(screen.getByTestId("eval-cases-table")).toBeInTheDocument();
    // case_id + capability columns both render the id, so match ≥1.
    expect(screen.getAllByText("J.1_plan_execute").length).toBeGreaterThan(0);
  });

  it("surfaces SDK errors in an alert", async () => {
    vi.spyOn(evalSdk, "getEvalRun").mockRejectedValue(new Error("boom"));
    vi.spyOn(evalSdk, "getEvalRunCases").mockResolvedValue({ cases: [] });
    renderPage();
    await waitFor(() => expect(screen.getByTestId("eval-detail-error")).toBeInTheDocument());
  });

  it("URL ?tenant_id= wins for both reads (W4 aggregate row-jump)", async () => {
    const runSpy = vi.spyOn(evalSdk, "getEvalRun").mockResolvedValue(RUN);
    const casesSpy = vi.spyOn(evalSdk, "getEvalRunCases").mockResolvedValue({ cases: [] });

    renderPage(`/eval-runs/${RUN_ID}?tenant_id=tenant-2-xxxx`);

    await waitFor(() =>
      expect(runSpy).toHaveBeenCalledWith(RUN_ID, "tenant-2-xxxx"),
    );
    expect(casesSpy).toHaveBeenCalledWith(RUN_ID, "tenant-2-xxxx");
  });
});
