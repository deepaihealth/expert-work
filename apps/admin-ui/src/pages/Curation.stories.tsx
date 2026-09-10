import type { Meta, StoryObj } from "@storybook/react";
import { MemoryRouter } from "react-router-dom";
import { App } from "antd";

import { Curation } from "./Curation";
import { TenantScopeProvider } from "../tenant/TenantScopeContext";
import { AuthProvider } from "../auth/AuthContext";
import { apiClient, setStoredToken } from "../api/client";
import "../i18n";

function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.`;
}

interface FixtureOptions {
  candidates: unknown[];
  datasets: unknown[];
  crossTenant?: boolean;
}

function withFixture({ candidates, datasets, crossTenant = false }: FixtureOptions) {
  return (Story: React.ComponentType) => {
    setStoredToken(makeJwt({ sub: "u1", tenant_id: "t1", roles: ["admin"] }));
    apiClient.defaults.adapter = (config) => {
      const url = config.url ?? "";
      let data: unknown = {};
      if (url.startsWith("/v1/curation/candidates")) {
        data = { items: candidates, total: candidates.length, cross_tenant: crossTenant };
      } else if (url.startsWith("/v1/eval-datasets")) {
        data = { items: datasets, total: datasets.length, cross_tenant: crossTenant };
      }
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

const meta: Meta<typeof Curation> = {
  title: "Pages/Curation",
  component: Curation,
};

export default meta;

type Story = StoryObj<typeof Curation>;

const candidate = {
  id: "c1",
  tenant_id: "t1",
  agent_name: "research",
  agent_version: "1.0",
  thread_id: "th1",
  user_id: null,
  trajectory_key: "obj/c1.json",
  outcome: "Agent gave incorrect answer about Q3 revenue",
  signal: "negative_feedback",
  feedback_rating: "down",
  status: "pending",
  eval_dataset_id: null,
  detected_at: "2026-05-26T10:00:00Z",
  reviewed_at: null,
  feedback_run_id: null,
  feedback_comment: null,
  feedback_changed_at: null,
};

const dataset = {
  id: "d1",
  tenant_id: "t1",
  agent_name: "research",
  name: "golden_q3_revenue",
  input: { query: "What was Q3 revenue?" },
  expected: { answer: "$1.4B" },
  source: "golden",
  source_trajectory_key: null,
  source_user_id: null,
  created_at: "2026-05-20T10:00:00Z",
  updated_at: "2026-05-26T10:00:00Z",
};

export const Empty: Story = {
  decorators: [withFixture({ candidates: [], datasets: [] })],
};

export const WithCandidates: Story = {
  decorators: [withFixture({ candidates: [candidate], datasets: [dataset] })],
};

export const CrossTenant: Story = {
  decorators: [withFixture({ candidates: [candidate], datasets: [dataset], crossTenant: true })],
};
