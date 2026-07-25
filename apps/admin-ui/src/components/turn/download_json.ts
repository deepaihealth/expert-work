/**
 * Client-side JSON download — lifted verbatim out of
 * ``pages/agent_detail/PlaygroundTab.tsx`` so the conversation detail page's
 * read-only ``TurnCard``s can back the same 「导出 JSON」 button with a real
 * export instead of a no-op.
 */

/** Trigger a client-side download of ``data`` as a pretty-printed JSON file. */
export function downloadJson(filename: string, data: unknown): void {
  const blob = new Blob([JSON.stringify(data, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
