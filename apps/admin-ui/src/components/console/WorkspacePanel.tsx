/**
 * Workspace inspector — the current user's persistent volume (VM
 * workspace) + registered artifacts: browse, download, (soft-)delete.
 * User-scoped (not thread-scoped), so it stays visible after the bound
 * session is deleted.
 *
 * Extracted from ``PlaygroundTab.tsx`` (Playground-Uplift D4) — the JSX is
 * ported as-is; ``isTenantSwitched`` becomes the ``readOnly`` prop the
 * parent computes (``useUserWorkspace`` owns the state/handlers this used
 * to close over).
 */
import { Button, Popconfirm, Typography } from "antd";
import { Download, HardDrive, RefreshCw, Trash2 } from "lucide-react";
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import { ReadonlyTooltip } from "../ReadonlyTooltip";
import { useUserWorkspace } from "./useUserWorkspace";
import { formatBytes, isHiddenWorkspacePath } from "./workspace_format";

const { Text } = Typography;

export interface WorkspacePanelProps {
  running: boolean;
  readOnly: boolean;
}

export function WorkspacePanel({ running, readOnly }: WorkspacePanelProps): JSX.Element {
  const { t } = useTranslation();
  const {
    workspace,
    files,
    loading,
    reload,
    downloadFile,
    deleteFile,
    downloadArtifact,
    deleteArtifact,
    busyKey,
  } = useUserWorkspace({ running });

  // The initial fetch failed outright (network error) — nothing to show.
  if (!workspace) return <></>;

  return (
    <div
      data-testid="playground-workspace"
      style={{
        marginTop: "auto",
        borderTop: "1px solid var(--ew-border-subtle)",
        paddingTop: 8,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          marginBottom: 6,
        }}
      >
        <HardDrive size={13} strokeWidth={1.75} />
        <Text strong style={{ fontSize: 12 }}>
          {t("playground.workspace_label")}
        </Text>
        <Button
          size="small"
          type="text"
          icon={<RefreshCw size={11} strokeWidth={1.75} />}
          loading={loading}
          onClick={() => void reload()}
          aria-label={t("playground.workspace_refresh")}
          data-testid="playground-workspace-refresh"
          style={{ marginLeft: "auto" }}
        />
      </div>
      {workspace.workspace ? (
        <div style={{ fontSize: 11 }} className="mono">
          <div data-testid="playground-workspace-volume">
            {t("playground.workspace_volume")}: {workspace.workspace.volume_name}
          </div>
          <div>
            {t("playground.workspace_size")}: {formatBytes(workspace.workspace.size_bytes)}
            {workspace.workspace.deleted_at ? ` · ${t("playground.workspace_deleted")}` : ""}
          </div>
        </div>
      ) : (
        <Text type="secondary" style={{ fontSize: 11 }} data-testid="playground-workspace-none">
          {t("playground.workspace_none")}
        </Text>
      )}
      {/* Artifacts — the agent's registered deliverables: download +
          (soft-)delete each. A list, not chips, since they're the things
          you actually take away. */}
      {workspace.artifacts.length > 0 && (
        <div style={{ marginTop: 6 }} data-testid="playground-workspace-artifacts">
          <Text type="secondary" style={{ fontSize: 11 }}>
            {t("playground.workspace_artifacts")}:
          </Text>
          <div
            style={{
              marginTop: 4,
              display: "flex",
              flexDirection: "column",
              gap: 2,
            }}
          >
            {workspace.artifacts.map((a) => (
              <div
                key={a.name}
                data-testid="playground-workspace-artifact"
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  fontSize: 11,
                }}
              >
                <span
                  className="mono"
                  style={{
                    flex: 1,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                  title={`${a.name} · ${a.kind} v${a.latest_version}`}
                >
                  {a.name}
                </span>
                <Text type="secondary" style={{ fontSize: 10 }}>
                  {a.kind} v{a.latest_version}
                </Text>
                <Button
                  size="small"
                  type="text"
                  icon={<Download size={11} strokeWidth={1.75} />}
                  loading={busyKey === `download:artifact:${a.name}`}
                  disabled={busyKey !== null}
                  onClick={() => void downloadArtifact(a.name)}
                  aria-label={t("playground.artifact_download", { name: a.name })}
                  data-testid="playground-workspace-artifact-download"
                />
                <ReadonlyTooltip on={readOnly}>
                  <Popconfirm
                    title={t("playground.artifact_delete_confirm")}
                    okText={t("playground.delete_ok")}
                    cancelText={t("playground.delete_cancel")}
                    okButtonProps={{ danger: true }}
                    onConfirm={() => void deleteArtifact(a.name)}
                    disabled={readOnly}
                  >
                    <Button
                      size="small"
                      type="text"
                      danger
                      icon={<Trash2 size={11} strokeWidth={1.75} />}
                      loading={busyKey === `delete:artifact:${a.name}`}
                      disabled={busyKey !== null || readOnly}
                      aria-label={t("playground.artifact_delete", { name: a.name })}
                      data-testid="playground-workspace-artifact-delete"
                    />
                  </Popconfirm>
                </ReadonlyTooltip>
              </div>
            ))}
          </div>
        </div>
      )}
      {/* Browse + download + delete the raw files the agent wrote. Hidden
          files (.npm/.cache/.mplconfig …) are filtered — runtime noise. */}
      {files.some((f) => !isHiddenWorkspacePath(f.path)) && (
        <div style={{ marginTop: 8 }} data-testid="playground-workspace-files">
          <Text type="secondary" style={{ fontSize: 11 }}>
            {t("playground.workspace_files")}:
          </Text>
          <div
            style={{
              marginTop: 4,
              display: "flex",
              flexDirection: "column",
              gap: 2,
            }}
          >
            {files
              .filter((f) => !isHiddenWorkspacePath(f.path))
              .map((f) => (
                <div
                  key={f.path}
                  data-testid="playground-workspace-file"
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 6,
                    fontSize: 11,
                  }}
                >
                  <span
                    className="mono"
                    style={{
                      flex: 1,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                    title={f.path}
                  >
                    {f.path}
                  </span>
                  <Text type="secondary" style={{ fontSize: 10 }}>
                    {formatBytes(f.size)}
                  </Text>
                  <Button
                    size="small"
                    type="text"
                    icon={<Download size={11} strokeWidth={1.75} />}
                    loading={busyKey === `download:${f.path}`}
                    disabled={busyKey !== null}
                    onClick={() => void downloadFile(f.path)}
                    aria-label={t("playground.workspace_file_download", { name: f.path })}
                    data-testid="playground-workspace-file-download"
                  />
                  <ReadonlyTooltip on={readOnly}>
                    <Popconfirm
                      title={t("playground.file_delete_confirm")}
                      okText={t("playground.delete_ok")}
                      cancelText={t("playground.delete_cancel")}
                      okButtonProps={{ danger: true }}
                      onConfirm={() => void deleteFile(f.path)}
                      disabled={readOnly}
                    >
                      <Button
                        size="small"
                        type="text"
                        danger
                        icon={<Trash2 size={11} strokeWidth={1.75} />}
                        loading={busyKey === `delete:${f.path}`}
                        disabled={busyKey !== null || readOnly}
                        aria-label={t("playground.file_delete", { name: f.path })}
                        data-testid="playground-workspace-file-delete"
                      />
                    </Popconfirm>
                  </ReadonlyTooltip>
                </div>
              ))}
          </div>
        </div>
      )}
    </div>
  );
}
