/** B-235 —— 对话页 / 调试台每一轮的产物按钮:按这一轮登记的版本下载。
 *
 *  那一版登记在逐版留存上线之前、又已不是最新版时,后端回 409 —— 这里提示「这一版内容
 *  没有单独保留」并给一个「下载最新版」,绝不悄悄换成最新版。 */
import { App } from "antd";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../../../i18n";

import { ApiError } from "../../../api/client";
import * as sessionsSdk from "../../../api/sessions";
import { useTurnArtifactDownload } from "../useTurnArtifactDownload";

const download = vi.spyOn(sessionsSdk, "downloadSessionArtifact");

afterEach(() => {
  download.mockReset();
});

function Probe({ version }: { version?: number }) {
  const onDownload = useTurnArtifactDownload("t-1", undefined);
  return (
    <button type="button" onClick={() => void onDownload("report.txt", version)}>
      go
    </button>
  );
}

function renderProbe(version?: number) {
  render(
    <App>
      <Probe version={version} />
    </App>,
  );
}

describe("useTurnArtifactDownload", () => {
  it("downloads the version that turn registered", async () => {
    download.mockResolvedValue("report.txt");
    renderProbe(2);

    await userEvent.click(screen.getByText("go"));

    expect(download).toHaveBeenCalledTimes(1);
    expect(download).toHaveBeenCalledWith("t-1", "report.txt", undefined, 2);
  });

  it("on 409 explains the version was not kept and offers the latest — never silently", async () => {
    download.mockRejectedValueOnce(new ApiError("not kept", "HTTP_409", 409));
    download.mockResolvedValueOnce("report.txt");
    renderProbe(1);

    await userEvent.click(screen.getByText("go"));

    expect(await screen.findByText(/内容没有单独保留|was not kept/)).toBeInTheDocument();
    expect(download).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByRole("button", { name: /下载最新版|Download latest/ }));

    await waitFor(() => expect(download).toHaveBeenCalledTimes(2));
    expect(download).toHaveBeenLastCalledWith("t-1", "report.txt", undefined, undefined);
  });

  it("other failures stay a plain error toast", async () => {
    download.mockRejectedValue(new ApiError("gone", "HTTP_404", 404));
    renderProbe(1);

    await userEvent.click(screen.getByText("go"));

    expect(await screen.findByText(/HTTP_404/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /下载最新版|Download latest/ })).toBeNull();
  });
});
