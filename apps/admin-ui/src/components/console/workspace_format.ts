/**
 * Pure formatting helpers for the workspace inspector (``WorkspacePanel``).
 *
 * Extracted from ``PlaygroundTab.tsx`` (Playground-Uplift D4) — copied
 * verbatim, behavior unchanged.
 */

/** Human-readable byte size (``512 B``, ``2.0 KB``, …) — scales up through
 *  KB/MB/GB/TB, one decimal place once past the byte range. */
export function formatBytes(bytes: number): string {
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

/** True when any path segment is a dotfile/dotdir (``.npm``, ``.cache``,
 *  ``.mplconfig`` …) — the runtime noise filtered from the files list. */
export function isHiddenWorkspacePath(path: string): boolean {
  return path.split("/").some((seg) => seg.startsWith("."));
}
