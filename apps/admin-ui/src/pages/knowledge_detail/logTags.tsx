/**
 * A retrieval record's source and outcome as tags — B-175 P2-PR4. A value
 * this build does not know yet shows as it is.
 */
import { Tag, Tooltip } from "antd";
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

/** 「平台管理员」 — a search made from outside this tenant (platform staff
 *  switched in; ``flags.platform_admin``, user ruling 2026-10-28). Nothing
 *  otherwise. */
export function PlatformAdminTag({
  flags,
  testId,
}: {
  flags: Record<string, unknown>;
  testId: string;
}) {
  const { t } = useTranslation();
  if (flags.platform_admin !== true) return null;
  return (
    <Tooltip title={t("knowledge_retrieval.platform_admin_hint")}>
      <Tag color="purple" bordered={false} data-testid={testId}>
        {t("knowledge_retrieval.platform_admin")}
      </Tag>
    </Tooltip>
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
