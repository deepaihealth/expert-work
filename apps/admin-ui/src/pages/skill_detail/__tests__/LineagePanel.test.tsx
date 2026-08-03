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

// Cross-tenant W4(D2)— readScope 由 SkillDetail 下传(不再自读 ambient
// scope);默认 home 态(undefined)。
function renderPanel(readScope?: string) {
  return render(
    <App>
      <LineagePanel skillId="sk-1" readScope={readScope} />
    </App>,
  );
}

beforeEach(() => {
  getMock.mockReset();
});
afterEach(() => {
  vi.clearAllMocks();
});

describe("LineagePanel", () => {
  it("threads readScope through getLineage (跨租户钻取)", async () => {
    const scope = "22222222-2222-2222-2222-222222222222";
    getMock.mockResolvedValue({
      skill: skill(),
      forked_from_source: null,
      versions: [version()],
    } satisfies SkillLineage);
    renderPanel(scope);
    await waitFor(() => expect(getMock).toHaveBeenCalledWith("sk-1", scope));
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
