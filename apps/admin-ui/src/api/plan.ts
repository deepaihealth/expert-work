/**
 * Thread plan API — Stream CM-8 (the plan UI channel).
 *
 * Raw (no envelope) endpoints, matching the runs.py family style:
 * ``GET /v1/sessions/{thread_id}/plan`` returns the protocol ``Plan``
 * directly (204 → null when the thread has no plan yet); ``PUT``
 * rewrites it and echoes the stored plan back. Writes are rejected
 * with 409 while the thread's latest run is queued or live
 * (Mini-ADR CM-I3) and 422 when the injection scan hits (CM-I6).
 */
import { apiClient } from "./client";

export type PlanStepStatus = "pending" | "in_progress" | "completed";

// B-35 — plan_first dispatch marker; absent on pre-B-35 payloads = inline.
export type PlanStepExecution = "delegate" | "inline";

export interface PlanStep {
  id: string;
  description: string;
  status: PlanStepStatus;
  execution?: PlanStepExecution;
}

export interface ThreadPlan {
  goal: string;
  steps: PlanStep[];
}

export async function getThreadPlan(threadId: string): Promise<ThreadPlan | null> {
  const response = await apiClient.get<ThreadPlan | "">(`/v1/sessions/${threadId}/plan`);
  if (response.status === 204 || response.data === "") {
    return null;
  }
  return response.data as ThreadPlan;
}

export async function updateThreadPlan(
  threadId: string,
  plan: ThreadPlan,
): Promise<ThreadPlan> {
  const response = await apiClient.put<ThreadPlan>(`/v1/sessions/${threadId}/plan`, plan);
  return response.data;
}
