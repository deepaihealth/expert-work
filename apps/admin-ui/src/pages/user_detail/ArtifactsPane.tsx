/**
 * User-detail Artifacts pane — the artifact governance surface
 * (conversation-centric IA M3, H.8-F1).
 *
 * Full actions (download / versions / re-classify kind / soft-delete)
 * against one member's artifacts via the ``?user_id=`` governance
 * target — this replaces the former top-level /artifacts page, whose
 * actions were caller-only. Non-admin callers viewing themselves get
 * the same surface; targeting someone else 403s server-side.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  App,
  Button,
  Drawer,
  Empty,
  Popconfirm,
  Select,
  Space,
  Table,
  Tooltip,
  Typography,
} from "antd";
import type { TableColumnsType } from "antd";
import { Download, History, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  deleteArtifact,
  downloadArtifact,
  listArtifacts,
  listArtifactVersions,
  patchArtifactKind,
  type ArtifactKind,
  type ArtifactList,
  type ArtifactListItem,
  type ArtifactVersion,
} from "../../api/artifacts";
import { ApiError } from "../../api/client";

const { Text } = Typography;

const KIND_OPTIONS: ArtifactKind[] = ["document", "code", "data", "other"];

function errMessage(err: unknown): string {
  return err instanceof ApiError
    ? `${err.code}: ${err.message}`
    : err instanceof Error
      ? err.message
      : "unknown error";
}

export function ArtifactsPane({ userId }: { userId: string }) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [data, setData] = useState<ArtifactList | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyName, setBusyName] = useState<string | null>(null);
  const [versionsFor, setVersionsFor] = useState<string | null>(null);
  const [versions, setVersions] = useState<ArtifactVersion[]>([]);
  const [versionsLoading, setVersionsLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await listArtifacts({ userId }));
    } catch (err) {
      setError(errMessage(err));
    } finally {
      setLoading(false);
    }
  }, [userId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // B-50 —— 一律按 ``artifact_id`` 寻址;``name`` 在 agent 维度下不再唯一
  // (同一用户下两个 agent 可以各有一个「报告.docx」),只用于文案与另存文件名。
  const handleDownload = useCallback(
    async (artifactId: string, name: string) => {
      setBusyName(artifactId);
      try {
        await downloadArtifact(artifactId, name, userId);
      } catch (err) {
        message.error(t("artifacts_page.download_failed", { detail: errMessage(err) }));
      } finally {
        setBusyName(null);
      }
    },
    [t, message, userId],
  );

  const handleDelete = useCallback(
    async (artifactId: string, name: string) => {
      setBusyName(artifactId);
      try {
        await deleteArtifact(artifactId, userId);
        message.success(t("artifacts_page.deleted", { name }));
        await refresh();
      } catch (err) {
        message.error(errMessage(err));
      } finally {
        setBusyName(null);
      }
    },
    [t, message, refresh, userId],
  );

  const handleKindChange = useCallback(
    async (record: ArtifactListItem, kind: ArtifactKind) => {
      // Mini-ADR H-16 — the backend 409s a no-op; skip it client-side.
      if (kind === record.kind) return;
      setBusyName(record.id);
      try {
        await patchArtifactKind(record.id, kind, userId);
        await refresh();
      } catch (err) {
        message.error(errMessage(err));
      } finally {
        setBusyName(null);
      }
    },
    [message, refresh, userId],
  );

  const openVersions = useCallback(
    async (artifactId: string, name: string) => {
      setVersionsFor(name);
      setVersionsLoading(true);
      try {
        const result = await listArtifactVersions(artifactId, userId);
        setVersions(result.versions);
      } catch (err) {
        message.error(errMessage(err));
        setVersions([]);
      } finally {
        setVersionsLoading(false);
      }
    },
    [message, userId],
  );

  const columns: TableColumnsType<ArtifactListItem> = useMemo(
    () => [
      {
        title: t("artifacts_page.col_name"),
        dataIndex: "name",
        key: "name",
        render: (name: string) => <Text strong>{name}</Text>,
      },
      {
        title: t("artifacts_page.col_kind"),
        dataIndex: "kind",
        key: "kind",
        width: 160,
        render: (kind: ArtifactKind, record) => (
          <Select<ArtifactKind>
            value={kind}
            size="small"
            style={{ width: 130 }}
            aria-label={t("artifacts_page.col_kind")}
            disabled={busyName === record.id}
            onChange={(next) => void handleKindChange(record, next)}
            options={KIND_OPTIONS.map((k) => ({ value: k, label: k }))}
            data-testid={`artifact-kind-${record.name}`}
          />
        ),
      },
      {
        title: t("artifacts_page.col_latest"),
        dataIndex: "latest_version",
        key: "latest_version",
        width: 100,
        render: (v: number) => <Text className="mono">v{v}</Text>,
      },
      {
        title: "",
        key: "actions",
        width: 230,
        render: (_: unknown, record) => (
          <Space size={6}>
            <Button
              size="small"
              icon={<Download size={13} strokeWidth={1.5} />}
              loading={busyName === record.id}
              onClick={() => void handleDownload(record.id, record.name)}
              data-testid={`artifact-download-${record.name}`}
            >
              {t("artifacts_page.download")}
            </Button>
            <Button
              size="small"
              icon={<History size={13} strokeWidth={1.5} />}
              onClick={() => void openVersions(record.id, record.name)}
              data-testid={`artifact-versions-${record.name}`}
            >
              {t("artifacts_page.versions")}
            </Button>
            <Popconfirm
              title={t("artifacts_page.delete_confirm_title", { name: record.name })}
              description={t("artifacts_page.delete_confirm_body")}
              onConfirm={() => void handleDelete(record.id, record.name)}
              okText={t("artifacts_page.delete")}
              okButtonProps={{ danger: true }}
            >
              <Button
                size="small"
                danger
                icon={<Trash2 size={13} strokeWidth={1.5} />}
                loading={busyName === record.id}
                data-testid={`artifact-delete-${record.name}`}
              >
                {t("artifacts_page.delete")}
              </Button>
            </Popconfirm>
          </Space>
        ),
      },
    ],
    [t, busyName, handleDownload, handleDelete, handleKindChange, openVersions],
  );

  const versionColumns: TableColumnsType<ArtifactVersion> = useMemo(
    () => [
      {
        title: t("artifacts_page.ver_col_version"),
        dataIndex: "version",
        width: 90,
        render: (v: number) => <Text className="mono">v{v}</Text>,
      },
      {
        title: t("artifacts_page.ver_col_path"),
        dataIndex: "path_in_workspace",
        ellipsis: true,
        render: (p: string) => (
          <Text code style={{ fontSize: 12 }}>
            {p}
          </Text>
        ),
      },
      {
        title: t("artifacts_page.ver_col_size"),
        dataIndex: "size_bytes",
        width: 110,
        render: (size: number | null) =>
          size === null ? (
            <Tooltip title={t("artifacts_page.digest_pending")}>
              <Text type="secondary">—</Text>
            </Tooltip>
          ) : (
            <Text className="mono">{size}</Text>
          ),
      },
      {
        title: "SHA-256",
        dataIndex: "sha256",
        width: 140,
        render: (sha: string | null) =>
          sha === null ? (
            <Tooltip title={t("artifacts_page.digest_pending")}>
              <Text type="secondary">—</Text>
            </Tooltip>
          ) : (
            <Tooltip title={sha}>
              <Text code style={{ fontSize: 12 }}>
                {sha.slice(0, 12)}…
              </Text>
            </Tooltip>
          ),
      },
      {
        title: t("artifacts_page.ver_col_created"),
        dataIndex: "created_at",
        width: 190,
        render: (iso: string | null) =>
          iso ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {new Date(iso).toLocaleString()}
            </Text>
          ) : (
            <Text type="secondary">—</Text>
          ),
      },
    ],
    [t],
  );

  return (
    <div data-testid="user-artifacts-pane">
      {/* Artifacts are cross-agent per-user workspace assets. */}
      <Alert
        type="info"
        showIcon
        message={t("user_detail.artifacts_scope_note")}
        style={{ marginBottom: 12 }}
      />
      {error !== null && (
        <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />
      )}
      <Table<ArtifactListItem>
        size="small"
        columns={columns}
        dataSource={data?.items ?? []}
        rowKey="name"
        loading={loading}
        pagination={false}
        locale={{ emptyText: <Empty description={t("user_detail.artifacts_empty")} /> }}
        data-testid="user-artifacts-table"
      />
      <Drawer
        title={t("artifacts_page.versions_title", { name: versionsFor ?? "" })}
        open={versionsFor !== null}
        onClose={() => setVersionsFor(null)}
        width={720}
        data-testid="artifact-versions-drawer"
      >
        <Table<ArtifactVersion>
          size="small"
          columns={versionColumns}
          dataSource={versions}
          rowKey="version"
          loading={versionsLoading}
          pagination={false}
          locale={{ emptyText: <Empty description={t("artifacts_page.versions_empty")} /> }}
          data-testid="artifact-versions-table"
        />
      </Drawer>
    </div>
  );
}
