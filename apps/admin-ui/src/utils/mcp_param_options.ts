/**
 * B-318 —— MCP 工具给一个参数声明的可选值。智能体配置的「固定值」下拉和 MCP 页的
 * 参数表共用。
 *
 * 与后端 ``orchestrator/tools/arg_bindings.py`` 的 ``fixed_value_options`` 逐条同义
 * (保存时据此校验,两边测试用例一一对应),改一边就要改另一边:
 *
 *  - 只看参数自己那一层(``properties[param]``),按 ``enum`` → ``oneOf`` → ``anyOf``
 *    取**第一个出现**的键;
 *  - ``enum``:非空数组、每项都是字符串;
 *  - ``oneOf`` / ``anyOf``:非空数组、每项是对象且 ``const`` 是字符串,``title`` 是非空
 *    字符串才当标题,项里的其它键不管;
 *  - 第一个出现的键形状不对 → ``null``,不退到下一个键去猜。重复值只留第一个。
 *
 * ``input_schema`` 是第三方 MCP 服务自报的,同 ``paramsOf`` 只认白名单形状,不抛。
 * 固定值只收字符串,所以非字符串的枚举、可空组合都当没有可选值。
 */

export interface ParamOption {
  /** 发出去 / 写进配置的值。 */
  value: string;
  /** 给人看的名字;没有就是 ``null``。 */
  title: string | null;
}

const isRecord = (x: unknown): x is Record<string, unknown> =>
  x !== null && typeof x === "object" && !Array.isArray(x);

const OPTION_KEYS = ["enum", "oneOf", "anyOf"] as const;

function enumOptions(items: unknown[]): ParamOption[] | null {
  if (!items.every((v): v is string => typeof v === "string")) return null;
  return items.map((value) => ({ value, title: null }));
}

function constOptions(items: unknown[]): ParamOption[] | null {
  const options: ParamOption[] = [];
  for (const item of items) {
    if (!isRecord(item) || typeof item.const !== "string") return null;
    const title = typeof item.title === "string" && item.title !== "" ? item.title : null;
    options.push({ value: item.const, title });
  }
  return options;
}

function dedupe(options: ParamOption[]): ParamOption[] {
  const seen = new Set<string>();
  return options.filter((o) => {
    if (seen.has(o.value)) return false;
    seen.add(o.value);
    return true;
  });
}

/** ``param`` 的可选值;``null`` = 没有可选值(文本框、后端不查)。 */
export function paramOptionsOf(inputSchema: unknown, param: string): ParamOption[] | null {
  if (!isRecord(inputSchema) || !isRecord(inputSchema.properties)) return null;
  const prop = inputSchema.properties[param];
  if (!isRecord(prop)) return null;
  const key = OPTION_KEYS.find((k) => k in prop);
  if (key === undefined) return null;
  const items = prop[key];
  if (!Array.isArray(items) || items.length === 0) return null;
  const options = key === "enum" ? enumOptions(items) : constOptions(items);
  return options === null ? null : dedupe(options);
}

/** 下拉里一个选项的文字:有标题用标题,没有用值本身。 */
export const optionLabel = (option: ParamOption): string => option.title ?? option.value;

/** 「本轮回复 reply / 本轮不回 silent」:有标题写「标题 值」,没有只写值。 */
export const formatParamOptions = (options: ParamOption[]): string =>
  options.map((o) => (o.title === null ? o.value : `${o.title} ${o.value}`)).join(" / ");
