/** B-235 —— 产物版本列表逐版下载。
 *
 *  每一版登记时都留了副本(``retained: true``)→ 都能单独下载;逐版留存上线前登记的
 *  (``retained: false``)只有最新那一版下得到(读工作区文件),更早的显示「内容未保留」。 */
import { App } from "antd";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../../../i18n";

import * as artifactsSdk from "../../../api/artifacts";
import { ArtifactsPane } from "../ArtifactsPane";

const list = vi.spyOn(artifactsSdk, "listArtifacts");
const versions = vi.spyOn(artifactsSdk, "listArtifactVersions");
const download = vi.spyOn(artifactsSdk, "downloadArtifact");

afterEach(() => {
  list.mockReset();
  versions.mockReset();
  download.mockReset();
});

function version(n: number, retained: boolean): artifactsSdk.ArtifactVersion {
  return {
    version: n,
    path_in_workspace: "agents/a/report.txt",
    size_bytes: retained ? 3 : null,
    sha256: retained ? "abc" : null,
    created_in_thread: "t",
    created_at: null,
    retained,
  };
}

describe("ArtifactsPane versions drawer", () => {
  it("offers a download for every kept version and for the latest one", async () => {
    list.mockResolvedValue({
      items: [{ id: "art-1", agent_key: "a", name: "report.txt", kind: "document", latest_version: 4 }],
      cross_tenant: false,
    });
    versions.mockResolvedValue({
      id: "art-1",
      versions: [version(4, true), version(3, true), version(2, false), version(1, false)],
    });
    download.mockResolvedValue("report.txt");
    render(
      <App>
        <ArtifactsPane userId="user-1" />
      </App>,
    );

    await userEvent.click(await screen.findByTestId("artifact-versions-report.txt"));
    const table = await screen.findByTestId("artifact-versions-table");

    expect(await within(table).findByTestId("artifact-version-download-3")).toBeEnabled();
    expect(within(table).getByTestId("artifact-version-download-4")).toBeEnabled();
    expect(within(table).queryByTestId("artifact-version-download-2")).toBeNull();
    expect(within(table).getAllByText(/内容未保留|Content not kept/)).toHaveLength(2);

    await userEvent.click(within(table).getByTestId("artifact-version-download-3"));
    expect(download).toHaveBeenCalledWith("art-1", "report.txt", "user-1", undefined, 3);
  });

  it("a pre-release artifact still downloads its latest version", async () => {
    list.mockResolvedValue({
      items: [{ id: "art-2", agent_key: "a", name: "old.txt", kind: "document", latest_version: 2 }],
      cross_tenant: false,
    });
    versions.mockResolvedValue({ id: "art-2", versions: [version(2, false), version(1, false)] });
    render(
      <App>
        <ArtifactsPane userId="user-1" />
      </App>,
    );

    await userEvent.click(await screen.findByTestId("artifact-versions-old.txt"));
    const table = await screen.findByTestId("artifact-versions-table");

    expect(await within(table).findByTestId("artifact-version-download-2")).toBeEnabled();
    expect(within(table).queryByTestId("artifact-version-download-1")).toBeNull();
  });
});
