/**
 * Storybook stories for SettingsQuality — Stream RT-5 (RT-ADR-26).
 *
 * The ``/v1/quality`` SDK returns the **raw** payload (no envelope), so the
 * mock adapter returns ``{ items }`` directly for the quality URLs and the
 * ``{success,data,error}`` envelope only for the ``/me`` bootstrap call.
 */
import type { ComponentType } from "react";
import type { Meta, StoryObj } from "@storybook/react";
import { MemoryRouter } from "react-router-dom";
import { App } from "antd";

import { SettingsQuality } from "./SettingsQuality";
import type { QualityDriftAlert, QualityScore } from "../api/quality";
import { AuthProvider } from "../auth/AuthContext";
import { TenantScopeProvider } from "../tenant/TenantScopeContext";
import { apiClient, setStoredToken } from "../api/client";
import "../i18n";

function score(
  agent: string,
  overall: number,
  minutesAgo: number,
  dims: Record<string, number>,
): QualityScore {
  const at = new Date(Date.parse("2026-07-06T12:00:00Z") - minutesAgo * 60_000);
  return {
    id: minutesAgo,
    agent_name: agent,
    agent_version: "1",
    run_id: `run-${agent}-${minutesAgo}`,
    thread_id: `thread-${agent}-${minutesAgo}`,
    overall,
    dimensions: dims,
    rationale:
      overall <= 2
        ? "Missed the user's actual question."
        : "Addressed the request clearly.",
    judge_model: "claude-haiku-4-5-20251001",
    observed_at: at.toISOString(),
  };
}

const SCORES: QualityScore[] = [
  score("support-bot", 2, 10, {
    addressed_request: 2,
    coherence: 3,
    safety: 5,
  }),
  score("support-bot", 3, 120, {
    addressed_request: 3,
    coherence: 3,
    safety: 5,
  }),
  score("support-bot", 5, 400, {
    addressed_request: 5,
    coherence: 5,
    safety: 5,
  }),
  score("research-assistant", 4, 30, {
    addressed_request: 4,
    coherence: 4,
    safety: 5,
  }),
  score("research-assistant", 4, 300, {
    addressed_request: 4,
    coherence: 4,
    safety: 5,
  }),
];

const ALERTS: QualityDriftAlert[] = [
  {
    id: 1,
    agent_name: "support-bot",
    recent_mean: 2.7,
    baseline_mean: 4.6,
    drift_pct: 0.413,
    recent_count: 18,
    baseline_count: 92,
    detected_at: "2026-07-06T09:15:00Z",
  },
];

function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.`;
}

function withFixture(scores: QualityScore[], alerts: QualityDriftAlert[]) {
  return (Story: ComponentType) => {
    setStoredToken(makeJwt({ sub: "u1", tenant_id: "t1", roles: ["member"] }));
    apiClient.defaults.adapter = (config) => {
      const url = config.url ?? "";
      const data = url.endsWith("/quality/scores")
        ? { items: scores }
        : url.endsWith("/quality/drift-alerts")
          ? { items: alerts }
          : {
              success: true,
              data: {
                subject_id: "u1",
                subject_type: "user",
                tenant_id: "t1",
                auth_method: "jwt",
                roles: ["member"],
                scopes: [],
                is_system_admin: false,
                allowed_tenants: ["t1"],
              },
              error: null,
            };
      return Promise.resolve({
        data,
        status: 200,
        statusText: "OK",
        headers: {},
        config,
        request: {},
      });
    };
    return (
      <MemoryRouter>
        <AuthProvider>
          {/* Cross-tenant W3 — the page consumes useTenantScope (throws
              without a provider); same stack as EvalRunsList.stories. */}
          <TenantScopeProvider>
            <App>
              <Story />
            </App>
          </TenantScopeProvider>
        </AuthProvider>
      </MemoryRouter>
    );
  };
}

const meta: Meta<typeof SettingsQuality> = {
  title: "Pages/SettingsQuality",
  component: SettingsQuality,
};

export default meta;

type Story = StoryObj<typeof SettingsQuality>;

/** Populated — a drift alert, per-agent trend, and low-score drill rows. */
export const Populated: Story = {
  decorators: [withFixture(SCORES, ALERTS)],
};

/** No sampled scores and no alerts yet. */
export const Empty: Story = {
  decorators: [withFixture([], [])],
};
