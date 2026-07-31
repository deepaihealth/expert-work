/**
 * Storybook stories for FormView — Stream V-G.
 *
 * Two stories:
 *   - ``Default``: a blank manifest with no tools enabled.
 *   - ``WithMcpPicker``: the MCP toggle is on, two servers are available
 *     (github=tenant, fs=platform); ``listMcpServerTools("github")``
 *     returns two tools so the picker's tool collapse can be exercised.
 *
 * Mirrors the ``SettingsMcpServers.stories.tsx`` fixture decorator pattern:
 * ``setStoredToken`` + ``apiClient.defaults.adapter`` returning the full
 * ``{success,data,error}`` envelope. The adapter is URL-aware so
 * ``/v1/mcp-servers/available`` and ``/v1/mcp-servers/github/tools`` each
 * get their own response while ``/v1/model-catalog`` gets the catalog.
 */
import type { ComponentType } from "react";
import type { Meta, StoryObj } from "@storybook/react";

import { FormView } from "./FormView";
import { AuthProvider } from "../../auth/AuthContext";
import { TenantScopeProvider } from "../../tenant/TenantScopeContext";
import { apiClient, setStoredToken } from "../../api/client";
import "../../i18n";
import type { AgentManifest } from "./form_model";
import {
  setPromptJinja,
  setPromptVariables,
  setSystemPrompt,
  setTool,
} from "./form_model";

// ── JWT helper ────────────────────────────────────────────────────────────

function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.`;
}

// ── Shared fixture data ───────────────────────────────────────────────────

const CATALOG_ENVELOPE = {
  success: true,
  data: {
    providers: [
      {
        provider: "openai",
        models: [
          {
            name: "gpt-4o",
            vision: true,
            embeddings: false,
            context_window: 128000,
            deprecated: false,
          },
        ],
      },
    ],
  },
  error: null,
};

const AVAILABLE_SERVERS_ENVELOPE = {
  success: true,
  data: [
    { name: "github", source: "tenant", enabled: true },
    { name: "fs", source: "platform", enabled: true },
  ],
  error: null,
};

const GITHUB_TOOLS_ENVELOPE = {
  success: true,
  data: [
    { name: "create_issue", description: "Create a new GitHub issue" },
    { name: "list_repos", description: "List repositories" },
  ],
  error: null,
};

// ``/v1/skills`` returns a RAW payload (no envelope) — see api/skills.ts.
const SKILLS_RAW = {
  items: [
    {
      id: "s1",
      name: "pptx-builder",
      status: "active",
      latest_version: 3,
      description: "Generate PowerPoint decks from an outline.",
      category: "office",
      pinned: false,
      last_used_at: null,
      state_changed_at: null,
      created_at: "",
      updated_at: "",
      source: "tenant",
    },
  ],
  platform_items: [
    {
      id: "s2",
      name: "sql-analyst",
      status: "active",
      latest_version: 1,
      description: "Run read-only SQL and summarise results.",
      category: "data",
      pinned: false,
      last_used_at: null,
      state_changed_at: null,
      created_at: "",
      updated_at: "",
      source: "platform",
      entitled: true,
    },
    {
      id: "s3",
      name: "premium-forecaster",
      status: "active",
      latest_version: 1,
      description: "Time-series forecasting (enterprise tier).",
      category: "ml",
      pinned: false,
      last_used_at: null,
      state_changed_at: null,
      created_at: "",
      updated_at: "",
      source: "platform",
      entitled: false,
      required_tier: "enterprise",
    },
  ],
  next_cursor: null,
  cross_tenant: false,
};

// ── Decorator factory ─────────────────────────────────────────────────────

function withMcpFixture(Story: ComponentType) {
  setStoredToken(makeJwt({ sub: "u1", tenant_id: "t1", roles: ["admin"] }));
  apiClient.defaults.adapter = (config) => {
    const url = typeof config.url === "string" ? config.url : "";
    let data: unknown = CATALOG_ENVELOPE;
    if (url.includes("/v1/mcp-servers/available")) {
      data = AVAILABLE_SERVERS_ENVELOPE;
    } else if (url.match(/\/v1\/mcp-servers\/github\/tools/)) {
      data = GITHUB_TOOLS_ENVELOPE;
    } else if (url.match(/\/v1\/mcp-servers\/[^/]+\/tools/)) {
      data = { success: true, data: [], error: null };
    } else if (url.includes("/v1/skills")) {
      data = SKILLS_RAW;
    } else if (url.includes("/v1/model-catalog")) {
      data = CATALOG_ENVELOPE;
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
  // Cross-tenant W3 — the skills/mcp/knowledge pickers consume
  // useTenantScope (throws without a provider). Routing comes from the
  // global preview decorator, so no MemoryRouter here.
  return (
    <AuthProvider>
      <TenantScopeProvider>
        <Story />
      </TenantScopeProvider>
    </AuthProvider>
  );
}

// ── Seed manifest helpers ─────────────────────────────────────────────────

const BLANK_MANIFEST: AgentManifest = {
  apiVersion: "expert_work/v1",
  kind: "Agent",
  metadata: { name: "" },
  spec: {},
};

const MCP_ON_MANIFEST: AgentManifest = (() => {
  const withMcp = setTool(BLANK_MANIFEST, "mcp", true) as AgentManifest;
  return { ...withMcp, metadata: { name: "mcp-agent" } };
})();

const JINJA_PROMPT_MANIFEST: AgentManifest = (() => {
  let m = setSystemPrompt(
    BLANK_MANIFEST,
    "You are helping {{ user_name }}.\n{% if topic %}Focus on {{ topic }}.{% endif %}",
  ) as AgentManifest;
  m = setPromptJinja(m, true) as AgentManifest;
  m = setPromptVariables(m, [
    {
      name: "user_name",
      trusted: true,
      required: true,
      description: "caller name",
    },
    {
      name: "topic",
      trusted: false,
      required: false,
      description: "optional focus",
    },
  ]) as AgentManifest;
  return { ...m, metadata: { name: "jinja-agent" } };
})();

// ── Meta ──────────────────────────────────────────────────────────────────

const meta: Meta<typeof FormView> = {
  title: "ManifestEditor/FormView",
  component: FormView,
  parameters: { layout: "padded" },
};

export default meta;

type Story = StoryObj<typeof FormView>;

// ── Stories ───────────────────────────────────────────────────────────────

/** Blank manifest — shows the curated sections with no tools enabled. */
export const Default: Story = {
  decorators: [withMcpFixture],
  args: {
    formData: BLANK_MANIFEST,
    onChange: () => {},
    section: "basic",
  },
};

/**
 * MCP toggle is ON; two servers are available (github=tenant, fs=platform).
 * Checking the github server and expanding its Tools collapse fetches
 * create_issue + list_repos (mocked). Demonstrates the McpToolPicker
 * in its live-data state.
 */
export const WithMcpPicker: Story = {
  decorators: [withMcpFixture],
  args: {
    formData: MCP_ON_MANIFEST,
    onChange: () => {},
    section: "mcp",
  },
};

/**
 * Jinja mode is ON — the prompt template renders in the Monaco editor with
 * ``{{ }}`` / ``{% %}`` highlighting; declared variables (user_name, topic)
 * drive the autocomplete and the variable rows below.
 */
export const JinjaPrompt: Story = {
  decorators: [withMcpFixture],
  args: {
    formData: JINJA_PROMPT_MANIFEST,
    onChange: () => {},
    section: "prompt",
  },
};

/**
 * The Skills tab — tenant + platform skills merged, each row showing
 * description / source badge / category, with the enterprise-tier skill
 * locked (shown but not selectable).
 */
export const SkillsRich: Story = {
  decorators: [withMcpFixture],
  args: {
    formData: { ...BLANK_MANIFEST, metadata: { name: "skilled-agent" } },
    onChange: () => {},
    section: "skills",
  },
};

// The former ``WithReflectionEvaluator`` story is gone: the evaluator model
// picker moved out of FormView's "model" section into ModelRoutingSection's
// reflection panel (rendered only while reflection is on).
