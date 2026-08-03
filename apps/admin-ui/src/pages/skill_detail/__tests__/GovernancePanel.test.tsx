/**
 * GovernancePanel tests — Stream SE (SE-8-4).
 *
 * The skill-evolution SDK is mocked (it would otherwise hit fetch); we drive
 * the pending-request state from the test body and assert the propose /
 * approve / reject affordances + visibility badge.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import "../../../i18n";

import * as sdk from "../../../api/skill-evolution";
import type { PromoteRequest } from "../../../api/skill-evolution";
import type { SkillRecord } from "../../../api/skills";

import { GovernancePanel } from "../GovernancePanel";

const listMock = vi.spyOn(sdk, "listPromoteRequests");
const requestMock = vi.spyOn(sdk, "requestPromote");
const approveMock = vi.spyOn(sdk, "approvePromote");

function skill(overrides: Partial<SkillRecord> = {}): SkillRecord {
  return {
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
    ...overrides,
  };
}

function pending(): PromoteRequest {
  return {
    id: "req-1",
    tenant_id: "t1",
    skill_id: "sk-1",
    skill_version: 2,
    status: "pending",
    requested_by_user_id: null,
    requested_by_agent_name: "assistant",
    reason: "",
    decided_by_user_id: null,
    decided_at: null,
    decision_reason: "",
    created_at: "2026-06-08T00:00:00Z",
  };
}

type PanelProps = Parameters<typeof GovernancePanel>[0];

// Cross-tenant W4(D2)— readScope/readonly 由 SkillDetail 下传;默认 home 态
// (readScope undefined、readonly false),跨租户/只读用例自行覆盖。
function renderPanel(
  props: Omit<PanelProps, "readScope" | "readonly"> &
    Partial<Pick<PanelProps, "readScope" | "readonly">>,
) {
  return render(
    <App>
      <GovernancePanel readScope={undefined} readonly={false} {...props} />
    </App>,
  );
}

beforeEach(() => {
  listMock.mockReset();
  requestMock.mockReset();
  approveMock.mockReset();
  if (typeof window !== "undefined") window.localStorage.clear();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("GovernancePanel", () => {
  // Cross-tenant W4(D2)— pending 列表读把 readScope 透传给 SDK。
  it("threads readScope through listPromoteRequests (跨租户钻取)", async () => {
    const scope = "22222222-2222-2222-2222-222222222222";
    listMock.mockResolvedValue({ items: [], next_cursor: null, cross_tenant: false });
    renderPanel({ skill: skill(), isAdmin: false, onChanged: vi.fn(), readScope: scope });

    await waitFor(() =>
      expect(listMock).toHaveBeenCalledWith({ status: "pending", tenantScope: scope }),
    );
  });

  it("shows the propose button for an agent_private skill with no pending request", async () => {
    listMock.mockResolvedValue({ items: [], next_cursor: null, cross_tenant: false });
    renderPanel({ skill: skill(), isAdmin: false, onChanged: vi.fn() });

    expect(screen.getByTestId("skill-visibility-badge")).toHaveTextContent(/agent/i);
    await waitFor(() =>
      expect(screen.getByTestId("skill-propose-button")).toBeInTheDocument(),
    );
  });

  it("proposing calls requestPromote with the latest version", async () => {
    const user = userEvent.setup();
    listMock.mockResolvedValue({ items: [], next_cursor: null, cross_tenant: false });
    requestMock.mockResolvedValue(pending());
    renderPanel({ skill: skill(), isAdmin: false, onChanged: vi.fn() });

    await waitFor(() => screen.getByTestId("skill-propose-button"));
    await user.click(screen.getByTestId("skill-propose-button"));
    expect(requestMock).toHaveBeenCalledWith("sk-1", { skill_version: 2 });
  });

  it("renders approve/reject for an admin when a request is pending", async () => {
    listMock.mockResolvedValue({ items: [pending()], next_cursor: null, cross_tenant: false });
    renderPanel({ skill: skill(), isAdmin: true, onChanged: vi.fn() });

    await waitFor(() => expect(screen.getByTestId("skill-pending-promotion")).toBeInTheDocument());
    expect(screen.getByTestId("skill-approve-button")).toBeInTheDocument();
    expect(screen.getByTestId("skill-reject-button")).toBeInTheDocument();
    expect(screen.queryByTestId("skill-propose-button")).not.toBeInTheDocument();
  });

  // Cross-tenant W4(D2)— promote 链路不带 scope,readonly(切入态/"*" 聚合
  // 深链外租户读)由 SkillDetail 下传置灰(上方用例覆盖 home 态可点)。
  it("readonly 态置灰提议/批准/驳回(两态)", async () => {
    listMock.mockResolvedValue({ items: [], next_cursor: null, cross_tenant: false });
    const first = renderPanel({
      skill: skill(),
      isAdmin: false,
      onChanged: vi.fn(),
      readonly: true,
    });
    await waitFor(() =>
      expect(screen.getByTestId("skill-propose-button")).toBeDisabled(),
    );
    first.unmount();

    listMock.mockResolvedValue({ items: [pending()], next_cursor: null, cross_tenant: false });
    renderPanel({ skill: skill(), isAdmin: true, onChanged: vi.fn(), readonly: true });
    await waitFor(() =>
      expect(screen.getByTestId("skill-approve-button")).toBeDisabled(),
    );
    expect(screen.getByTestId("skill-reject-button")).toBeDisabled();
  });

  it("hides approve/reject for a non-admin even when pending", async () => {
    listMock.mockResolvedValue({ items: [pending()], next_cursor: null, cross_tenant: false });
    renderPanel({ skill: skill(), isAdmin: false, onChanged: vi.fn() });

    await waitFor(() => expect(screen.getByTestId("skill-pending-promotion")).toBeInTheDocument());
    expect(screen.queryByTestId("skill-approve-button")).not.toBeInTheDocument();
  });

  it("shows the tenant badge for a tenant-visible skill", async () => {
    listMock.mockResolvedValue({ items: [], next_cursor: null, cross_tenant: false });
    renderPanel({ skill: skill({ visibility: "tenant" }), isAdmin: true, onChanged: vi.fn() });
    expect(screen.getByTestId("skill-visibility-badge")).toHaveTextContent(/tenant/i);
    expect(screen.queryByTestId("skill-propose-button")).not.toBeInTheDocument();
  });
});
