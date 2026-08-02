import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import "../../i18n";

import { AddMcpServerDrawer } from "./AddMcpServerDrawer";
import * as catalogSdk from "../../api/mcp-catalog";

// Cross-tenant W3 — 切入态置灰;组件不挂 Provider,mock 判定 hook,
// ``isTenantSwitchedMock`` 可翻转做两态断言。
const { isTenantSwitchedMock } = vi.hoisted(() => ({
  isTenantSwitchedMock: vi.fn(() => false),
}));
vi.mock("../../tenant/useIsTenantSwitched", () => ({
  useIsTenantSwitched: isTenantSwitchedMock,
}));

const listMock = vi.spyOn(catalogSdk, "listTenantCatalog");
const enableMock = vi.spyOn(catalogSdk, "enablePlatformServer");

beforeEach(() => {
  listMock.mockReset();
  enableMock.mockReset();
  // vitest 4 的 reset 不复位 mockReturnValue — 显式归位防串台。
  isTenantSwitchedMock.mockReturnValue(false);
});

function renderDrawer(onEnabledChange: () => void) {
  return render(
    <App>
      <AddMcpServerDrawer
        open
        onClose={() => {}}
        onSaved={() => {}}
        onEnabledChange={onEnabledChange}
      />
    </App>,
  );
}

describe("AddMcpServerDrawer", () => {
  it("fires onEnabledChange after a successful enable toggle", async () => {
    listMock.mockResolvedValue([
      {
        id: "c1",
        name: "amap-maps",
        display_name: "高德地图",
        description: "",
        transport: "streamable_http",
        auth_type: "bearer",
        category: "location",
        required_tier: "free",
        entitled: true,
        tenant_enabled: false,
      },
    ] as never);
    enableMock.mockResolvedValue({} as never);
    const onEnabledChange = vi.fn();
    renderDrawer(onEnabledChange);

    const toggle = await screen.findByTestId("cb-toggle-amap-maps");
    await userEvent.click(toggle);

    await waitFor(() => expect(enableMock).toHaveBeenCalledWith("c1"));
    await waitFor(() => expect(onEnabledChange).toHaveBeenCalledTimes(1));
  });

  // Cross-tenant W3 — 切入态置灰目录启停 + 自建入口(home 态由上方用例覆盖)。
  it("切入态置灰启停开关与自建 server 入口(两态)", async () => {
    isTenantSwitchedMock.mockReturnValue(true);
    listMock.mockResolvedValue([
      {
        id: "c1",
        name: "amap-maps",
        display_name: "高德地图",
        description: "",
        transport: "streamable_http",
        auth_type: "bearer",
        category: "location",
        required_tier: "free",
        entitled: true,
        tenant_enabled: false,
      },
    ] as never);
    renderDrawer(vi.fn());

    expect(await screen.findByTestId("cb-toggle-amap-maps")).toBeDisabled();
    expect(screen.getByTestId("amsd-custom")).toBeDisabled();
  });
});
