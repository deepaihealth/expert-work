import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { App } from "antd";
import "../i18n";

// Cross-tenant W3 — 切入态置灰;组件不挂 Provider,mock 判定 hook,
// ``isTenantSwitchedMock`` 可翻转做两态断言。
const { isTenantSwitchedMock } = vi.hoisted(() => ({
  isTenantSwitchedMock: vi.fn(() => false),
}));
vi.mock("../tenant/useIsTenantSwitched", () => ({
  useIsTenantSwitched: isTenantSwitchedMock,
}));

import { collectHeaders, CreateMcpServerDrawer } from "./CreateMcpServerDrawer";

describe("collectHeaders", () => {
  it("returns undefined when there are no rows", () => {
    expect(collectHeaders(undefined)).toBeUndefined();
    expect(collectHeaders([])).toBeUndefined();
  });

  it("skips incomplete rows (blank key or value)", () => {
    expect(
      collectHeaders([
        { key: "", value: "v" },
        { key: "X-Org", value: "  " },
      ]),
    ).toBeUndefined();
  });

  it("collects complete rows and trims the key", () => {
    expect(
      collectHeaders([
        { key: " X-API-Key ", value: "secret" },
        { key: "X-Org", value: "acme" },
        { key: "X-Blank", value: "" },
      ]),
    ).toEqual({ "X-API-Key": "secret", "X-Org": "acme" });
  });
});

describe("CreateMcpServerDrawer 切入态置灰 (W3)", () => {
  beforeEach(() => {
    // vitest 4 的 restore 不复位 mockReturnValue — 显式归位防串台。
    isTenantSwitchedMock.mockReturnValue(false);
  });

  function renderDrawer() {
    return render(
      <App>
        <CreateMcpServerDrawer open onClose={() => {}} onSaved={() => {}} />
      </App>,
    );
  }

  it("home 态提交可用;切入态置灰(两态)", () => {
    const first = renderDrawer();
    expect(screen.getByTestId("cms-submit")).toBeEnabled();
    first.unmount();

    isTenantSwitchedMock.mockReturnValue(true);
    renderDrawer();
    expect(screen.getByTestId("cms-submit")).toBeDisabled();
  });
});
