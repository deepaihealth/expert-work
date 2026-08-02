/**
 * Storybook stories for SettingsUsage — Stream Z3.
 *
 * Mirrors SettingsMcpCatalog.stories: JWT via setStoredToken + an
 * apiClient.defaults.adapter returning the {success,data,error} envelope.
 * The usage page is tenant-scoped (not system_admin gated).
 */
import type { ComponentType } from "react";
import type { Meta, StoryObj } from "@storybook/react";
import { MemoryRouter } from "react-router-dom";
import { App } from "antd";

import { SettingsUsage } from "./SettingsUsage";
import type { UsageCost, UsageTokens } from "../api/usage";
import { AuthProvider } from "../auth/AuthContext";
import { TenantScopeProvider } from "../tenant/TenantScopeContext";
import { apiClient, setStoredToken } from "../api/client";
import "../i18n";

const COST: UsageCost = {
  month: "2026-06",
  group_by: "agent",
  as_of: "2026-06-03T10:00:00Z",
  total_billed_cost_micros: 4_820_000,
  groups: [
    {
      key: "customer-support-bot",
      input_tokens: 1_204_500,
      output_tokens: 320_100,
      cache_creation_tokens: 40_000,
      cache_read_tokens: 980_000,
      billed_cost_micros: 3_120_000,
      unpriced: false,
    },
    {
      key: "research-assistant",
      input_tokens: 410_000,
      output_tokens: 120_000,
      cache_creation_tokens: 0,
      cache_read_tokens: 0,
      billed_cost_micros: 1_700_000,
      unpriced: true,
    },
  ],
};

const TOKENS: UsageTokens = {
  month: "2026-06",
  as_of: "2026-06-03T11:30:00Z",
  realtime: true,
  total: {
    input_tokens: 1_614_500,
    output_tokens: 440_100,
    cache_creation_tokens: 40_000,
    cache_read_tokens: 980_000,
  },
  by_agent: [
    { key: "customer-support-bot", input_tokens: 1_204_500, output_tokens: 320_100, cache_creation_tokens: 40_000, cache_read_tokens: 980_000 },
    { key: "research-assistant", input_tokens: 410_000, output_tokens: 120_000, cache_creation_tokens: 0, cache_read_tokens: 0 },
  ],
  by_model: [
    { key: "claude-sonnet-4", input_tokens: 1_614_500, output_tokens: 440_100, cache_creation_tokens: 40_000, cache_read_tokens: 980_000 },
  ],
  by_kind: [
    { key: "conversation", input_tokens: 1_474_500, output_tokens: 400_100, cache_creation_tokens: 40_000, cache_read_tokens: 980_000 },
    { key: "skill_evolution", input_tokens: 140_000, output_tokens: 40_000, cache_creation_tokens: 0, cache_read_tokens: 0 },
  ],
};

function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.`;
}

function withFixture(cost: UsageCost, tokens: UsageTokens) {
  return (Story: ComponentType) => {
    setStoredToken(makeJwt({ sub: "u1", tenant_id: "t1", roles: ["member"] }));
    apiClient.defaults.adapter = (config) => {
      const url = config.url ?? "";
      const data = url.endsWith("/usage/tokens")
        ? { success: true, data: tokens, error: null }
        : url.endsWith("/usage/cost")
          ? { success: true, data: cost, error: null }
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

const meta: Meta<typeof SettingsUsage> = {
  title: "Pages/SettingsUsage",
  component: SettingsUsage,
};

export default meta;

type Story = StoryObj<typeof SettingsUsage>;

/** Populated month — billed cost + realtime tokens. */
export const Populated: Story = {
  decorators: [withFixture(COST, TOKENS)],
};

/** No usage recorded this month. */
export const Empty: Story = {
  decorators: [
    withFixture(
      { ...COST, total_billed_cost_micros: 0, groups: [] },
      { ...TOKENS, total: { input_tokens: 0, output_tokens: 0, cache_creation_tokens: 0, cache_read_tokens: 0 }, by_agent: [], by_model: [], by_kind: [] },
    ),
  ],
};
