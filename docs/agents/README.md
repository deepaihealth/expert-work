# Agent 设计目录

每个业务 Agent 一份「配置书」,是在 expert-work 控制台创建/维护该 Agent 的**单一权威依据**。

## 约定

- 一个 Agent 一个文件,以 `agent_code` 命名:`docs/agents/<agent_code>.md`(如 `ai-health-plan.md`)。
- 配置书是**活文档**:Agent 的 manifest 或 prompt 每次演进,先改这里、评审合并,再到控制台按它更新(控制台保存新版本即生效)。
- 每份配置书至少包含:
  1. 控制台落地步骤(含占位项与前置依赖)
  2. 完整 manifest YAML(system prompt 全文内嵌)
  3. 设计要点与理由(逐条对应平台硬约束)
  4. 对接方契约(inputs 结构、产物命名、取件规则等,变更需回传对接系统)
  5. playground 冒烟清单

## 现有 Agent

| agent_code | 说明 | 配置书 |
|---|---|---|
| `ai-health-plan` | AI 健康方案生成助手(deep-ai-health):员工对话式生成客户健康管理方案,深护智康 MCP 拉档案,产 PPT/PDF+JSON 双产物 | [ai-health-plan.md](./ai-health-plan.md) |

## 平台配置面参考

manifest 字段权威定义:`packages/expert-work-protocol/src/expert_work/protocol/agent_spec.py`;inputs/Jinja 机制:`services/control-plane/src/control_plane/prompt_render.py`;恒装工具:`services/orchestrator/src/orchestrator/tools/assembly.py`。
