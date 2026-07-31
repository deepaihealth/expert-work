/**
 * LineagePanel tests — Stream SE (SE-8-5).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { App } from "antd";
import "../../../i18n";

import * as sdk from "../../../api/skill-evolution";
import type { SkillLineage } from "../../../api/skill-evolution";
import type { SkillRecord, SkillVersion } from "../../../api/skills";
import { LineagePanel } from "../LineagePanel";

let mockScope: string | undefined;
vi.mock("../../../tenant/TenantScopeContext", () => ({
  SCOPE_ALL: "*",
  useTenantScope: () => ({ scope: mockScope, apiTenantScope: mockScope }),
}));

const getMock = vi.spyOn(sdk, "getLineage");

function skill(overrides: Partial<SkillRecord> = {}): SkillRecord {
  return {
    id: "sk-1",
    name: "researcher",
    status: "active",
    latest_version: 1,
    description: "",
    category: "research",
    pinned: false,
    last_used_at: null,
    state_changed_at: null,
    created_at: "2026-06-08T00:00:00Z",
    updated_at: "2026-06-08T00:00:00Z",
    visibility: "tenant",
    ...overrides,
  };
}

function version(overrides: Partial<SkillVersion> = {}): SkillVersion {
  return {
    id: "v-1",
    skill_id: "sk-1",
    version: 1,
    prompt_fragment: "x",
    tool_names: [],
    description: "",
    category: "",
    required_models: [],
    authored_by: "agent",
    supporting_files: {},
    lazy_load: false,
    high_risk: false,
    evolution_origin: "distilled",
    created_at: "2026-06-08T00:00:00Z",
    ...overrides,
  };
}

function renderPanel() {
  return render(
    <App>
      <LineagePanel skillId="sk-1" />
    </App>,
  );
}

beforeEach(() => {
  getMock.mockReset();
});
afterEach(() => {
  mockScope = undefined;
  vi.clearAllMocks();
});

describe("LineagePanel", () => {
  it("threads the tenant scope through getLineage (跨租户钻取)", async () => {
    mockScope = "22222222-2222-2222-2222-222222222222";
    getMock.mockResolvedValue({
      skill: skill(),
      forked_from_source: null,
      versions: [version()],
    } satisfies SkillLineage);
    renderPanel();
    await waitFor(() => expect(getMock).toHaveBeenCalledWith("sk-1", mockScope));
  });

  it("renders the version timeline with origin tags", async () => {
    getMock.mockResolvedValue({
      skill: skill(),
      forked_from_source: null,
      versions: [version()],
    } satisfies SkillLineage);
    renderPanel();
    await waitFor(() => expect(screen.getByTestId("skill-lineage-versions")).toBeInTheDocument());
    expect(screen.getByTestId("skill-lineage-versions")).toHaveTextContent(/v1/);
    expect(screen.queryByTestId("skill-lineage-fork")).not.toBeInTheDocument();
  });

  it("renders the fork edge when forked", async () => {
    getMock.mockResolvedValue({
      skill: skill({ forked_from: "src-1" }),
      forked_from_source: skill({ id: "src-1", name: "origin-skill" }),
      versions: [version({ evolution_origin: "in_session" })],
    } satisfies SkillLineage);
    renderPanel();
    await waitFor(() => expect(screen.getByTestId("skill-lineage-fork")).toBeInTheDocument());
    expect(screen.getByTestId("skill-lineage-fork")).toHaveTextContent(/origin-skill/);
  });
});
