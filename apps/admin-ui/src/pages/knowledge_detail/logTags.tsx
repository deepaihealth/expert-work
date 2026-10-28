/**
 * A retrieval record's source and outcome as tags — B-175 P2-PR4. A value
 * this build does not know yet shows as it is.
 */
import { Tag } from "antd";
import { useTranslation } from "react-i18next";

const SOURCES = new Set(["agent", "console_test", "open_api"]);

const STATUS_COLOR: Record<string, string> = {
  ok: "green",
  no_hits: "orange",
  below_threshold: "gold",
  error: "red",
};

export function LogSourceTag({ source }: { source: string }) {
  const { t } = useTranslation();
  return (
    <Tag bordered={false}>
      {SOURCES.has(source) ? t(`knowledge_retrieval.source_${source}`) : source}
    </Tag>
  );
}

export function LogStatusTag({ status }: { status: string }) {
  const { t } = useTranslation();
  const color = STATUS_COLOR[status];
  return (
    <Tag color={color ?? "default"} bordered={false}>
      {color ? t(`knowledge_retrieval.status_${status}`) : status}
    </Tag>
  );
}
