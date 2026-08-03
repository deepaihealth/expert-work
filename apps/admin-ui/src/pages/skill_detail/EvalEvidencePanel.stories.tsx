import type { Meta, StoryObj } from "@storybook/react";
import { App } from "antd";

import { EvalEvidencePanel } from "./EvalEvidencePanel";
import { apiClient } from "../../api/client";
import "../../i18n";

function stub(items: unknown[]) {
  apiClient.defaults.adapter = (config) =>
    Promise.resolve({
      data: { items },
      status: 200,
      statusText: "OK",
      headers: {},
      config,
      request: {},
    });
}

const meta: Meta<typeof EvalEvidencePanel> = {
  title: "SkillDetail/EvalEvidencePanel",
  component: EvalEvidencePanel,
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
type Story = StoryObj<typeof EvalEvidencePanel>;

export const WithEvidence: Story = {
  render: () => {
    stub([
      {
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
      },
    ]);
    return <EvalEvidencePanel skillId="sk-1" readScope={undefined} />;
  },
};

export const Empty: Story = {
  render: () => {
    stub([]);
    return <EvalEvidencePanel skillId="sk-1" readScope={undefined} />;
  },
};
