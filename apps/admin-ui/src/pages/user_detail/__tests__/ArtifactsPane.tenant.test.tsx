/** B-239 —— 系统管理员切入别的租户看用户详情「产物」页:列表 / 版本 / 下载都带上切入的
 *  租户(不带就查成自己租户、显示「该用户暂无产物」);删除 / 改类型的接口不收租户,切入态置灰。 */
import { App } from "antd";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "../../../i18n";

import * as artifactsSdk from "../../../api/artifacts";
import { ArtifactsPane } from "../ArtifactsPane";

const TENANT = "11111111-2222-3333-4444-555555555555";
const scopeRef = vi.hoisted(() => ({ current: undefined as string | undefined }));
const switchedRef = vi.hoisted(() => ({ current: false }));

vi.mock("../../../tenant/TenantScopeContext", async (importOriginal) => {
  const { mockTenantScopeModule } = await import("../../../test-utils/tenantScopeMock");
  return mockTenantScopeModule(
    await importOriginal<typeof import("../../../tenant/TenantScopeContext")>(),
    scopeRef,
  );
});
vi.mock("../../../tenant/useIsTenantSwitched", () => ({
  useIsTenantSwitched: () => switchedRef.current,
}));

const list = vi.spyOn(artifactsSdk, "listArtifacts");
const versions = vi.spyOn(artifactsSdk, "listArtifactVersions");
const download = vi.spyOn(artifactsSdk, "downloadArtifact");

beforeEach(() => {
  list.mockResolvedValue({
    items: [{ id: "art-1", agent_key: "a", name: "report.txt", kind: "document", latest_version: 2 }],
    cross_tenant: false,
  });
  versions.mockResolvedValue({ id: "art-1", versions: [] });
  download.mockResolvedValue("report.txt");
});

afterEach(() => {
  scopeRef.current = undefined;
  switchedRef.current = false;
  list.mockReset();
  versions.mockReset();
  download.mockReset();
});

function renderPane() {
  render(
    <App>
      <ArtifactsPane userId="user-1" />
    </App>,
  );
}

describe("ArtifactsPane tenant scope", () => {
  it("reads in the switched-into tenant and grays out the writes", async () => {
    scopeRef.current = TENANT;
    switchedRef.current = true;
    renderPane();

    await userEvent.click(await screen.findByTestId("artifact-download-report.txt"));
    await userEvent.click(screen.getByTestId("artifact-versions-report.txt"));

    expect(list).toHaveBeenCalledWith({ userId: "user-1", tenantScope: TENANT });
    expect(download).toHaveBeenCalledWith("art-1", "report.txt", "user-1", TENANT);
    expect(versions).toHaveBeenCalledWith("art-1", "user-1", TENANT);
    expect(screen.getByTestId("artifact-delete-report.txt")).toBeDisabled();
    // 键盘用户绕得过 pointer-events 包装:类型下拉本身也要置灰。
    const kind = within(screen.getByTestId("artifact-kind-report.txt")).getByRole("combobox");
    expect(kind).toBeDisabled();
    expect(screen.getAllByTestId("readonly-tooltip")).toHaveLength(2);
  });

  it("the all-tenants aggregate falls back to the home tenant", async () => {
    scopeRef.current = "*";
    renderPane();

    await screen.findByTestId("artifact-download-report.txt");
    expect(list).toHaveBeenCalledWith({ userId: "user-1", tenantScope: undefined });
    expect(screen.getByTestId("artifact-delete-report.txt")).toBeEnabled();
    const kind = within(screen.getByTestId("artifact-kind-report.txt")).getByRole("combobox");
    expect(kind).toBeEnabled();
    expect(screen.queryByTestId("readonly-tooltip")).toBeNull();
  });
});
