/**
 * RowDetailSchema — the tool / `update_plan` record's Schema tab (PR-A.3
 * §十.2): the agent's live tool contract (description / source / deferred
 * flag / JSON Schema parameters) for the exact tool a TOOL row invoked, or
 * for `update_plan` on a PLAN row. Purely a renderer of whatever
 * `ToolSchemaState` (`useAgentTools.ts`) it's handed — no fetching here.
 */
import { Button, Typography } from "antd";
import { useTranslation } from "react-i18next";

import type { TrajectoryRow } from "../../api/trajectory_rows";
import { DetailRow } from "./DetailsFrame";
import { JsonBlock } from "./RowDetailPayloadResult";
import type { ToolSchemaState } from "./useAgentTools";

const { Text } = Typography;

/** TOOL row → its `entry.toolName`; PLAN row whose `source` is
 *  `"update_plan"` → the synthetic tool name `"update_plan"` (it's a real
 *  tool call under the hood, just not a `ToolRow` projection); everything
 *  else (planner-node PLAN rows included) → null, meaning "no tool schema
 *  to show for this record". */
export function schemaToolNameOf(row: TrajectoryRow): string | null {
  if (row.kind === "tool") return row.entry.toolName;
  if (row.kind === "plan" && row.source === "update_plan") return "update_plan";
  return null;
}

export function SchemaPanel({
  toolName,
  state,
}: {
  toolName: string;
  state: ToolSchemaState;
}) {
  const { t } = useTranslation();

  if (state.status === "idle" || state.status === "loading") {
    return (
      <div data-testid="console-detail-schema">
        <Text type="secondary">{t("console.detail_schema_loading")}</Text>
      </div>
    );
  }

  if (state.status === "error") {
    return (
      <div data-testid="console-detail-schema">
        <Text type="secondary">{t("console.detail_schema_error")}</Text>{" "}
        <Button size="small" data-testid="console-detail-schema-retry" onClick={state.reload}>
          {t("console.detail_schema_retry")}
        </Button>
      </div>
    );
  }

  const item = state.byName.get(toolName);
  if (item === undefined) {
    return (
      <div data-testid="console-detail-schema">
        <div data-testid="console-detail-schema-missing">
          <Text type="secondary">{t("console.detail_schema_missing")}</Text>
        </div>
      </div>
    );
  }

  return (
    <div data-testid="console-detail-schema">
      <dl className="ew-detail__ov">
        <DetailRow label="">{item.description}</DetailRow>
        <DetailRow label={t("console.detail_schema_source")}>
          {item.source}
          {item.from_skill !== null && ` · skill:${item.from_skill}`}
        </DetailRow>
        {item.deferred && <DetailRow label="">{t("console.detail_schema_deferred")}</DetailRow>}
      </dl>
      <h4>{t("console.detail_schema_parameters")}</h4>
      <JsonBlock value={item.parameters} copyTestId="console-detail-schema-copy" />
    </div>
  );
}
