/**
 * relativeTime — compact relative time ("3分钟前") from an ISO timestamp,
 * localized via i18n keys. Falls back to the raw locale string for anything
 * older than a week.
 *
 * Lifted verbatim out of ``SessionHistoryDrawer`` (debug console redesign
 * PR-A Task 6) so ``SessionSidebar`` can reuse it without importing the
 * drawer.
 */
export function relativeTime(
  iso: string,
  t: (key: string, opts?: Record<string, unknown>) => string,
): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const secs = Math.floor((Date.now() - then) / 1000);
  if (secs < 60) return t("session_history.time_now");
  const mins = Math.floor(secs / 60);
  if (mins < 60) return t("session_history.time_minutes", { n: mins });
  const hours = Math.floor(mins / 60);
  if (hours < 24) return t("session_history.time_hours", { n: hours });
  const days = Math.floor(hours / 24);
  if (days < 7) return t("session_history.time_days", { n: days });
  return new Date(iso).toLocaleDateString();
}
