/**
 * UserProfile — Workspace pane. The user's persistent ``(tenant, user)``
 * volume: its meta (name + size), the registered artifacts, and the raw
 * files — each downloadable / deletable via the ``?user_id=`` governance
 * target. Mirrors the playground workspace inspector, simplified.
 */
import { useCallback, useEffect, useState } from "react";
import { Alert, App, Button, Empty, Popconfirm, Space, Table, Typography } from "antd";
import type { TableColumnsType } from "antd";
import { Download, HardDrive, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  deleteArtifact,
  downloadArtifact,
  listArtifacts,
  type ArtifactListItem,
} from "../../api/artifacts";
import {
  deleteUserWorkspaceFile,
  downloadUserWorkspaceFile,
  getUserWorkspace,
  getUserWorkspaceFiles,
} from "../../api/workspace";
import type { SessionWorkspace, WorkspaceFile } from "../../api/sessions";
import { concreteTenantScope, useTenantScope } from "../../tenant/TenantScopeContext";
import { errMessage } from "./useLoad";

const { Text } = Typography;

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(1)} ${units[i]}`;
}

/** Hide dotfiles/dotdirs — runtime scaffolding, not authored files. */
function isHiddenWorkspacePath(path: string): boolean {
  return path.split("/").some((seg) => seg.startsWith("."));
}

export function WorkspacePane({ userId }: { userId: string }) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  // Cross-tenant W3 — user detail is single-tenant semantics: concrete UUID only.
  const { apiTenantScope } = useTenantScope();

  const [workspace, setWorkspace] = useState<SessionWorkspace | null>(null);
  const [files, setFiles] = useState<WorkspaceFile[]>([]);
  const [artifacts, setArtifacts] = useState<ArtifactListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    const [ws, fs, art] = await Promise.allSettled([
      getUserWorkspace(userId, concreteTenantScope(apiTenantScope)),
      getUserWorkspaceFiles(userId, concreteTenantScope(apiTenantScope)),
      listArtifacts({ userId }),
    ]);
    if (ws.status === "fulfilled") setWorkspace(ws.value);
    if (fs.status === "fulfilled") setFiles(fs.value);
    if (art.status === "fulfilled") setArtifacts(art.value.items);
    const failed = [ws, fs, art].find((r) => r.status === "rejected");
    if (failed && failed.status === "rejected") setError(errMessage(failed.reason));
    setLoading(false);
  }, [userId, apiTenantScope]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleDownloadArtifact = useCallback(
    async (name: string) => {
      setBusyKey(`artifact:${name}`);
      try {
        await downloadArtifact(name, userId);
      } catch (err) {
        message.error(errMessage(err));
      } finally {
        setBusyKey(null);
      }
    },
    [userId, message],
  );

  const handleDeleteArtifact = useCallback(
    async (name: string) => {
      setBusyKey(`artifact:${name}`);
      try {
        await deleteArtifact(name, userId);
        message.success(t("user_profile.deleted", { name }));
        await refresh();
      } catch (err) {
        message.error(errMessage(err));
      } finally {
        setBusyKey(null);
      }
    },
    [userId, message, t, refresh],
  );

  const handleDownloadFile = useCallback(
    async (path: string) => {
      setBusyKey(`file:${path}`);
      try {
        await downloadUserWorkspaceFile(path, userId, concreteTenantScope(apiTenantScope));
      } catch (err) {
        message.error(errMessage(err));
      } finally {
        setBusyKey(null);
      }
    },
    [userId, message, apiTenantScope],
  );

  const handleDeleteFile = useCallback(
    async (path: string) => {
      setBusyKey(`file:${path}`);
      try {
        await deleteUserWorkspaceFile(path, userId);
        message.success(t("user_profile.deleted", { name: path }));
        await refresh();
      } catch (err) {
        message.error(errMessage(err));
      } finally {
        setBusyKey(null);
      }
    },
    [userId, message, t, refresh],
  );

  const artifactColumns: TableColumnsType<ArtifactListItem> = [
    {
      title: t("user_detail.artifact_name"),
      dataIndex: "name",
      key: "name",
      render: (name: string) => <Text strong>{name}</Text>,
    },
    {
      title: t("user_detail.artifact_kind"),
      dataIndex: "kind",
      key: "kind",
      width: 120,
      render: (kind: string) => <Text type="secondary">{kind}</Text>,
    },
    {
      title: t("user_detail.artifact_version"),
      dataIndex: "latest_version",
      key: "latest_version",
      width: 90,
      render: (v: number) => <Text className="mono">v{v}</Text>,
    },
    {
      title: "",
      key: "actions",
      width: 170,
      render: (_: unknown, record) => (
        <Space size={6}>
          <Button
            size="small"
            icon={<Download size={13} strokeWidth={1.5} />}
            loading={busyKey === `artifact:${record.name}`}
            onClick={() => void handleDownloadArtifact(record.name)}
            data-testid={`ws-artifact-download-${record.name}`}
          >
            {t("user_profile.download")}
          </Button>
          <Popconfirm
            title={t("user_profile.delete_confirm", { name: record.name })}
            onConfirm={() => void handleDeleteArtifact(record.name)}
            okText={t("user_profile.delete")}
            okButtonProps={{ danger: true }}
          >
            <Button
              size="small"
              danger
              icon={<Trash2 size={13} strokeWidth={1.5} />}
              loading={busyKey === `artifact:${record.name}`}
              data-testid={`ws-artifact-delete-${record.name}`}
            />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const visibleFiles = files.filter((f) => !isHiddenWorkspacePath(f.path));

  const fileColumns: TableColumnsType<WorkspaceFile> = [
    {
      title: t("user_profile.workspace_files"),
      dataIndex: "path",
      key: "path",
      ellipsis: true,
      render: (path: string) => (
        <Text code style={{ fontSize: 12 }}>
          {path}
        </Text>
      ),
    },
    {
      title: t("user_profile.workspace_size"),
      dataIndex: "size",
      key: "size",
      width: 110,
      render: (size: number) => <Text className="mono">{formatBytes(size)}</Text>,
    },
    {
      title: "",
      key: "actions",
      width: 130,
      render: (_: unknown, record) => (
        <Space size={6}>
          <Button
            size="small"
            icon={<Download size={13} strokeWidth={1.5} />}
            loading={busyKey === `file:${record.path}`}
            onClick={() => void handleDownloadFile(record.path)}
            data-testid={`ws-file-download-${record.path}`}
          />
          <Popconfirm
            title={t("user_profile.delete_confirm", { name: record.path })}
            onConfirm={() => void handleDeleteFile(record.path)}
            okText={t("user_profile.delete")}
            okButtonProps={{ danger: true }}
          >
            <Button
              size="small"
              danger
              icon={<Trash2 size={13} strokeWidth={1.5} />}
              loading={busyKey === `file:${record.path}`}
              data-testid={`ws-file-delete-${record.path}`}
            />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const meta = workspace?.workspace ?? null;

  return (
    <div data-testid="user-workspace-pane">
      <Alert
        type="info"
        showIcon
        message={t("user_profile.workspace_scope_note")}
        style={{ marginBottom: 12 }}
      />
      {error !== null && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}

      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "8px 12px",
          marginBottom: 16,
          background: "var(--ew-surface-raised)",
          border: "1px solid var(--ew-border-subtle)",
          borderRadius: 6,
        }}
        data-testid="user-workspace-meta"
      >
        <HardDrive size={14} strokeWidth={1.5} />
        {meta ? (
          <Text style={{ fontSize: 13 }} className="mono">
            {t("user_profile.workspace_volume")}: {meta.volume_name} ·{" "}
            {t("user_profile.workspace_size")}: {formatBytes(meta.size_bytes)}
          </Text>
        ) : (
          <Text type="secondary" style={{ fontSize: 13 }} data-testid="user-workspace-none">
            {t("user_profile.workspace_none")}
          </Text>
        )}
      </div>

      <div style={{ marginBottom: 8 }}>
        <Text strong style={{ fontSize: 13 }}>
          {t("user_detail.tab_artifacts")}
        </Text>
      </div>
      <Table<ArtifactListItem>
        size="small"
        columns={artifactColumns}
        dataSource={artifacts}
        rowKey="name"
        loading={loading}
        pagination={false}
        locale={{ emptyText: <Empty description={t("user_detail.artifacts_empty")} /> }}
        style={{ marginBottom: 24 }}
        data-testid="user-workspace-artifacts-table"
      />

      <div style={{ marginBottom: 8 }}>
        <Text strong style={{ fontSize: 13 }}>
          {t("user_profile.workspace_files")}
        </Text>
      </div>
      <Table<WorkspaceFile>
        size="small"
        columns={fileColumns}
        dataSource={visibleFiles}
        rowKey="path"
        loading={loading}
        pagination={false}
        locale={{ emptyText: <Empty description={t("user_profile.workspace_files_empty")} /> }}
        data-testid="user-workspace-files-table"
      />
    </div>
  );
}
