/**
 * 一个 MCP 工具的参数表:参数名(必填带 *)、类型、描述,声明了可选值的参数在描述下
 * 多一行「可选：本轮回复 reply / 本轮不回 silent」(B-318)。
 *
 * 平台 MCP 目录详情页(``CatalogToolsTab``)和租户 MCP 页(``SettingsMcpServers``)共用,
 * 两边看到的同一个工具必须一模一样 —— 别在页面里各写一份。
 */
import { useMemo } from "react";
import { Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "react-i18next";

import {
  formatParamOptions,
  paramOptionsOf,
  type ParamOption,
} from "../../utils/mcp_param_options";

const { Text, Paragraph } = Typography;

interface ParamRow {
  name: string;
  type: string;
  required: boolean;
  description: string;
  options: ParamOption[] | null;
}

const isRecord = (x: unknown): x is Record<string, unknown> =>
  x !== null && typeof x === "object" && !Array.isArray(x);

const stringOr = (x: unknown, fallback: string): string =>
  typeof x === "string" ? x : fallback;

/**
 * Flatten a JSON-Schema ``input_schema`` into displayable parameter rows.
 *
 * The schema is whatever a third-party MCP server advertised: every shape is
 * checked and a bad one degrades to "absent" — never throws (admin-ui has no
 * ErrorBoundary, so a throw here blanks the whole console). A parameter whose
 * spec is not an object still gets its row, with no type / description.
 */
function extractParams(schema: unknown): ParamRow[] {
  if (!isRecord(schema) || !isRecord(schema.properties)) return [];
  const required = new Set(
    Array.isArray(schema.required)
      ? schema.required.filter((x): x is string => typeof x === "string")
      : [],
  );
  return Object.entries(schema.properties).map(([name, raw]) => {
    const spec = isRecord(raw) ? raw : {};
    return {
      name,
      type: stringOr(spec.type, "—"),
      required: required.has(name),
      description: stringOr(spec.description, ""),
      options: paramOptionsOf(schema, name),
    };
  });
}

export interface ToolParamsTableProps {
  /** 工具自报的 ``input_schema``(第三方给的,可能缺、可能形状不对)。 */
  schema: unknown;
  /** 表格的 ``data-testid``;可选值那一行是 ``{testId}-options-{参数名}``。 */
  testId: string;
}

export function ToolParamsTable({ schema, testId }: ToolParamsTableProps) {
  const { t } = useTranslation();
  const rows = useMemo(() => extractParams(schema), [schema]);

  const columns: ColumnsType<ParamRow> = useMemo(
    () => [
      {
        title: t("mcp_catalog.param_name"),
        dataIndex: "name",
        key: "name",
        width: "32%",
        render: (name: string, row) => (
          <Text strong>
            {name}
            {row.required && <Text type="danger"> *</Text>}
          </Text>
        ),
      },
      {
        title: t("mcp_catalog.param_type"),
        dataIndex: "type",
        key: "type",
        width: "18%",
        render: (type: string) => <Tag>{type}</Tag>,
      },
      {
        title: t("mcp_catalog.param_desc"),
        dataIndex: "description",
        key: "description",
        render: (d: string, row) => (
          <>
            {d || <Text type="secondary">—</Text>}
            {row.options !== null && (
              <div>
                <Text
                  type="secondary"
                  style={{ fontSize: 12 }}
                  data-testid={`${testId}-options-${row.name}`}
                >
                  {t("mcp_catalog.param_options", {
                    options: formatParamOptions(row.options),
                  })}
                </Text>
              </div>
            )}
          </>
        ),
      },
    ],
    [t, testId],
  );

  if (rows.length === 0) {
    return (
      <Paragraph type="secondary" style={{ marginBottom: 0 }}>
        {t("mcp_catalog.tool_no_params")}
      </Paragraph>
    );
  }
  return (
    <Table<ParamRow>
      size="small"
      rowKey="name"
      pagination={false}
      columns={columns}
      dataSource={rows}
      data-testid={testId}
    />
  );
}
