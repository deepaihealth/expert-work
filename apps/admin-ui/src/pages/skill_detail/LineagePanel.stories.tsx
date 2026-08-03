import type { Meta, StoryObj } from "@storybook/react";
import { App } from "antd";

import { LineagePanel } from "./LineagePanel";
import { apiClient } from "../../api/client";
import "../../i18n";

const SKILL = {
  id: "sk-1",
  name: "researcher",
  status: "active",
  latest_version: 2,
  description: "",
  category: "research",
  pinned: false,
  last_used_at: null,
  state_changed_at: null,
  created_at: "2026-06-08T00:00:00Z",
  updated_at: "2026-06-08T00:00:00Z",
  visibility: "tenant",
};

const VERSION = {
  id: "v-1",
  skill_id: "sk-1",
  version: 1,
  prompt_fragment: "x",
  tool_names: [],
  description: "",
  category: "research",
  required_models: [],
  authored_by: "agent",
  supporting_files: {},
  lazy_load: false,
  high_risk: false,
  evolution_origin: "distilled",
  distilled_from_trajectory_key: "t/abc123.jsonl",
  created_at: "2026-06-08T00:00:00Z",
};

function stub(payload: unknown) {
  apiClient.defaults.adapter = (config) =>
    Promise.resolve({
      data: payload,
      status: 200,
      statusText: "OK",
      headers: {},
      config,
      request: {},
    });
}

const meta: Meta<typeof LineagePanel> = {
  title: "SkillDetail/LineagePanel",
  component: LineagePanel,
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
type Story = StoryObj<typeof LineagePanel>;

export const Forked: Story = {
  render: () => {
    stub({
      skill: SKILL,
      forked_from_source: { ...SKILL, id: "src-1", name: "origin-skill" },
      versions: [VERSION],
    });
    return <LineagePanel skillId="sk-1" readScope={undefined} />;
  },
};

export const NoFork: Story = {
  render: () => {
    stub({
      skill: SKILL,
      forked_from_source: null,
      versions: [{ ...VERSION, evolution_origin: null, distilled_from_trajectory_key: null }],
    });
    return <LineagePanel skillId="sk-1" readScope={undefined} />;
  },
};
