import type { ComponentType } from "react";
import type { Meta, StoryObj } from "@storybook/react";
import { MemoryRouter } from "react-router-dom";

import { HistoryTab } from "./HistoryTab";
import type { AgentDetailResponse } from "../../api/agents";
import { apiClient } from "../../api/client";
import "../../i18n";

const SHA_V1 = "a".repeat(64);
const SHA_V2 = "b".repeat(64);

const detail: AgentDetailResponse = {
  record: {
    id: "11111111-1111-1111-1111-111111111111",
    tenant_id: "22222222-2222-2222-2222-222222222222",
    name: "demo-agent",
    version: "1.0.0",
    status: "active",
    spec_sha256: SHA_V2,
    created_by: "u",
    created_at: "2026-06-11T00:00:00Z",
    updated_at: "2026-06-11T09:00:00Z",
    spec: {},
  },
};

/** Stub the axios layer: the list call resolves two revisions; the
 *  per-revision snapshot calls resolve small manifests so selecting two
 *  rows renders a real Monaco diff in the story. */
function withStubs(Story: ComponentType) {
  apiClient.defaults.adapter = (config) => {
    const url = config.url ?? "";
    const respond = (data: unknown) =>
      Promise.resolve({
        data: { success: true, data },
        status: 200,
        statusText: "OK",
        headers: {},
        config,
        request: {},
      });
    if (/\/revisions\/\d+$/.test(url)) {
      const revision = Number(url.split("/").pop());
      return respond({
        record: {
          revision,
          spec_sha256: revision === 1 ? SHA_V1 : SHA_V2,
          actor_id: revision === 1 ? "bob" : "alice",
          created_at: "2026-06-11T00:00:00Z",
          spec: {
            apiVersion: "expert_work.io/v1",
            kind: "Agent",
            spec: {
              system_prompt: {
                template: revision === 1 ? "you are a reviewer" : "you are a strict reviewer",
              },
            },
          },
        },
      });
    }
    return respond({
      items: [
        {
          revision: 2,
          spec_sha256: SHA_V2,
          actor_id: "alice",
          created_at: "2026-06-11T09:00:00Z",
        },
        {
          revision: 1,
          spec_sha256: SHA_V1,
          actor_id: "bob",
          created_at: "2026-06-11T00:00:00Z",
        },
      ],
    });
  };
  return (
    <MemoryRouter>
      <Story />
    </MemoryRouter>
  );
}

const meta: Meta<typeof HistoryTab> = {
  title: "Pages/AgentDetail/HistoryTab",
  component: HistoryTab,
  decorators: [withStubs],
};
export default meta;

type Story = StoryObj<typeof HistoryTab>;

export const TwoRevisions: Story = {
  args: { detail, onRolledBack: () => {}, memberNameOf: (id: string) => id },
};
