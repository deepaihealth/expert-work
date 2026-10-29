/**
 * B-222 PR-2 — a document's ingest cost: 「预估 ¥0.12」 while it waits for its
 * confirmation (hover: the billed lines, what is not billed again, the
 * hints), 「已有同样内容的文档 D12《…》，默认不入库」 for a duplicate, and
 * 「预估 ¥x · 实际 ¥y」 once the paid ingest is over (§14 ⑥). Read-only:
 * cross-tenant viewers see it too.
 */
import { Space, Tag, Tooltip, Typography } from "antd";
import { useTranslation } from "react-i18next";

import type { EstimateItem, IngestEstimate, KnowledgeDocument } from "../../api/knowledge";
import { daysLeft, formatYuan, isDuplicate, matchingActual } from "./ingestCost";

const { Text } = Typography;

function itemLine(item: EstimateItem, t: ReturnType<typeof useTranslation>["t"]): string {
  const amount = item.priced ? formatYuan(item.micros) : t("knowledge_page.estimate_unpriced");
  const reused =
    item.reused_units > 0
      ? t(`knowledge_page.estimate_reused_${item.unit}`, { count: item.reused_units })
      : "";
  return t(`knowledge_page.estimate_item_${item.kind}`, {
    units: item.units,
    tokens: item.input_tokens + item.output_tokens,
    amount,
    reused,
    model: item.model ?? "—",
  });
}

function Breakdown({ estimate }: { estimate: IngestEstimate }) {
  const { t } = useTranslation();
  return (
    <div data-testid="doc-estimate-breakdown">
      {estimate.items.map((item) => (
        <div key={`${item.kind}-${item.model ?? ""}`}>{itemLine(item, t)}</div>
      ))}
      {estimate.notes.map((note) => (
        <div key={note} style={{ opacity: 0.85 }}>
          {t(`knowledge_page.estimate_note_${note}`, { defaultValue: note })}
        </div>
      ))}
      <div style={{ marginTop: 4, opacity: 0.7 }}>{t("knowledge_page.estimate_footnote")}</div>
    </div>
  );
}

export function IngestCostTag({ document }: { document: KnowledgeDocument }) {
  const { t } = useTranslation();
  const estimate = document.ingest_estimate ?? null;
  const actual = matchingActual(document);
  const testid = `doc-cost-${document.id}`;

  if (document.status === "awaiting_confirmation" && estimate && isDuplicate(estimate)) {
    const original = estimate.duplicate_of;
    return (
      <Space size={4} wrap data-testid={testid} data-kind="duplicate">
        <Tag color="purple" bordered={false}>
          {t("knowledge_page.estimate_duplicate_tag")}
        </Tag>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {original
            ? t("knowledge_page.estimate_duplicate_of", {
                seq: original.doc_seq ?? "—",
                name: original.filename,
              })
            : t("knowledge_page.estimate_duplicate_unknown")}
        </Text>
      </Space>
    );
  }
  if (document.status === "awaiting_confirmation" && estimate) {
    const days = daysLeft(document.confirm_by);
    return (
      <Space size={4} wrap data-testid={testid} data-kind="estimate">
        <Tooltip title={<Breakdown estimate={estimate} />}>
          <Tag color="blue" bordered={false} tabIndex={0}>
            {t("knowledge_page.estimate_amount", { amount: formatYuan(estimate.total_micros) })}
            {estimate.unpriced ? ` · ${t("knowledge_page.estimate_unpriced")}` : ""}
          </Tag>
        </Tooltip>
        <Text type="secondary" style={{ fontSize: 12 }} data-testid={`doc-days-left-${document.id}`}>
          {t("knowledge_page.estimate_days_left", { count: days })}
        </Text>
      </Space>
    );
  }
  if (actual !== null && estimate) {
    return (
      <Tooltip title={<Breakdown estimate={estimate} />}>
        <Text style={{ fontSize: 12 }} data-testid={testid} data-kind="actual" tabIndex={0}>
          {t("knowledge_page.estimate_and_actual", {
            estimate: formatYuan(estimate.total_micros),
            actual: formatYuan(actual),
          })}
        </Text>
      </Tooltip>
    );
  }
  if (actual !== null) {
    return (
      <Text style={{ fontSize: 12 }} data-testid={testid} data-kind="actual">
        {t("knowledge_page.estimate_actual_only", { actual: formatYuan(actual) })}
      </Text>
    );
  }
  if (estimate && document.status !== "estimating") {
    // Confirmed, the paid ingest under way.
    return (
      <Text type="secondary" style={{ fontSize: 12 }} data-testid={testid} data-kind="confirmed">
        {t("knowledge_page.estimate_amount", { amount: formatYuan(estimate.total_micros) })}
      </Text>
    );
  }
  return (
    <Text type="secondary" data-testid={testid} data-kind="none">
      —
    </Text>
  );
}
