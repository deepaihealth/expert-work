import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { App } from "antd";
import "../../i18n";
import i18n from "../../i18n";

import { SettingsMcpServers } from "../SettingsMcpServers";
import * as serversSdk from "../../api/mcp-servers";
import type { McpServer } from "../../api/mcp-servers";

// Cross-tenant W3 — the page reads the ambient tenant scope; these tests
// don't mount a TenantScopeProvider, so mock it (switchable per test;
// undefined = home state).
let mockScope: string | undefined;
vi.mock("../../tenant/TenantScopeContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../tenant/TenantScopeContext")>()),
  useTenantScope: () => ({
    scope: mockScope ?? "home",
    setScope: () => {},
    apiTenantScope: mockScope,
  }),
}));

const listMock = vi.spyOn(serversSdk, "listMcpServers");
const availMock = vi.spyOn(serversSdk, "listAvailableMcpServers");
const toolsMock = vi.spyOn(serversSdk, "listMcpServerTools");

// The default test-time locale resolves to "en" (jsdom's navigator.language
// is "en-US"; see AgentsList.test.tsx for the same forced-locale idiom) —
// force zh-CN so the source-tag assertions below ("平台"/"自定义") are
// deterministic regardless of the jsdom-detected browser language.
beforeEach(async () => {
  await i18n.changeLanguage("zh-CN");
  listMock.mockReset();
  availMock.mockReset();
  toolsMock.mockReset();
  mockScope = undefined;
});

const custom: McpServer = {
  id: "s1",
  name: "my-custom",
  transport: "sse",
  url: "https://x.example.com/sse",
  auth_type: "bearer",
  timeout_s: 30,
  enabled: true,
  created_at: "",
  updated_at: "",
};

function renderPage() {
  return render(
    <MemoryRouter>
      <App>
        <SettingsMcpServers />
      </App>
    </MemoryRouter>,
  );
}

describe("SettingsMcpServers unified list", () => {
  it("renders platform and custom rows with source tags", async () => {
    listMock.mockResolvedValue([custom]);
    availMock.mockResolvedValue([
      { name: "amap", source: "platform", display_name: "高德地图", auth_type: "none", catalog_id: "c1" },
    ]);
    renderPage();
    expect(await screen.findByText("高德地图")).toBeInTheDocument();
    expect(screen.getByText("my-custom")).toBeInTheDocument();
    expect(screen.getAllByText("平台").length).toBeGreaterThan(0);
    expect(screen.getAllByText("自定义").length).toBeGreaterThan(0);
  });

  it("oauth2 platform row shows the authorize link and hides Test", async () => {
    listMock.mockResolvedValue([]);
    availMock.mockResolvedValue([
      { name: "gh", source: "platform", display_name: "GitHub", auth_type: "oauth2", catalog_id: "c2" },
    ]);
    renderPage();
    expect(await screen.findByTestId("ms-authorize-gh")).toBeInTheDocument();
    expect(screen.queryByTestId("ms-test-gh")).not.toBeInTheDocument();
  });

  // Regression: oauth2 platform rows have no working probe endpoint (the
  // backend 409s listMcpServerTools for them), so there must be no way to
  // trigger one. Without `rowExpandable`, every row — including this one —
  // got an expand chevron whose `onExpand` unconditionally called probe(),
  // flipping the green "已启用（平台）" badge to a red "无法连接" tag that
  // reload() never resets.
  it("oauth2 platform row's expand control can never trigger a probe", async () => {
    listMock.mockResolvedValue([]);
    availMock.mockResolvedValue([
      { name: "gh", source: "platform", display_name: "GitHub", auth_type: "oauth2", catalog_id: "c2" },
    ]);
    const { container } = renderPage();
    await screen.findByTestId("ms-authorize-gh");

    // antd always renders the expand control as a
    // `<button class="ant-table-row-expand-icon">` (even for
    // non-expandable rows); `rowExpandable` returning false adds the
    // `-spaced` modifier (no chevron, visibility:hidden) instead of
    // `-collapsed`/`-expanded`. There is exactly one row in this fixture.
    const expandIcon = container.querySelector(".ant-table-row-expand-icon");
    expect(expandIcon).not.toBeNull();
    expect(expandIcon).toHaveClass("ant-table-row-expand-icon-spaced");

    // antd still wires an onClick on that button regardless of
    // `rowExpandable` — so the real guarantee has to hold even if it's
    // clicked. This is the assertion that actually fails a 409 on revert.
    fireEvent.click(expandIcon as Element);
    expect(toolsMock).not.toHaveBeenCalled();
  });

  // Regression: a platform row whose catalog entry was deleted while still
  // allowlisted (`buildUnifiedRows` sets catalogId=null, authType="none")
  // used to show a Test button that 404s and a Remove button whose handler
  // silently no-ops. Neither is a real affordance, so both must be hidden
  // or disabled instead.
  it("degraded platform row (catalog deleted) hides Test/authorize and disables Remove", async () => {
    listMock.mockResolvedValue([]);
    availMock.mockResolvedValue([{ name: "ghost", source: "platform", display_name: "Ghost" }]);
    renderPage();
    await screen.findByText("Ghost");

    expect(screen.queryByTestId("ms-test-ghost")).not.toBeInTheDocument();
    expect(screen.queryByTestId("ms-authorize-ghost")).not.toBeInTheDocument();

    const removeButton = screen.getByTestId("ms-remove-ghost");
    expect(removeButton).toBeDisabled();
  });

  // Regression: /available (platform allowlist) is supplementary. If it fails,
  // the page must still render the tenant's custom servers — a Promise.all
  // rejection used to blank the whole table (this is what took down the E2E
  // spec, whose fixtures didn't stub the new /available call).
  it("still renders custom servers when /available fails", async () => {
    listMock.mockResolvedValue([custom]);
    availMock.mockRejectedValue(new Error("available down"));
    renderPage();
    expect(await screen.findByText("my-custom")).toBeInTheDocument();
    expect(screen.queryByTestId("ms-error")).not.toBeInTheDocument();
  });

  // The Test button is an explicit "re-probe now" action: clicking it must
  // always re-hit the tools endpoint, even after the row is already
  // "connected" (cached). The passive row-expand path keeps the cache; only
  // the button forces. Regression: the cache guard used to swallow every
  // click after the first, leaving the button a silent no-op.
  it("Test button force re-probes even when the row is already connected", async () => {
    listMock.mockResolvedValue([custom]);
    availMock.mockResolvedValue([]);
    toolsMock.mockResolvedValue([{ name: "t1", description: "" }]);
    renderPage();
    const btn = await screen.findByTestId("ms-test-my-custom");

    fireEvent.click(btn);
    // Wait until probe #1 resolves and the row renders "connected" (the
    // button leaves its loading state) before clicking again.
    await screen.findByText(/已连接/);
    expect(toolsMock).toHaveBeenCalledTimes(1);
    // Row is now cached "connected". Wait for the Test button to leave its
    // loading state (antd re-renders the action cell once the probe resolves)
    // — antd Button swallows clicks while loading — then click again.
    await waitFor(() =>
      expect(screen.getByTestId("ms-test-my-custom")).not.toHaveClass("ant-btn-loading"),
    );
    fireEvent.click(screen.getByTestId("ms-test-my-custom"));
    await waitFor(() => expect(toolsMock).toHaveBeenCalledTimes(2));
  });

  it("threads the ambient tenant scope into both list reads (W3)", async () => {
    mockScope = "22222222-2222-2222-2222-222222222222";
    listMock.mockResolvedValue([custom]);
    availMock.mockResolvedValue([]);

    renderPage();

    await waitFor(() => expect(listMock).toHaveBeenCalledWith(mockScope));
    expect(availMock).toHaveBeenCalledWith(mockScope);
  });
});
