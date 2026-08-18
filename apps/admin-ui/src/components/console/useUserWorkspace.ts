/**
 * User-scoped workspace state — the persistent volume keyed on the current
 * user (not the bound thread), so it survives session deletion.
 *
 * Extracted from ``PlaygroundTab.tsx`` (Playground-Uplift D4) — the five
 * ``useState`` groups + ``loadWorkspace`` / ``handleDownloadFile`` /
 * ``handleDownloadArtifact`` / ``handleDeleteFile`` / ``handleDeleteArtifact``
 * + the ``useEffect([running])`` refresh, unified behind one hook. The two
 * separate in-flight trackers (``downloadingPath`` for file downloads,
 * ``busyWorkspaceKey`` for artifact download/delete + file delete) collapse
 * into a single ``busyKey`` per the hook's public contract — at most one
 * workspace mutation is ever in flight at a time, so one tracker is enough.
 */
import { useCallback, useEffect, useState } from "react";

import {
  deleteArtifact as deleteArtifactApi,
  downloadArtifact as downloadArtifactApi,
} from "../../api/artifacts";
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
  /** The path or `artifact:<name>` currently being downloaded/deleted. */
  busyKey: string | null;
}

/** 用户维度工作区;`running` 从 true 变 false(run 结束)时自动 reload;挂载时
 *  reload 一次(effect 首跑即按当前 `running` 值判断)。 */
export function useUserWorkspace({ running }: { running: boolean }): UseUserWorkspace {
  const { apiTenantScope } = useTenantScope();
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
      setBusyKey(path);
      try {
        await downloadUserWorkspaceFile(path, undefined, concreteTenantScope(apiTenantScope));
      } catch {
        // Swallow — the file may have been removed between list + click; the
        // refresh button re-syncs. A toast here would need the App message API.
      } finally {
        setBusyKey(null);
      }
    },
    [apiTenantScope],
  );

  const deleteFile = useCallback(
    async (path: string) => {
      setBusyKey(path);
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
      setBusyKey(`artifact:${name}`);
      try {
        await downloadArtifactApi(name, undefined, concreteTenantScope(apiTenantScope));
      } catch {
        // Swallow — same rationale as the file download.
      } finally {
        setBusyKey(null);
      }
    },
    [apiTenantScope],
  );

  const deleteArtifact = useCallback(
    async (name: string) => {
      setBusyKey(`artifact:${name}`);
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
