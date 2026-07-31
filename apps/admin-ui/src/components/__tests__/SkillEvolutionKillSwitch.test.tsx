/**
 * SkillEvolutionKillSwitch tests — Stream SE (SE-8-5).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { App } from "antd";
import "../../i18n";

import * as sdk from "../../api/skill-evolution";
import type { KillSwitchState } from "../../api/skill-evolution";

const useAuthMock = vi.fn();
vi.mock("../../auth/AuthContext", () => ({
  useAuth: () => useAuthMock(),
}));

let mockScope: string | undefined;
vi.mock("../../tenant/TenantScopeContext", () => ({
  SCOPE_ALL: "*",
  useTenantScope: () => ({ scope: mockScope, apiTenantScope: mockScope }),
}));

import { SkillEvolutionKillSwitch } from "../SkillEvolutionKillSwitch";

const getMock = vi.spyOn(sdk, "getKillSwitch");

function state(overrides: Partial<KillSwitchState> = {}): KillSwitchState {
  return { global: null, tenant: null, effective_halted: false, ...overrides };
}

function renderControl() {
  return render(
    <App>
      <SkillEvolutionKillSwitch />
    </App>,
  );
}

beforeEach(() => {
  getMock.mockReset();
  useAuthMock.mockReset();
  useAuthMock.mockReturnValue({ identity: { isSystemAdmin: false, roles: ["admin"] } });
});
afterEach(() => {
  mockScope = undefined;
  vi.clearAllMocks();
});

describe("SkillEvolutionKillSwitch", () => {
  it("threads the tenant scope through getKillSwitch (跨租户钻取)", async () => {
    mockScope = "22222222-2222-2222-2222-222222222222";
    getMock.mockResolvedValue(state());
    renderControl();
    await waitFor(() => expect(getMock).toHaveBeenCalledWith(mockScope));
  });

  it("shows the active status + tenant toggle for a tenant admin (no global)", async () => {
    getMock.mockResolvedValue(state());
    renderControl();
    await waitFor(() => expect(screen.getByTestId("skill-kill-switch")).toBeInTheDocument());
    expect(screen.getByTestId("skill-kill-switch-status")).toHaveTextContent(/active/i);
    expect(screen.getByTestId("skill-kill-switch-tenant")).toBeInTheDocument();
    expect(screen.queryByTestId("skill-kill-switch-global")).not.toBeInTheDocument();
  });

  it("shows the halted status when effective", async () => {
    getMock.mockResolvedValue(
      state({
        tenant: {
          id: "k1",
          scope: "tenant",
          tenant_id: "t1",
          engaged: true,
          reason: "",
          engaged_by_user_id: null,
          engaged_at: null,
          released_by_user_id: null,
          released_at: null,
          updated_at: "2026-06-08T00:00:00Z",
        },
        effective_halted: true,
      }),
    );
    renderControl();
    await waitFor(() =>
      expect(screen.getByTestId("skill-kill-switch-status")).toHaveTextContent(/halted/i),
    );
  });

  it("exposes the global toggle for a system_admin", async () => {
    useAuthMock.mockReturnValue({ identity: { isSystemAdmin: true, roles: ["system_admin"] } });
    getMock.mockResolvedValue(state());
    renderControl();
    await waitFor(() =>
      expect(screen.getByTestId("skill-kill-switch-global")).toBeInTheDocument(),
    );
  });
});
