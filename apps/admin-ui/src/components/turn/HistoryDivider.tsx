/**
 * i18n note: reads the ``playground.*`` namespace — now a **cross-page
 * shared** namespace (see ``components/turn/types.ts``).
 */
import { useTranslation } from "react-i18next";

/** #6 / 历史懒重建 — the "以下为本次新消息" divider closing a resumed thread's
 *  prior conversation, shared by both the lazy (count-paired) and flat
 *  (degradation) history render branches. */
export function HistoryDivider() {
  const { t } = useTranslation();
  return (
    <div
      style={{
        textAlign: "center",
        fontSize: 11,
        color: "var(--ew-text-tertiary)",
        borderTop: "1px dashed var(--ew-border-subtle)",
        paddingTop: 6,
        marginTop: 2,
      }}
    >
      {t("playground.history_divider")}
    </div>
  );
}
