/**
 * FieldDef / PolicyFieldList — the declarative config-array pattern that
 * replaces hand-written ``FieldRow`` blocks (the shape ``RunBudgetSection``,
 * Task 6 of PR1, wrote by hand: five near-identical blocks of
 * label/brief/impact/default-badge plumbing around a single antd control).
 * A ``FieldDef`` is a data row describing one manifest field; ``PolicyFieldList``
 * renders one ``FieldRow`` per def, resolving its copy via the
 * ``${i18nKey}_label`` / ``_brief`` / ``_impact`` / ``_default`` convention and
 * wiring its control (``InputNumber`` for number/percent, ``Switch`` for
 * switch, antd ``Select`` for select/tags) to a single ``onPatch`` callback —
 * so a new field group is a data table, not a page of near-duplicate JSX.
 *
 * ``_impact`` feeds ``FieldRow``'s ``help`` (the long, click-to-reveal
 * explanation); ``_default`` feeds ``resetHint`` (the reset button's
 * tooltip). Both may be omitted per field — presence is checked at render
 * time via ``i18n.exists`` rather than assumed, since not every field needs
 * either. When ``_default`` is absent, ``resetHint`` falls back to
 * ``String(def.effectiveDefault)`` (or is itself omitted when
 * ``effectiveDefault`` is ``null`` — an unset-is-the-feature-off field).
 *
 * select/tags (PR3 Task 1) widen the value domain beyond number/boolean to
 * string / string-array; ``PolicyFieldList`` is generic over that domain
 * (``V extends FieldValue``) purely so existing number/boolean-only callers
 * (``RunBudgetSection``, ``ContextGatesSection``) keep their narrower
 * ``onPatch`` signatures — TS's contravariant callback-parameter check would
 * otherwise reject a ``(patch: Record<string, number|boolean|undefined>)``
 * handler once the prop type itself included string/array.
 */
import type { ReactNode } from "react";
import { InputNumber, Select, Switch } from "antd";
import { useTranslation } from "react-i18next";

import { FieldRow } from "../FieldRow";

export interface FieldDef {
  /** manifest 路径,= FieldRow data-field-id,也是 i18n 键的后缀源 */
  fieldId: string;
  /** i18n 命名空间前缀,如 "run_budget.max_iterations" → label/_brief/_impact/_default 四键 */
  i18nKey: string;
  /** 读写键,对应 read/patch helper 返回对象的字段名 */
  valueKey: string;
  kind: "number" | "switch" | "percent" | "select" | "tags"; // percent = 0–1 浮点,InputNumber step 0.05
  effectiveDefault: number | boolean | string | readonly string[] | null; // 显示层 effective 默认(isDefault 判定 + 徽章)
  min?: number;
  max?: number;
  /** select 专用:候选值列表,值本身即 option value */
  options?: readonly string[];
  /** select 专用:option label 的 i18n 前缀,`${optionLabelKey}_${option}`;缺省则显示裸值 */
  optionLabelKey?: string;
}

type FieldValue = number | boolean | string | readonly string[];

export interface PolicyFieldListProps<V extends FieldValue = FieldValue> {
  defs: readonly FieldDef[];
  values: Record<string, V | undefined>; // read helper 输出
  onPatch: (patch: Record<string, V | undefined>) => void;
  /** i18n 四键约定:`${i18nKey}_label` `_brief` `_impact` `_default`(_impact/_default 可缺省) */
}

function arraysEqual(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((v, i) => v === b[i]);
}

function isAtDefault(
  raw: FieldValue | undefined,
  effectiveDefault: FieldDef["effectiveDefault"],
): boolean {
  if (raw === undefined) return true;
  if (Array.isArray(raw) || Array.isArray(effectiveDefault)) {
    return arraysEqual(
      Array.isArray(raw) ? raw : [],
      Array.isArray(effectiveDefault) ? effectiveDefault : [],
    );
  }
  return raw === effectiveDefault;
}

function numericValue(
  raw: FieldValue | undefined,
  effectiveDefault: FieldDef["effectiveDefault"],
): number | null {
  if (typeof raw === "number") return raw;
  return typeof effectiveDefault === "number" ? effectiveDefault : null;
}

function booleanValue(
  raw: FieldValue | undefined,
  effectiveDefault: FieldDef["effectiveDefault"],
): boolean {
  if (typeof raw === "boolean") return raw;
  return typeof effectiveDefault === "boolean" ? effectiveDefault : false;
}

function stringValue(
  raw: FieldValue | undefined,
  effectiveDefault: FieldDef["effectiveDefault"],
): string | undefined {
  if (typeof raw === "string") return raw;
  return typeof effectiveDefault === "string" ? effectiveDefault : undefined;
}

function tagsValue(
  raw: FieldValue | undefined,
  effectiveDefault: FieldDef["effectiveDefault"],
): string[] {
  if (Array.isArray(raw)) return [...raw] as string[];
  return Array.isArray(effectiveDefault) ? ([...effectiveDefault] as string[]) : [];
}

/**
 * FieldControl — the ``def.kind``-dispatched control renderer that used to
 * live inline in ``PolicyFieldList``'s map (switch/select/tags/number, each
 * wired to the same ``onPatch`` convention: patch the explicit value, or an
 * explicit ``undefined`` when the new value round-trips back to
 * ``effectiveDefault``). Pulled out as its own component (Task 2 of a later
 * redesign PR) so ``PolicyFieldTable`` can reuse the exact same control
 * rendering without ``PolicyFieldList``'s ``FieldRow``-per-def layout.
 */
export function FieldControl<V extends FieldValue = FieldValue>({
  def,
  raw,
  label,
  onPatch,
}: {
  def: FieldDef;
  raw: V | undefined;
  label: string;
  onPatch: (patch: Record<string, V | undefined>) => void;
}): ReactNode {
  const { t } = useTranslation();

  return def.kind === "switch" ? (
    <Switch
      checked={booleanValue(raw, def.effectiveDefault)}
      aria-label={label}
      onChange={(checked) =>
        onPatch({
          [def.valueKey]:
            checked === def.effectiveDefault ? undefined : checked,
        } as Record<string, V | undefined>)
      }
    />
  ) : def.kind === "select" ? (
    <Select
      value={stringValue(raw, def.effectiveDefault)}
      aria-label={label}
      // 定宽而非 100%:FieldRow 一行式布局里 100% 会把右侧「已自定义」徽章
      // 挤掉行(2026-08-28 用户截图反馈,工作流类型行)。
      style={{ width: 220 }}
      options={(def.options ?? []).map((opt) => ({
        value: opt,
        label: def.optionLabelKey
          ? t(`${def.optionLabelKey}_${opt}`)
          : opt,
      }))}
      onChange={(v: string) =>
        onPatch({
          [def.valueKey]: v === def.effectiveDefault ? undefined : v,
        } as Record<string, V | undefined>)
      }
    />
  ) : def.kind === "tags" ? (
    <Select
      mode="tags"
      value={tagsValue(raw, def.effectiveDefault)}
      aria-label={label}
      style={{ width: "100%", minWidth: 220 }}
      placeholder={t("manifest_editor.tags_placeholder")}
      onChange={(v: string[]) =>
        onPatch({
          [def.valueKey]: v.length === 0 ? undefined : v,
        } as Record<string, V | undefined>)
      }
    />
  ) : (
    <InputNumber
      min={def.min ?? (def.kind === "percent" ? 0.05 : undefined)}
      max={def.max ?? (def.kind === "percent" ? 1 : undefined)}
      step={def.kind === "percent" ? 0.05 : undefined}
      value={numericValue(raw, def.effectiveDefault)}
      aria-label={label}
      onChange={(v) =>
        onPatch({
          [def.valueKey]: v ?? undefined,
        } as Record<string, V | undefined>)
      }
    />
  );
}

export function PolicyFieldList<V extends FieldValue = FieldValue>({
  defs,
  values,
  onPatch,
}: PolicyFieldListProps<V>): ReactNode {
  const { t, i18n } = useTranslation();

  return (
    <>
      {defs.map((def) => {
        const raw = values[def.valueKey];
        const atDefault = isAtDefault(raw, def.effectiveDefault);
        const label = t(`${def.i18nKey}_label`);

        const impactKey = `${def.i18nKey}_impact`;
        const defaultKey = `${def.i18nKey}_default`;
        const help = i18n.exists(impactKey) ? t(impactKey) : undefined;
        // select 的说明随所选值变化(2026-08-28 用户反馈,工具调用强制):
        // 存在 `${i18nKey}_brief_${当前值}` 键则用之,否则回退静态 _brief。
        const currentValue =
          typeof raw === "string"
            ? raw
            : typeof def.effectiveDefault === "string"
              ? def.effectiveDefault
              : undefined;
        const briefByValueKey =
          currentValue !== undefined ? `${def.i18nKey}_brief_${currentValue}` : undefined;
        const brief =
          briefByValueKey !== undefined && i18n.exists(briefByValueKey)
            ? t(briefByValueKey)
            : t(`${def.i18nKey}_brief`);
        const resetHint = i18n.exists(defaultKey)
          ? t(defaultKey)
          : def.effectiveDefault === null
            ? undefined
            : String(def.effectiveDefault);
        return (
          <FieldRow
            key={def.fieldId}
            fieldId={def.fieldId}
            label={label}
            brief={brief}
            help={help}
            isDefault={atDefault}
            onReset={() => onPatch({ [def.valueKey]: undefined } as Record<string, V | undefined>)}
            resetHint={resetHint}
          >
            <FieldControl def={def} raw={raw} label={label} onPatch={onPatch} />
          </FieldRow>
        );
      })}
    </>
  );
}
