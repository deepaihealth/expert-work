/**
 * OutputSchemaLimits — B-315. The per-row "Limits" button of the structured
 * output field tree; clicking it opens a small popover with the limits that
 * apply to the row's type (text: min/max characters + regex; number/integer:
 * min/max; list: min/max items + no duplicates) and, for text/number/integer,
 * the allowed values (``enum``). Every change goes straight to ``onChange``
 * as a new node; clearing a box removes that key from the schema.
 *
 * Hints, never blocking: a minimum larger than its maximum, and allowed
 * values that were dropped because they aren't numbers (integers).
 */
import { useState, type CSSProperties } from "react";
import { Button, Checkbox, Input, InputNumber, Popover, Select, Typography } from "antd";
import { useTranslation } from "react-i18next";

import {
  constraintKeysFor,
  supportsEnum,
  type ConstraintKey,
  type EditableSchemaNode,
  type SchemaConstraints,
  type SchemaNodeType,
} from "../output_schema_tree";

const { Text } = Typography;

const LABEL: CSSProperties = { fontSize: 12, marginBottom: 2 };
const ITEM: CSSProperties = { marginBottom: 8 };

const COUNT_KEYS = new Set<ConstraintKey>(["minLength", "maxLength", "minItems", "maxItems"]);
const RANGES: [ConstraintKey, ConstraintKey][] = [
  ["minLength", "maxLength"],
  ["minimum", "maximum"],
  ["minItems", "maxItems"],
];

interface EnumParse {
  values: (string | number)[] | undefined;
  rejected: string[];
}

// Tag strings → enum values of the node's type (``undefined`` when empty),
// plus the entries that aren't valid for that type.
export function parseEnumTags(type: SchemaNodeType, tags: string[]): EnumParse {
  if (type === "string") return { values: tags.length > 0 ? tags : undefined, rejected: [] };
  const ok = (n: number): boolean => (type === "integer" ? Number.isInteger(n) : Number.isFinite(n));
  const rejected = tags.filter((s) => s.trim() === "" || !ok(Number(s.trim())));
  const nums = tags.filter((s) => !rejected.includes(s)).map((s) => Number(s.trim()));
  return { values: nums.length > 0 ? Array.from(new Set(nums)) : undefined, rejected };
}

/** How many limits are set (``uniqueItems: false`` is the default, not a limit). */
export function countLimits(node: EditableSchemaNode): number {
  const set = Object.entries(node.constraints).filter(
    ([k, v]) => v !== undefined && !(k === "uniqueItems" && v === false),
  ).length;
  return set + (node.enum !== undefined ? 1 : 0);
}

export function hasInvertedRange(c: SchemaConstraints): boolean {
  return RANGES.some(([lo, hi]) => {
    const a = c[lo];
    const b = c[hi];
    return typeof a === "number" && typeof b === "number" && a > b;
  });
}

interface ConstraintInputProps {
  node: EditableSchemaNode;
  name: ConstraintKey;
  pathKey: string;
  onSet: (key: ConstraintKey, value: unknown) => void;
}

function ConstraintInput({ node, name, pathKey, onSet }: ConstraintInputProps) {
  const { t } = useTranslation();
  const testId = `af-output-schema-limit-${name}-${pathKey}`;
  const caption = t(`agent_form.output_schema.limit_${name}`);
  if (name === "uniqueItems") {
    return (
      <div style={ITEM}>
        <Checkbox
          checked={node.constraints.uniqueItems === true}
          data-testid={testId}
          onChange={(e) => onSet(name, e.target.checked)}
        >
          {caption}
        </Checkbox>
      </div>
    );
  }
  const control =
    name === "pattern" ? (
      <Input
        size="small"
        value={node.constraints.pattern ?? ""}
        aria-label={caption}
        data-testid={testId}
        onChange={(e) => onSet(name, e.target.value)}
      />
    ) : (
      <InputNumber
        size="small"
        style={{ width: "100%" }}
        min={COUNT_KEYS.has(name) ? 0 : undefined}
        // Counts only: a precision on minimum/maximum makes the input reformat
        // (and write back) a stored fraction on mere focus + blur.
        precision={COUNT_KEYS.has(name) ? 0 : undefined}
        value={node.constraints[name] as number | undefined}
        aria-label={caption}
        data-testid={testId}
        onChange={(v) => onSet(name, v)}
      />
    );
  return (
    <div style={ITEM}>
      <div style={LABEL}>{caption}</div>
      {control}
    </div>
  );
}

interface EnumInputProps {
  node: EditableSchemaNode;
  pathKey: string;
  onChange: (node: EditableSchemaNode) => void;
}

function EnumInput({ node, pathKey, onChange }: EnumInputProps) {
  const { t } = useTranslation();
  const [rejected, setRejected] = useState<string[]>([]);
  const label = t("agent_form.output_schema.enum_label");
  const handle = (tags: string[]): void => {
    const parsed = parseEnumTags(node.type, tags);
    setRejected(parsed.rejected);
    onChange({ ...node, enum: parsed.values });
  };
  return (
    <div style={ITEM}>
      <div style={LABEL}>{label}</div>
      <Select
        mode="tags"
        size="small"
        style={{ width: "100%" }}
        // Numbers split on commas; text values may contain commas, so a text
        // value is confirmed with Enter only.
        tokenSeparators={node.type === "string" ? undefined : [","]}
        placeholder={t(
          node.type === "string"
            ? "agent_form.output_schema.enum_placeholder_text"
            : "agent_form.output_schema.enum_placeholder",
        )}
        value={(node.enum ?? []).map(String)}
        aria-label={label}
        data-testid={`af-output-schema-enum-${pathKey}`}
        onChange={handle}
      />
      {rejected.length > 0 && (
        <Text type="warning" style={{ fontSize: 12 }} data-testid={`af-output-schema-enum-rejected-${pathKey}`}>
          {t(`agent_form.output_schema.enum_rejected_${node.type === "integer" ? "integer" : "number"}`, {
            values: rejected.join(", "),
          })}
        </Text>
      )}
    </div>
  );
}

interface PanelProps {
  node: EditableSchemaNode;
  pathKey: string;
  onChange: (node: EditableSchemaNode) => void;
}

function LimitsPanel({ node, pathKey, onChange }: PanelProps) {
  const { t } = useTranslation();
  const setConstraint = (key: ConstraintKey, value: unknown): void => {
    const { [key]: _old, ...rest } = node.constraints;
    const blank = value === null || value === undefined || value === "" || value === false;
    onChange({ ...node, constraints: blank ? rest : { ...rest, [key]: value } });
  };
  return (
    <div style={{ width: 220 }} data-testid={`af-output-schema-limits-panel-${pathKey}`}>
      {constraintKeysFor(node.type).map((key) => (
        <ConstraintInput key={key} node={node} name={key} pathKey={pathKey} onSet={setConstraint} />
      ))}
      {hasInvertedRange(node.constraints) && (
        <div style={ITEM}>
          <Text type="warning" style={{ fontSize: 12 }} data-testid={`af-output-schema-range-warn-${pathKey}`}>
            {t("agent_form.output_schema.limit_range_warn")}
          </Text>
        </div>
      )}
      {/* keyed by type: a type change starts with no stale "not a number" hint */}
      {supportsEnum(node.type) && <EnumInput key={node.type} node={node} pathKey={pathKey} onChange={onChange} />}
    </div>
  );
}

export function OutputSchemaLimits({ node, pathKey, onChange }: PanelProps) {
  const { t } = useTranslation();
  const testId = `af-output-schema-limits-${pathKey}`;
  if (constraintKeysFor(node.type).length === 0 && !supportsEnum(node.type)) {
    return (
      <Button size="small" disabled data-testid={testId}>
        {t("agent_form.output_schema.limits")}
      </Button>
    );
  }
  const count = countLimits(node);
  return (
    <Popover
      trigger="click"
      placement="bottomRight"
      content={<LimitsPanel node={node} pathKey={pathKey} onChange={onChange} />}
    >
      <Button size="small" data-testid={testId}>
        {`${t("agent_form.output_schema.limits")}${count > 0 ? ` (${count})` : ""}`}
      </Button>
    </Popover>
  );
}
