/**
 * B-300 PR 2 — one memory's source, as a tag: the same four labels the
 * agent sees in its recall block (stated in user messages / from the
 * assistant / corrected by the user / source not recorded). Shared by the
 * memory list page and the user-profile memory pane.
 */
import { Tag } from "antd";
import { useTranslation } from "react-i18next";

import { effectiveOrigin, type EffectiveOrigin, type MemoryItem } from "../api/memory";

const ORIGIN_COLOR: Record<EffectiveOrigin, string> = {
  user_corrected: "green",
  user_stated: "blue",
  assistant_derived: "orange",
  unrecorded: "default",
};

export function MemoryOriginTag({ item }: { item: MemoryItem }) {
  const { t } = useTranslation();
  const origin = effectiveOrigin(item);
  return (
    <Tag color={ORIGIN_COLOR[origin]} bordered={false} data-testid={`memory-origin-${item.id}`}>
      {t(`memory.origin_${origin}`)}
    </Tag>
  );
}
