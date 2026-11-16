/**
 * B-300 PR 2 — one memory's source, as a tag: the same five labels the
 * agent sees in its recall block (stated in user messages / confirmed by the
 * user / from the assistant / corrected by the user / source not recorded). Shared by the
 * memory list page and the user-profile memory pane.
 *
 * B-300 redesign rev 9 — a row that a newer memory replaced (``invalid_at`` set)
 * also carries a "superseded" tag, linked to the row that replaced it when
 * ``superseded_by`` is known. Whether a row was replaced is read from
 * ``invalid_at`` alone: ``supersedes`` on the newer row may also point at a row
 * that is still valid (the two just sit side by side). Every row's tag cell is
 * the anchor (``#memory-row-<id>``) such a link jumps to.
 */
import { Tag } from "antd";
import { useTranslation } from "react-i18next";

import { effectiveOrigin, type EffectiveOrigin, type MemoryItem } from "../api/memory";

const ORIGIN_COLOR: Record<EffectiveOrigin, string> = {
  user_corrected: "green",
  user_stated: "blue",
  user_confirmed: "cyan",
  assistant_derived: "orange",
  unrecorded: "default",
};

function SupersededTag({ item }: { item: MemoryItem }) {
  const { t } = useTranslation();
  if (!item.invalid_at) return null;
  const tag = (
    <Tag bordered={false} data-testid={`memory-superseded-${item.id}`}>
      {t("memory.superseded")}
    </Tag>
  );
  if (!item.superseded_by) return tag;
  return (
    <a
      href={`#memory-row-${item.superseded_by}`}
      title={t("memory.superseded_by_tip")}
      data-testid={`memory-superseded-link-${item.id}`}
    >
      {tag}
    </a>
  );
}

export function MemoryOriginTag({ item }: { item: MemoryItem }) {
  const { t } = useTranslation();
  const origin = effectiveOrigin(item);
  return (
    <span id={`memory-row-${item.id}`}>
      <Tag color={ORIGIN_COLOR[origin]} bordered={false} data-testid={`memory-origin-${item.id}`}>
        {t(`memory.origin_${origin}`)}
      </Tag>
      <SupersededTag item={item} />
    </span>
  );
}
