/**
 * UserProfile — Workspace pane. The user's persistent ``(tenant, user)``
 * volume: its meta (name + size), the registered artifacts, and the raw
 * files — each downloadable / deletable via the ``?user_id=`` governance
 * target. Mirrors the playground workspace inspector, simplified.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
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
import { useIsTenantSwitched } from "../../tenant/useIsTenantSwitched";
import { ReadonlyTooltip } from "../../components/ReadonlyTooltip";
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

/** 文件表的树节点 —— 目录行(children)+ 文件行(可下载/删除)。 */
interface WsFileNode {
  /** rowKey:目录用 `dir:<prefix>`(与同名文件不撞),文件用完整 path。 */
  key: string;
  /** 本级显示名:目录名 / 文件 basename。 */
  name: string;
  /** 文件的完整工作区路径;目录行为其前缀(仅作展示辅助)。 */
  path: string;
  isDir: boolean;
  /** 文件自身大小;目录为子树合计。 */
  size: number;
  children?: WsFileNode[];
}

/** 平铺的 `qa/a.pdf` 列表 → 嵌套目录树,目录在前、同级按名排序。
 *  2026-08-26 用户反馈:全路径平铺一行一条,同目录文件视觉上完全散开。 */
export function buildFileTree(files: readonly WorkspaceFile[]): WsFileNode[] {
  const root: WsFileNode[] = [];
  for (const f of files) {
    const segs = f.path.split("/");
    let siblings = root;
    let prefix = "";
    for (let i = 0; i < segs.length - 1; i += 1) {
      prefix = prefix === "" ? segs[i] : `${prefix}/${segs[i]}`;
      let dir = siblings.find((n) => n.isDir && n.name === segs[i]);
      if (dir === undefined) {
        dir = { key: `dir:${prefix}`, name: segs[i], path: prefix, isDir: true, size: 0, children: [] };
        siblings.push(dir);
      }
      dir.size += f.size;
      siblings = dir.children!;
    }
    siblings.push({
      key: f.path,
      name: segs[segs.length - 1],
      path: f.path,
      isDir: false,
      size: f.size,
    });
  }
  const sortLevel = (nodes: WsFileNode[]): void => {
    nodes.sort((a, b) => Number(b.isDir) - Number(a.isDir) || a.name.localeCompare(b.name));
    for (const n of nodes) if (n.children) sortLevel(n.children);
  };
  sortLevel(root);
  return root;
}

function collectDirKeys(nodes: readonly WsFileNode[], out: string[] = []): string[] {
  for (const n of nodes) {
    if (n.isDir) {
      out.push(n.key);
      if (n.children) collectDirKeys(n.children, out);
    }
  }
  return out;
}

export function WorkspacePane({ userId }: { userId: string }) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  // Cross-tenant W3 — user detail is single-tenant semantics: concrete UUID only.
  const { apiTenantScope } = useTenantScope();
  // Cross-tenant W3 — 切入态只读:删工件/删文件是写操作,置灰。
  const isTenantSwitched = useIsTenantSwitched();

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
      listArtifacts({ userId, tenantScope: concreteTenantScope(apiTenantScope) }),
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
        await downloadArtifact(name, userId, concreteTenantScope(apiTenantScope));
      } catch (err) {
        message.error(errMessage(err));
      } finally {
        setBusyKey(null);
      }
    },
    [userId, message, apiTenantScope],
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
          <ReadonlyTooltip on={isTenantSwitched}>
            <Popconfirm
              title={t("user_profile.delete_confirm", { name: record.name })}
              onConfirm={() => void handleDeleteArtifact(record.name)}
              okText={t("user_profile.delete")}
              okButtonProps={{ danger: true }}
              disabled={isTenantSwitched}
            >
              <Button
                size="small"
                danger
                disabled={isTenantSwitched}
                icon={<Trash2 size={13} strokeWidth={1.5} />}
                loading={busyKey === `artifact:${record.name}`}
                data-testid={`ws-artifact-delete-${record.name}`}
              />
            </Popconfirm>
          </ReadonlyTooltip>
        </Space>
      ),
    },
  ];

  const fileTree = useMemo(
    () => buildFileTree(files.filter((f) => !isHiddenWorkspacePath(f.path))),
    [files],
  );
  // 目录默认全展开。antd 的 defaultExpandAllRows 只在首挂载时生效,而
  // files 是异步落地的 —— 必须受控,数据到达时重置成全部目录 key。
  const [expandedKeys, setExpandedKeys] = useState<readonly string[]>([]);
  useEffect(() => {
    setExpandedKeys(collectDirKeys(fileTree));
  }, [fileTree]);

  const fileColumns: TableColumnsType<WsFileNode> = [
    {
      title: t("user_profile.workspace_files"),
      dataIndex: "name",
      key: "name",
      ellipsis: true,
      render: (_: unknown, record) =>
        record.isDir ? (
          <Text strong style={{ fontSize: 12 }}>
            {record.name}/
          </Text>
        ) : (
          <Text code style={{ fontSize: 12 }}>
            {record.name}
          </Text>
        ),
    },
    {
      title: t("user_profile.workspace_size"),
      dataIndex: "size",
      key: "size",
      width: 110,
      render: (size: number, record) => (
        <Text className="mono" type={record.isDir ? "secondary" : undefined}>
          {formatBytes(size)}
        </Text>
      ),
    },
    {
      title: "",
      key: "actions",
      width: 130,
      render: (_: unknown, record) =>
        record.isDir ? null : (
          <Space size={6}>
            <Button
              size="small"
              icon={<Download size={13} strokeWidth={1.5} />}
              loading={busyKey === `file:${record.path}`}
              onClick={() => void handleDownloadFile(record.path)}
              data-testid={`ws-file-download-${record.path}`}
            />
            <ReadonlyTooltip on={isTenantSwitched}>
              <Popconfirm
                title={t("user_profile.delete_confirm", { name: record.path })}
                onConfirm={() => void handleDeleteFile(record.path)}
                okText={t("user_profile.delete")}
                okButtonProps={{ danger: true }}
                disabled={isTenantSwitched}
              >
                <Button
                  size="small"
                  danger
                  disabled={isTenantSwitched}
                  icon={<Trash2 size={13} strokeWidth={1.5} />}
                  loading={busyKey === `file:${record.path}`}
                  data-testid={`ws-file-delete-${record.path}`}
                />
              </Popconfirm>
            </ReadonlyTooltip>
          </Space>
        ),
    },
  ];

  const meta = workspace?.workspace ?? null;
  // Task 7 — effective per-user byte cap; ``undefined`` on an old backend
  // that hasn't shipped it yet falls back to the pre-existing size-only line.
  const limitBytes = workspace?.limit_bytes ?? null;

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
            {limitBytes != null ? (
              <>
                {t("user_profile.workspace_volume")}: {meta.volume_name} ·{" "}
                {t("user_profile.workspace_usage", {
                  used: formatBytes(meta.size_bytes),
                  limit: formatBytes(limitBytes),
                })}
              </>
            ) : (
              // 旧后端容错:保留原「大小」行(limit_bytes 尚未发布)。
              <>
                {t("user_profile.workspace_volume")}: {meta.volume_name} ·{" "}
                {t("user_profile.workspace_size")}: {formatBytes(meta.size_bytes)}
              </>
            )}
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
      <Table<WsFileNode>
        size="small"
        columns={fileColumns}
        dataSource={fileTree}
        rowKey="key"
        loading={loading}
        pagination={false}
        expandable={{
          expandedRowKeys: expandedKeys as string[],
          onExpandedRowsChange: (keys) => setExpandedKeys(keys.map(String)),
        }}
        locale={{ emptyText: <Empty description={t("user_profile.workspace_files_empty")} /> }}
        data-testid="user-workspace-files-table"
      />
    </div>
  );
}
