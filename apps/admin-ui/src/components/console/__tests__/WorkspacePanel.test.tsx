/**
 * WorkspacePanel tests — migrated from ``pages/__tests__/PlaygroundTab.test.tsx``
 * (Task 9 of the debug-console PR-A plan): the four workspace-inspector
 * behaviors that used to live inline in ``PlaygroundTab``, now exercised
 * directly against the extracted ``WorkspacePanel`` + ``useUserWorkspace``.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../../i18n";

import * as artifactsSdk from "../../../api/artifacts";
import * as workspaceSdk from "../../../api/workspace";
import { setStoredToken } from "../../../api/client";
import { AuthProvider } from "../../../auth/AuthContext";
import { TenantScopeProvider } from "../../../tenant/TenantScopeContext";
import { WorkspacePanel, type WorkspacePanelProps } from "../WorkspacePanel";

const getWorkspaceMock = vi.spyOn(workspaceSdk, "getUserWorkspace");
const getWorkspaceFilesMock = vi.spyOn(workspaceSdk, "getUserWorkspaceFiles");
const downloadFileMock = vi.spyOn(workspaceSdk, "downloadUserWorkspaceFile");
const deleteFileMock = vi.spyOn(workspaceSdk, "deleteUserWorkspaceFile");
const downloadArtifactMock = vi.spyOn(artifactsSdk, "downloadArtifact");
const deleteArtifactMock = vi.spyOn(artifactsSdk, "deleteArtifact");

beforeEach(() => {
  getWorkspaceMock.mockReset();
  getWorkspaceMock.mockResolvedValue({ workspace: null, artifacts: [] });
  getWorkspaceFilesMock.mockReset();
  getWorkspaceFilesMock.mockResolvedValue([]);
  downloadFileMock.mockReset();
  downloadFileMock.mockResolvedValue(undefined);
  deleteFileMock.mockReset();
  deleteFileMock.mockResolvedValue(undefined);
  downloadArtifactMock.mockReset();
  downloadArtifactMock.mockResolvedValue("report.md");
  deleteArtifactMock.mockReset();
  deleteArtifactMock.mockResolvedValue(undefined);
});

afterEach(() => {
  vi.clearAllMocks();
  setStoredToken(null);
});

function jwt(): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(
    JSON.stringify({
      sub: "u",
      tenant_id: "22222222-2222-2222-2222-222222222222",
      roles: [],
    }),
  );
  return `${header}.${body}.`;
}

function renderPanel(props: Partial<WorkspacePanelProps> = {}) {
  setStoredToken(jwt());
  return render(
    <AuthProvider>
      <TenantScopeProvider>
        <WorkspacePanel running={false} readOnly={false} {...props} />
      </TenantScopeProvider>
    </AuthProvider>,
  );
}

describe("WorkspacePanel", () => {
  it("lists artifacts with download/delete and hides dotfiles from files", async () => {
    getWorkspaceMock.mockResolvedValue({
      workspace: {
        id: "w1",
        tenant_id: "t1",
        user_id: "u1",
        volume_name: "vol-1",
        size_bytes: 1024,
        size_limit_bytes: 1_048_576,
        created_at: null,
        last_accessed_at: null,
        deleted_at: null,
        archived_object_key: null,
      },
      artifacts: [
        {
          name: "report.pdf",
          kind: "document",
          latest_version: 1,
          created_at: null,
          updated_at: null,
        },
      ],
    });
    getWorkspaceFilesMock.mockResolvedValue([
      { path: "agent_report.md", size: 2048 },
      { path: ".npm/_cacache/index", size: 99 },
      { path: ".mplconfig/matplotlibrc", size: 10 },
    ]);
    renderPanel();

    // Artifact renders as a list row with download + delete affordances.
    expect(
      await screen.findByTestId("playground-workspace-artifact-download"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("playground-workspace-artifact-delete")).toBeInTheDocument();
    expect(screen.getByText("report.pdf")).toBeInTheDocument();

    // Only the agent's own file shows; the dotfiles (.npm/.mplconfig) are hidden.
    const fileRows = screen.getAllByTestId("playground-workspace-file");
    expect(fileRows).toHaveLength(1);
    expect(screen.getByText("agent_report.md")).toBeInTheDocument();
    expect(screen.queryByText(".npm/_cacache/index")).not.toBeInTheDocument();
    expect(screen.getByTestId("playground-workspace-file-delete")).toBeInTheDocument();
  });

  it("shows the workspace inspector with the volume + artifacts", async () => {
    getWorkspaceMock.mockResolvedValue({
      workspace: {
        id: "w1",
        tenant_id: "22222222-2222-2222-2222-222222222222",
        user_id: "u-1",
        volume_name: "expert-work-ws-t-u",
        size_bytes: 2048,
        size_limit_bytes: 1000000,
        created_at: null,
        last_accessed_at: null,
        deleted_at: null,
        archived_object_key: null,
      },
      artifacts: [
        {
          name: "report.md",
          kind: "document",
          latest_version: 2,
          created_at: null,
          updated_at: null,
        },
      ],
    });
    renderPanel();
    const panel = await screen.findByTestId("playground-workspace");
    expect(panel).toHaveTextContent("expert-work-ws-t-u");
    expect(panel).toHaveTextContent("2.0 KB");
    expect(panel).toHaveTextContent("report.md");
  });

  it("shows 'no workspace' when the user has none (read-only null)", async () => {
    renderPanel();
    expect(await screen.findByTestId("playground-workspace-none")).toBeInTheDocument();
  });

  it("lists workspace files and downloads one on click", async () => {
    const user = userEvent.setup();
    getWorkspaceFilesMock.mockResolvedValue([{ path: "report.pdf", size: 2048 }]);
    renderPanel();
    const files = await screen.findByTestId("playground-workspace-files");
    expect(files).toHaveTextContent("report.pdf");
    await user.click(await screen.findByTestId("playground-workspace-file-download"));
    await waitFor(() =>
      expect(downloadFileMock).toHaveBeenCalledWith("report.pdf", undefined, undefined),
    );
  });

  it("refetches when running flips from true back to false", async () => {
    const { rerender } = renderPanel({ running: false });
    await waitFor(() => expect(getWorkspaceMock).toHaveBeenCalledTimes(1));

    rerender(
      <AuthProvider>
        <TenantScopeProvider>
          <WorkspacePanel running readOnly={false} />
        </TenantScopeProvider>
      </AuthProvider>,
    );
    // running=true — no refetch while a run is in flight.
    expect(getWorkspaceMock).toHaveBeenCalledTimes(1);

    rerender(
      <AuthProvider>
        <TenantScopeProvider>
          <WorkspacePanel running={false} readOnly={false} />
        </TenantScopeProvider>
      </AuthProvider>,
    );
    // running: true -> false — the run ended, refresh (second call).
    await waitFor(() => expect(getWorkspaceMock).toHaveBeenCalledTimes(2));
  });

  it("disables delete but keeps download enabled when readOnly", async () => {
    getWorkspaceMock.mockResolvedValue({
      workspace: {
        id: "w1",
        tenant_id: "t1",
        user_id: "u1",
        volume_name: "vol-1",
        size_bytes: 1024,
        size_limit_bytes: 1_048_576,
        created_at: null,
        last_accessed_at: null,
        deleted_at: null,
        archived_object_key: null,
      },
      artifacts: [
        {
          name: "report.pdf",
          kind: "document",
          latest_version: 1,
          created_at: null,
          updated_at: null,
        },
      ],
    });
    getWorkspaceFilesMock.mockResolvedValue([{ path: "out.txt", size: 10 }]);
    renderPanel({ readOnly: true });

    const artifactDownload = await screen.findByTestId("playground-workspace-artifact-download");
    const artifactDelete = screen.getByTestId("playground-workspace-artifact-delete");
    const fileDownload = await screen.findByTestId("playground-workspace-file-download");
    const fileDelete = screen.getByTestId("playground-workspace-file-delete");

    expect(artifactDelete).toBeDisabled();
    expect(fileDelete).toBeDisabled();
    expect(artifactDownload).not.toBeDisabled();
    expect(fileDownload).not.toBeDisabled();
  });
});
