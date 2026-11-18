/**
 * OutputSchemaLimits — B-315. The per-row "Limits" button of the structured
 * output field tree; clicking it opens a small popover with the limits that
 * apply to the row's type (text: min/max characters + regex; number/integer:
 * min/max; list: min/max items + no duplicates) and, for text/number/integer,
 * the allowed values (``enum``). Every change goes straight to ``onChange``
 * as a new node; clearing a box removes that key from the schema.
 */
import type { CSSProperties } from "react";
import { Button, Checkbox, Input, InputNumber, Popover, Select } from "antd";
import { useTranslation } from "react-i18next";

import {
  constraintKeysFor,
  supportsEnum,
  type ConstraintKey,
  type EditableSchemaNode,
  type SchemaNodeType,
} from "../output_schema_tree";

const LABEL: CSSProperties = { fontSize: 12, marginBottom: 2 };
const ITEM: CSSProperties = { marginBottom: 8 };

const COUNT_KEYS = new Set<ConstraintKey>(["minLength", "maxLength", "minItems", "maxItems"]);

// Tag strings → enum values of the node's type; ``undefined`` when empty.
function toEnum(type: SchemaNodeType, tags: string[]): (string | number)[] | undefined {
  if (type === "string") return tags.length > 0 ? tags : undefined;
  const nums = tags
    .map((s) => Number(s.trim()))
    .filter((n) => (type === "integer" ? Number.isInteger(n) : Number.isFinite(n)));
  return nums.length > 0 ? Array.from(new Set(nums)) : undefined;
}

export function countLimits(node: EditableSchemaNode): number {
  const set = Object.values(node.constraints).filter((v) => v !== undefined).length;
  return set + (node.enum !== undefined ? 1 : 0);
}

interface OutputSchemaLimitsProps {
  node: EditableSchemaNode;
  pathKey: string;
  onChange: (node: EditableSchemaNode) => void;
}

export function OutputSchemaLimits({ node, pathKey, onChange }: OutputSchemaLimitsProps) {
  const { t } = useTranslation();
  const keys = constraintKeysFor(node.type);
  const hasEnum = supportsEnum(node.type);
  const count = countLimits(node);
  const label = `${t("agent_form.output_schema.limits")}${count > 0 ? ` (${count})` : ""}`;

  if (keys.length === 0 && !hasEnum) {
    return (
      <Button size="small" disabled data-testid={`af-output-schema-limits-${pathKey}`}>
        {t("agent_form.output_schema.limits")}
      </Button>
    );
  }

  const setConstraint = (key: ConstraintKey, value: unknown): void => {
    const { [key]: _old, ...rest } = node.constraints;
    const blank = value === null || value === undefined || value === "" || value === false;
    onChange({ ...node, constraints: blank ? rest : { ...rest, [key]: value } });
  };

  const field = (key: ConstraintKey) => {
    const testId = `af-output-schema-limit-${key}-${pathKey}`;
    const caption = t(`agent_form.output_schema.limit_${key}`);
    if (key === "uniqueItems") {
      return (
        <div key={key} style={ITEM}>
          <Checkbox
            checked={node.constraints.uniqueItems === true}
            data-testid={testId}
            onChange={(e) => setConstraint(key, e.target.checked)}
          >
            {caption}
          </Checkbox>
        </div>
      );
    }
    if (key === "pattern") {
      return (
        <div key={key} style={ITEM}>
          <div style={LABEL}>{caption}</div>
          <Input
            size="small"
            value={node.constraints.pattern ?? ""}
            aria-label={caption}
            data-testid={testId}
            onChange={(e) => setConstraint(key, e.target.value)}
          />
        </div>
      );
    }
    const integerOnly = COUNT_KEYS.has(key) || node.type === "integer";
    return (
      <div key={key} style={ITEM}>
        <div style={LABEL}>{caption}</div>
        <InputNumber
          size="small"
          style={{ width: "100%" }}
          min={COUNT_KEYS.has(key) ? 0 : undefined}
          precision={integerOnly ? 0 : undefined}
          value={node.constraints[key] as number | undefined}
          aria-label={caption}
          data-testid={testId}
          onChange={(v) => setConstraint(key, v)}
        />
      </div>
    );
  };

  const content = (
    <div style={{ width: 220 }} data-testid={`af-output-schema-limits-panel-${pathKey}`}>
      {keys.map(field)}
      {hasEnum && (
        <div style={ITEM}>
          <div style={LABEL}>{t("agent_form.output_schema.enum_label")}</div>
          <Select
            mode="tags"
            size="small"
            style={{ width: "100%" }}
            tokenSeparators={[","]}
            placeholder={t("agent_form.output_schema.enum_placeholder")}
            value={(node.enum ?? []).map(String)}
            aria-label={t("agent_form.output_schema.enum_label")}
            data-testid={`af-output-schema-enum-${pathKey}`}
            onChange={(tags: string[]) => onChange({ ...node, enum: toEnum(node.type, tags) })}
          />
        </div>
      )}
    </div>
  );

  return (
    <Popover trigger="click" placement="bottomRight" content={content}>
      <Button size="small" data-testid={`af-output-schema-limits-${pathKey}`}>
        {label}
      </Button>
    </Popover>
  );
}
