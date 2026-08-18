import type { ComponentType } from "react";
import type { Meta, StoryObj } from "@storybook/react";
import { MemoryRouter } from "react-router-dom";
import { App } from "antd";

import { PlaygroundTab } from "./PlaygroundTab";
import type { AgentDetailResponse } from "../../api/agents";
import { apiClient, setStoredToken } from "../../api/client";
import { AuthProvider } from "../../auth/AuthContext";
import "../../i18n";

function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.`;
}

const detail: AgentDetailResponse = {
  record: {
    id: "11111111-1111-1111-1111-111111111111",
    tenant_id: "22222222-2222-2222-2222-222222222222",
    name: "demo-agent",
    version: "1.0.0",
    status: "active",
    spec_sha256: "abc",
    created_by: "u",
    created_at: "2026-05-25T00:00:00Z",
    updated_at: "2026-05-25T00:00:00Z",
    spec: {},
  },
};

/** Stub the axios layer so the on-mount ``createSession`` resolves and an
 *  upload returns a fake ref — the Playground renders its ready state with
 *  the attach-image affordance visible. The SSE run uses ``fetch`` (not
 *  axios) so it stays inert in Storybook, which is fine for the visual. */
function withStubs(Story: ComponentType) {
  setStoredToken(
    makeJwt({ sub: "u", tenant_id: "22222222-2222-2222-2222-222222222222", roles: [] }),
  );
  apiClient.defaults.adapter = (config) => {
    const url = config.url ?? "";
    if (url.includes("/workspace")) {
      return Promise.resolve({
        data: {
          success: true,
          data: {
            workspace: {
              id: "w1",
              tenant_id: "22222222-2222-2222-2222-222222222222",
              user_id: "u-1",
              volume_name: "expert-work-ws-demo",
              size_bytes: 1048576,
              size_limit_bytes: 10737418240,
              created_at: "2026-05-25T00:00:00Z",
              last_accessed_at: "2026-05-25T00:00:00Z",
              deleted_at: null,
              archived_object_key: null,
            },
            artifacts: [
              {
                name: "summary.md",
                kind: "document",
                latest_version: 1,
                created_at: null,
                updated_at: null,
              },
            ],
          },
          error: null,
        },
        status: 200,
        statusText: "OK",
        headers: {},
        config,
        request: {},
      });
    }
    if (url.includes("/v1/members")) {
      return Promise.resolve({
        data: {
          success: true,
          data: {
            items: [
              {
                id: "m1",
                tenant_id: "22222222-2222-2222-2222-222222222222",
                email: "alice@acme.test",
                display_name: "Alice",
                role: "member",
                status: "active",
                keycloak_user_id: "kc-1",
                subject_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                invited_by: "u",
                invited_at: null,
                activated_at: null,
                updated_at: null,
              },
            ],
            total: 1,
          },
          error: null,
        },
        status: 200,
        statusText: "OK",
        headers: {},
        config,
        request: {},
      });
    }
    // Left rail — ``listSessions`` (GET /v1/sessions) vs ``createSession``
    // (POST /v1/sessions): only the list method returns an ``items`` page.
    if (url.endsWith("/v1/sessions") && (config.method ?? "get") === "get") {
      return Promise.resolve({
        data: {
          success: true,
          data: {
            items: [
              {
                thread_id: "33333333-3333-3333-3333-333333333333",
                tenant_id: "22222222-2222-2222-2222-222222222222",
                agent_name: "demo-agent",
                agent_version: "1.0.0",
                user_id: null,
                status: "active",
                title: "上周的销售复盘",
                created_by: "u",
                created_at: "2026-05-24T00:00:00Z",
                updated_at: "2026-05-25T00:00:00Z",
              },
              {
                thread_id: "44444444-4444-4444-4444-444444444444",
                tenant_id: "22222222-2222-2222-2222-222222222222",
                agent_name: "demo-agent",
                agent_version: "1.0.0",
                user_id: null,
                status: "active",
                title: null,
                created_by: "u",
                created_at: "2026-05-20T00:00:00Z",
                updated_at: "2026-05-20T00:00:00Z",
              },
            ],
            total: 2,
          },
          error: null,
        },
        status: 200,
        statusText: "OK",
        headers: {},
        config,
        request: {},
      });
    }
    if (url.endsWith("/uploads")) {
      return Promise.resolve({
        data: { image_ref: "expert_work://image/demo.png" },
        status: 201,
        statusText: "Created",
        headers: {},
        config,
        request: {},
      });
    }
    return Promise.resolve({
      data: {
        success: true,
        data: {
          thread_id: "33333333-3333-3333-3333-333333333333",
          tenant_id: "22222222-2222-2222-2222-222222222222",
          agent_name: "demo-agent",
          agent_version: "1.0.0",
          user_id: null,
          status: "active",
          created_by: "u",
          created_at: "2026-05-25T00:00:00Z",
          updated_at: "2026-05-25T00:00:00Z",
        },
        error: null,
      },
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
        <App>
          <Story />
        </App>
      </AuthProvider>
    </MemoryRouter>
  );
}

const meta: Meta<typeof PlaygroundTab> = {
  title: "Pages/AgentDetail/PlaygroundTab",
  component: PlaygroundTab,
  decorators: [withStubs],
};
export default meta;

type Story = StoryObj<typeof PlaygroundTab>;

export const Ready: Story = {
  args: { detail },
};
