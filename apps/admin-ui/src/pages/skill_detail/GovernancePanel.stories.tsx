import type { Meta, StoryObj } from "@storybook/react";
import { App } from "antd";

import { GovernancePanel } from "./GovernancePanel";
import { apiClient } from "../../api/client";
import type { SkillRecord } from "../../api/skills";
import "../../i18n";

function stubPromoteRequests(items: unknown[]) {
  apiClient.defaults.adapter = (config) =>
    Promise.resolve({
      data: { items, next_cursor: null, cross_tenant: false },
      status: 200,
      statusText: "OK",
      headers: {},
      config,
      request: {},
    });
}

const BASE: SkillRecord = {
  id: "sk-1",
  name: "researcher",
  status: "draft",
  latest_version: 2,
  description: "",
  category: "research",
  pinned: false,
  last_used_at: null,
  state_changed_at: null,
  created_at: "2026-06-08T00:00:00Z",
  updated_at: "2026-06-08T00:00:00Z",
  visibility: "agent_private",
  created_by_agent_name: "assistant",
};

const meta: Meta<typeof GovernancePanel> = {
  title: "SkillDetail/GovernancePanel",
  component: GovernancePanel,
  decorators: [
    (Story) => (
      <App>
        <div style={{ maxWidth: 720 }}>
          <Story />
        </div>
      </App>
    ),
  ],
};

export default meta;
type Story = StoryObj<typeof GovernancePanel>;

export const AgentPrivateNoPending: Story = {
  render: () => {
    stubPromoteRequests([]);
    return <GovernancePanel skill={BASE} isAdmin={false} onChanged={() => {}} readScope={undefined} />;
  },
};

export const PendingForAdmin: Story = {
  render: () => {
    stubPromoteRequests([
      {
        id: "req-1",
        tenant_id: "t1",
        skill_id: "sk-1",
        skill_version: 2,
        status: "pending",
        requested_by_user_id: null,
        requested_by_agent_name: "assistant",
        reason: "useful tenant-wide",
        decided_by_user_id: null,
        decided_at: null,
        decision_reason: "",
        created_at: "2026-06-08T00:00:00Z",
      },
    ]);
    return <GovernancePanel skill={BASE} isAdmin onChanged={() => {}} readScope={undefined} />;
  },
};

export const TenantVisible: Story = {
  render: () => {
    stubPromoteRequests([]);
    return (
      <GovernancePanel
        skill={{ ...BASE, visibility: "tenant" }}
        isAdmin
        onChanged={() => {}}
        readScope={undefined}
      />
    );
  },
};
