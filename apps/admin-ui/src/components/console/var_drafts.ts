/**
 * var_drafts — 调试台变量草稿记忆(调试台侧栏重设计,规格 C)。
 *
 * localStorage 按 ``agent_code + 变量名`` 记「最后一次提交」的变量值:
 * 每个 agent 一条 JSON(``expert_work.console.varDrafts.<agent>``),键是
 * 变量名。发出 run 时合并写入(本次没提交的变量保留各自的旧草稿);进入
 * 页面时**仅对空字段**预填 —— 已有值绝不覆盖。刻意不做过期策略。
 */

const DRAFTS_KEY_PREFIX = "expert_work.console.varDrafts.";

function draftsKey(agentCode: string): string {
  return `${DRAFTS_KEY_PREFIX}${agentCode}`;
}

/** 读出该 agent 的草稿表;没有 / 损坏 / 无 localStorage 一律空表。 */
export function readVarDrafts(agentCode: string): Record<string, string> {
  if (typeof window === "undefined") return {};
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(draftsKey(agentCode));
  } catch {
    return {};
  }
  if (raw === null) return {};
  try {
    const parsed: unknown = JSON.parse(raw);
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      return {};
    }
    const out: Record<string, string> = {};
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof v === "string") out[k] = v;
    }
    return out;
  } catch {
    return {};
  }
}

/** 合并写入本次提交的变量值(空表不写,免得白占一条 key)。 */
export function saveVarDrafts(
  agentCode: string,
  submitted: Readonly<Record<string, string>>,
): void {
  if (typeof window === "undefined") return;
  if (Object.keys(submitted).length === 0) return;
  const merged = { ...readVarDrafts(agentCode), ...submitted };
  try {
    window.localStorage.setItem(draftsKey(agentCode), JSON.stringify(merged));
  } catch {
    // 配额满等存储失败:草稿是便利功能,静默放弃,不打断发送。
  }
}

/** 仅对空字段预填草稿值:``names`` 限定当前声明的变量(不复活已删除的);
 *  已有非空值的字段绝不覆盖。没有任何字段被填时原引用返回。 */
export function prefillEmptyValues(
  values: Record<string, string>,
  drafts: Readonly<Record<string, string>>,
  names: readonly string[],
): Record<string, string> {
  let changed = false;
  const out: Record<string, string> = { ...values };
  for (const name of names) {
    const draft = drafts[name];
    if (draft === undefined || draft === "") continue;
    if ((out[name] ?? "") !== "") continue;
    out[name] = draft;
    changed = true;
  }
  return changed ? out : values;
}
