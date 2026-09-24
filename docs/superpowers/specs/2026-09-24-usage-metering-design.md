# 用量记账口径补齐(B-102 / B-103 / B-104)设计

**日期**:2026-09-24 **来源**:B-64 真栈与 Task 9 复审带出 **拍板**:用户 2026-09-24「都按你推荐的做」

## 一、问题(实测)

- **B-104**:一个 run 里,只有主循环(`agent_node`,`builder.py:1324` 的 `after_llm_call` 链上的 `TokenUsageMiddleware`)、思考档位升级重答、看图(Task 9 `vl_metering.py`)写 `token_usage` 并计入 `TokenBudget`。以下 in-run 调用**两样都没有**:
  | # | 调用 | 位置 | 模型 |
  |---|---|---|---|
  | 1 | 任务规划 `planner_node` | `graph_builder/planner.py:206` | Agent(routing when=planning,缺省主模型) |
  | 2 | 反思评判 `reflect_node` | `graph_builder/reflect.py:197` | Agent(routing when=reflection) |
  | 3 | 对话压缩摘要 | `context/compressor.py:808,818` | Agent 主模型 |
  | 4 | 记忆读时校验 | `graph_builder/memory.py:457` | Agent 主模型 |
  | 5 | 记忆查询改写 | `memory.py:497` | Agent 主模型 |
  | 6 | 记忆写回抽取 | `memory.py:954` | Agent 主模型 |
  | 7 | 记忆写回归并 | `memory.py:822` | Agent 主模型 |
  | 8 | 输出安全评审 `LLMOutputJudge` | `control_plane/runtime.py:667` → `output_judge.py` | 平台评审模型(未配退回 Agent 主模型) |
  | 9 | 工具调用安全评审 `LLMActionJudge` | 同上 | 同上 |
  | 10 | 知识库 / 记忆重排序(LLM 分支) | `runtime.py:1181`、`:1297` | 平台设置 |
  另:#8/#9/#10 的 router 构建时连 `around_llm_call`(熔断 / Langfuse)都没接。
- **B-102**:主模型的 `token_usage.model` 记**配置的主模型名**,备用模型接管时也按主模型计价;测试环境 30 天 4006 条主 Agent 回复里 310 条(7.7%)由非主模型回答(sop2-designer→kimi-k3 206 条)。Task 9 起看图记**实际回答的模型** —— 同一张表两种含义。
- **B-103**:worker 事件帧 `usage_by_model` 由 `_child_run._usage_by_model_of` 按 worker 主模型单桶汇总,不含 worker 内的看图调用。

## 二、拍板(用户 2026-09-24)

1. **用 Agent 自己模型的 in-run 调用(#1-#7)记 `usage_kind="conversation"`**:对外可见(end 帧 `usage_by_model`、runs usage 接口)、计入 `TokenBudget`。
2. **用平台模型的 in-run 调用(#8-#10)记新 kind `"platform_overhead"`**:**不**进对外对话用量(`EXTERNAL_USAGE_KINDS` 不变,仍只有 conversation),运营后台用量页按 kind 可见;**计入 `TokenBudget`**(熔断看真实消耗)。#8/#9 在「未配平台评审模型、退回 Agent 主模型」时也记 `platform_overhead` —— 口径按**用途**分,不按模型归属分(评审是平台为安全付的成本)。
3. **B-102:所有 `token_usage` 行按「实际回答的模型」记**,名字用**配置里该模型条目的 provider/name**(路由里被选中的那个模型句柄),**不用厂商回显的 `response_metadata.model_name`**(会带别名,如 deepseek-flash vs deepseek-v4-flash)。**历史行不回填**。
4. **B-103**:worker 事件帧的 `usage_by_model` 与 run 级口径一致 —— 按实际模型分桶,含 worker 内的看图(及 B-104 新增的 in-run 调用)。

## 三、设计约束

- **单一机制**:造一个通用「记账调用包装」—— 把 Task 9 的 `VLUsageRecorder` + 「实际回答者盖章」泛化成可按 `usage_kind` 参数化、同时负责 `TokenBudget.add` 的组件;VL、#1-#10、主循环全走它或它的同一套规则。不许为每个调用点手写一份。主循环现有的 `after_llm_call` 记账**不重复计**:要么主循环也换成同一组件,要么组件对主循环不生效,二选一并写明。
- **失败 / 取消 / 缓存命中**语义照主循环现状(失败不记账;缓存命中按现有规则)。
- **trace 与维度**:同一个 run 的所有行共享该 run 的 trace_id,带 tenant / user / agent_name / agent_version;子 Agent / worker 内调用沿用现有「记在父 run trace 下、agent_name 为子代名」的规则。
- **预算**:`TokenBudget.add` 在调用成功返回后执行,不在调用中途抛;是否超额仍由下一次进入 agent 节点时检查(不改熔断语义)。
- **#8-#10 顺带接上 `around_llm_call`**(熔断 / Langfuse)—— 若牵涉面超出本设计,在任务报告里单列,由我裁定是否拆票。
- 运营后台用量页(`api/usage.py` 按 kind 汇总)能看到 `platform_overhead`;对外接口(`_run_usage.py` `EXTERNAL_USAGE_KINDS`)不变。计费汇总若按 kind 过滤,确认 `platform_overhead` 不进客户账单(找到计费汇总的实际读取处核对并写明)。

## 四、不做

- 不回填历史 `token_usage`。
- 不改熔断阈值与判定时机。
- 不改输出上限 / 思考上限(B-105)。

## 五、验收

- 单测 / 集成:每个调用点各一条「调用一次 → 一行、kind 正确、trace 正确、预算增加」;主循环行数不变(不重复计);备用接管时记备用模型名;worker 帧含看图桶。
- 测试环境真栈:ai-health-plan 跑一次会触发压缩 / 看图的会话(或 sop2-designer 触发备用),在 `token_usage` 按 trace 查:kind 分布、模型名、与 end 帧 `usage_by_model` 对得上。
