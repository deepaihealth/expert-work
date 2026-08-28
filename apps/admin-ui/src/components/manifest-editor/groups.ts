import type { FormSection } from "./FormView";

export interface ConfigGroup {
  id: string; // 稳定 id,亦作树节点 key
  labelKey: string; // i18n: manifest_editor.group_<id>
  /** 该组堆叠渲染的既有 FormView sections(迁移映射,机械)。 */
  sections: readonly FormSection[];
  /** 组级搜索关键词(中英混合,小写匹配)。 */
  keywords: readonly string[];
}

/** A minimal translate function shape — matches ``useTranslation()``'s
 * ``t`` structurally without importing react-i18next here, so this module
 * stays React-free and independently testable. */
export type Translate = (key: string) => string;

export const CONFIG_GROUPS: readonly ConfigGroup[] = [
  { id: "basic", labelKey: "manifest_editor.group_basic", sections: ["basic"], keywords: ["名称", "版本", "描述", "name", "version", "extends"] },
  { id: "model", labelKey: "manifest_editor.group_model", sections: ["model"], keywords: ["模型", "回退", "路由", "思考", "model", "fallback", "thinking", "vision"] },
  { id: "prompt", labelKey: "manifest_editor.group_prompt", sections: ["prompt"], keywords: ["提示词", "输出", "jinja", "prompt", "schema", "日期", "date", "提醒"] },
  { id: "capabilities", labelKey: "manifest_editor.group_capabilities", sections: ["tools", "mcp", "knowledge", "skills", "subagents"], keywords: ["工具", "技能", "知识库", "子agent", "worker", "tools", "mcp", "skills"] },
  { id: "memory", labelKey: "manifest_editor.group_memory", sections: [], keywords: ["记忆", "memory", "recall"] },
  { id: "budget", labelKey: "manifest_editor.group_budget", sections: [], keywords: ["步数", "超时", "预算", "max_iterations", "deadline", "idle", "no_progress", "工作流", "workflow", "plan_execute", "规划", "plan_first", "结构化执行", "委派"] },
  { id: "context", labelKey: "manifest_editor.group_context", sections: [], keywords: ["压缩", "上下文", "compression", "working memory", "prune"] },
  { id: "security", labelKey: "manifest_editor.group_security", sections: [], keywords: ["防护", "审批", "安全", "defense", "approval", "egress"] },
  { id: "sandbox", labelKey: "manifest_editor.group_sandbox", sections: [], keywords: ["沙箱", "资源", "镜像", "sandbox", "cpu", "image"] },
  { id: "observability", labelKey: "manifest_editor.group_observability", sections: [], keywords: ["触发器", "可观测", "trigger", "trace", "log", "缓存", "cache", "响应缓存", "录制"] },
];

/**
 * Group-level search over ``CONFIG_GROUPS`` — a group matches when the
 * query is a lowercase substring of its i18n-resolved label OR of any of
 * its ``keywords``. An empty/whitespace-only query matches nothing (the
 * caller shows no dropdown rather than the full group list).
 *
 * Pure: the caller supplies ``t`` (e.g. ``useTranslation()``'s ``t``) so
 * this module has no React/i18next dependency and can be unit-tested
 * directly.
 */
export function searchGroups(q: string, t: Translate): ConfigGroup[] {
  const query = q.trim().toLowerCase();
  if (query === "") {
    return [];
  }
  return CONFIG_GROUPS.filter((group) => {
    const label = t(group.labelKey).toLowerCase();
    if (label.includes(query)) {
      return true;
    }
    return group.keywords.some((keyword) =>
      keyword.toLowerCase().includes(query),
    );
  });
}
