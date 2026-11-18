import type { ComponentType } from "react";
import type { Meta, StoryObj } from "@storybook/react";
import { MemoryRouter } from "react-router-dom";

import { ConversationsTab } from "./ConversationsTab";
import type { AgentDetailResponse } from "../../api/agents";
import { apiClient } from "../../api/client";
import "../../i18n";

const detail: AgentDetailResponse = {
  record: {
    id: "11111111-1111-1111-1111-111111111111",
    tenant_id: "22222222-2222-2222-2222-222222222222",
    name: "code-reviewer",
    version: "1.0.0",
    status: "active",
    spec_sha256: "a".repeat(64),
    created_by: "u",
    created_at: "2026-06-12T00:00:00Z",
    updated_at: "2026-06-12T00:00:00Z",
    spec: {},
  },
};

const items = [
  {
    thread_id: "44444444-4444-4444-4444-444444444444",
    tenant_id: detail.record.tenant_id,
    user_id: "88888888-8888-8888-8888-888888888888",
    agent_name: "code-reviewer",
    agent_version: "1.0.0",
    title: "refund question",
    status: "active",
    created_at: "2026-06-12T01:00:00Z",
    updated_at: "2026-06-12T01:05:00Z",
    run_count: 3,
    error_count: 1,
    pending_count: 1,
    last_run_at: "2026-06-12T01:05:00Z",
    tokens: {
      input_tokens: 1200,
      output_tokens: 340,
      cache_creation_tokens: 0,
      cache_read_tokens: 0,
      total_tokens: 1540,
      llm_calls: 4,
      models: ["claude-sonnet-4-5"],
    },
  },
  {
    thread_id: "44444444-4444-4444-4444-444444444445",
    tenant_id: detail.record.tenant_id,
    user_id: "99999999-9999-9999-9999-999999999999",
    agent_name: "code-reviewer",
    agent_version: "1.0.0",
    title: null,
    status: "completed",
    created_at: "2026-06-12T02:00:00Z",
    updated_at: "2026-06-12T02:03:00Z",
    run_count: 1,
    error_count: 0,
    pending_count: 0,
    last_run_at: "2026-06-12T02:03:00Z",
    tokens: null,
  },
];

/** ``GET /v1/conversations`` is an envelope endpoint — ``{success,data}``. */
function withStub() {
  return (Story: ComponentType) => {
    apiClient.defaults.adapter = (config) =>
      Promise.resolve({
        data: {
          success: true,
          data: { items, total: items.length, cross_tenant: false },
          error: null,
        },
        status: 200,
        statusText: "OK",
        headers: {},
        config,
        request: {},
      });
    return (
      <MemoryRouter>
        <Story />
      </MemoryRouter>
    );
  };
}

const meta: Meta<typeof ConversationsTab> = {
  title: "AgentDetail/ConversationsTab",
  component: ConversationsTab,
  args: { detail, memberNameOf: (id: string) => id },
};
export default meta;

type Story = StoryObj<typeof ConversationsTab>;

export const Default: Story = { decorators: [withStub()] };
