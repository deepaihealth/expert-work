/**
 * User-scoped workspace state — the persistent volume keyed on the current
 * user (not the bound thread), so it survives session deletion.
 *
 * Extracted from ``PlaygroundTab.tsx`` (Playground-Uplift D4) — the five
 * ``useState`` groups + ``loadWorkspace`` / ``handleDownloadFile`` /
 * ``handleDownloadArtifact`` / ``handleDeleteFile`` / ``handleDeleteArtifact``
 * + the ``useEffect([running])`` refresh, unified behind one hook.
 *
 * Fix round 1 (review finding): the original had *two* independent in-flight
 * trackers (``downloadingPath`` for file downloads only, ``busyWorkspaceKey``
 * for artifact download/delete + file delete), so a file's own download and
 * delete buttons never both lit up for the same in-flight action. Collapsing
 * both into one bare ``busyKey`` (path or ``artifact:<name>``) lost that —
 * a delete would make the download button spin too (identical key, no way to
 * tell the actions apart). ``busyKey`` now encodes **which** action is in
 * flight as a prefix — ``download:<key>`` / ``delete:<key>`` (``<key>`` is
 * the file path or ``artifact:<name>``, same as before) — so each button's
 * ``loading`` reads only its own action+key. Whole-panel mutual exclusion
 * (``busyKey !== null`` disables every other button) is intentional and
 * unchanged — the controller ruling accepted it (prevents deleting a file
 * mid-download of that same file).
 */
import { useCallback, useEffect, useState } from "react";
import { App } from "antd";
import { useTranslation } from "react-i18next";

import {
  deleteArtifact as deleteArtifactApi,
  downloadArtifact as downloadArtifactApi,
} from "../../api/artifacts";
import { errMessage } from "../../api/client";
import type { SessionWorkspace, WorkspaceFile } from "../../api/sessions";
import {
  deleteUserWorkspaceFile,
  downloadUserWorkspaceFile,
  getUserWorkspace,
  getUserWorkspaceFiles,
} from "../../api/workspace";
import { concreteTenantScope, useTenantScope } from "../../tenant/TenantScopeContext";

export interface UseUserWorkspace {
  workspace: SessionWorkspace | null;
  files: WorkspaceFile[];
  loading: boolean;
  reload: () => Promise<void>;
  downloadFile: (path: string) => Promise<void>;
  deleteFile: (path: string) => Promise<void>;
  downloadArtifact: (name: string) => Promise<void>;
  deleteArtifact: (name: string) => Promise<void>;
  /** ``download:<key>`` / ``delete:<key>`` for the action + path/`artifact:<name>`
   *  currently in flight, or ``null`` when idle. */
  busyKey: string | null;
}

/** 用户维度工作区;`running` 从 true 变 false(run 结束)时自动 reload;挂载时
 *  reload 一次(effect 首跑即按当前 `running` 值判断)。 */
export function useUserWorkspace({ running }: { running: boolean }): UseUserWorkspace {
  const { apiTenantScope } = useTenantScope();
  const { message } = App.useApp();
  const { t } = useTranslation();
  const [workspace, setWorkspace] = useState<SessionWorkspace | null>(null);
  const [files, setFiles] = useState<WorkspaceFile[]>([]);
  const [loading, setLoading] = useState(false);
  const [busyKey, setBusyKey] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const [ws, fs] = await Promise.all([
        getUserWorkspace(undefined, concreteTenantScope(apiTenantScope)),
        getUserWorkspaceFiles(undefined, concreteTenantScope(apiTenantScope)).catch(
          () => [] as WorkspaceFile[],
        ),
      ]);
      setWorkspace(ws);
      setFiles(fs);
    } catch {
      setWorkspace(null);
      setFiles([]);
    } finally {
      setLoading(false);
    }
  }, [apiTenantScope]);

  // Load on mount and refresh after each run — a run that wrote files makes
  // the volume appear / its size grow. Not tied to a thread, so the panel
  // stays visible after the session is deleted.
  useEffect(() => {
    if (!running) void reload();
  }, [running, reload]);

  const downloadFile = useCallback(
    async (path: string) => {
      setBusyKey(`download:${path}`);
      try {
        await downloadUserWorkspaceFile(path, undefined, concreteTenantScope(apiTenantScope));
      } catch (err) {
        // 静默吞错让「下载失败」表现成「点了没反应」(2026-08-26 用户反馈)。
        message.error(t("artifacts_page.download_failed", { detail: errMessage(err) }));
      } finally {
        setBusyKey(null);
      }
    },
    [apiTenantScope, message, t],
  );

  const deleteFile = useCallback(
    async (path: string) => {
      setBusyKey(`delete:${path}`);
      try {
        await deleteUserWorkspaceFile(path);
        await reload();
      } catch {
        // Swallow — refresh re-syncs the listing on the next manual refresh.
      } finally {
        setBusyKey(null);
      }
    },
    [reload],
  );

  const downloadArtifact = useCallback(
    async (name: string) => {
      setBusyKey(`download:artifact:${name}`);
      try {
        await downloadArtifactApi(name, undefined, concreteTenantScope(apiTenantScope));
      } catch (err) {
        // 同 downloadFile —— 失败要说出来,带后端 detail(不存在 / 太大)。
        message.error(t("artifacts_page.download_failed", { detail: errMessage(err) }));
      } finally {
        setBusyKey(null);
      }
    },
    [apiTenantScope, message, t],
  );

  const deleteArtifact = useCallback(
    async (name: string) => {
      setBusyKey(`delete:artifact:${name}`);
      try {
        await deleteArtifactApi(name);
        await reload();
      } catch {
        // Swallow — refresh re-syncs.
      } finally {
        setBusyKey(null);
      }
    },
    [reload],
  );

  return {
    workspace,
    files,
    loading,
    reload,
    downloadFile,
    deleteFile,
    downloadArtifact,
    deleteArtifact,
    busyKey,
  };
}
