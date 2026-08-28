# B-35 · `execution_mode: plan_first` 结构化执行设计(委派增强层 4)

日期:2026-08-28
状态:设计拍板,**未开工**(启动条件见 §1.3)
关联:ROADMAP B-35;委派增强层 0-3(PR #1345/#1346/#1347/#1352);真栈验证记录见 ROADMAP B-35 条目

---

## 1. 背景与动机

### 1.1 问题

配了 `dynamic_workers` 的 Agent 极少主动调用 `spawn_worker`,必须在输入里点名才委派。层 0-3(工具描述形状判据 / system prompt 注入 / plan 落盘提醒 / 配置页领域策略生成)已全部上线并经四轮真栈探针实证**机制全部生效**——glm-5.3 与 kimi-k3 的思考原文都引用了注入的判据——但四轮 `expert_work_dynamic_worker_spawned_total` 均为 0:模型权衡后总是选 inline。

这与业界观察一致(Claude 自家栈实测 7 跑 0 委派):**prompt 层没有强制手段,委派永远是概率**。把委派从概率变保证的唯一结构解,是规划-执行分离——本设计。

### 1.2 核心思路

per-agent 开关 `execution_mode: plan_first`。开启后,run 的执行形态变为:

1. planner 先出结构化计划,每步带 `execution: delegate | inline` 标注;
2. 存在待执行的 delegate 步骤时,图路由进入**分发轮(dispatch turn)**:该轮工具注册表收窄到 `{spawn_worker, update_plan}`,主模型只能把 delegate 步骤展开成 self-contained task 并行派发——**委派由结构保证,task 质量由 LLM 保证**;
3. worker 结果回来后回到普通循环:主线负责汇总、校验、写操作与最终拍板(Cognition 边界:写操作与拍板永远留主线)。

### 1.3 启动条件(不变)

层 0-3 上线后,真实流量 `expert_work_dynamic_worker_spawned_total` 跑 1-2 周仍趋零,才立项开工。两个零成本杠杆(kimi 工具过载收敛 / 提问措辞进层 3 策略)先试。

---

## 2. 现状地基(2026-08-28 侦察,行号为当日工作树)

| 事实 | 锚点 |
|---|---|
| `workflow.type ∈ {react, plan_execute, custom}` 默认 react,`extra="forbid"`;custom/early_stop/builder 已废弃惰性 | `packages/expert-work-protocol/src/expert_work/protocol/agent_spec.py:599-627` |
| **plan_execute 现状是 advisory 的**:唯一分支点是挂不挂 planner 节点;planner 一次性 LLM 调用出 `{goal, steps[]}`,解析失败降级单步计划永不阻断;之后就是普通 ReAct 循环,plan 只作为每轮尾部 HumanMessage 复诵块注入(保 prompt cache 前缀) | `agent_factory.py:822-826`、`graph_builder/planner.py:47-159`、`graph_builder/builder.py:1495-1529,1575-1588` |
| **没有 executor 节点、没有 per-step 循环**——「结构化分发」是本设计真正的新造物 | 同上 |
| `PlanStep = {id, description, status}` frozen;plan 落 LangGraph checkpoint `plan` 通道(非独立表);SSE 顶层 `plan` 帧整份快照;前端 PlanCard 三源合并;`PUT /plan` 有 RUNNING 409 保护 | `protocol/plan.py:22-41`、`control_plane/api/plan.py`、`orchestrator/sse.py:555-556,1682-1696`、`admin-ui/src/components/console/usePlanCard.ts` |
| `update_plan` 对**每个** agent 隐式注册,create-or-replace 整份替换,`state_updates={"plan": ...}` 通道写回 | `orchestrator/tools/update_plan.py`、`agent_factory.py:834` |
| `dynamic_workers = {enabled: bool = True, model}`;数值上限全在平台 settings(并发 3 / 每 run 16 / 迭代 32);生效公式=平台开关 ∧ per-agent enabled ∧ 深度未封顶 ∧ builder 已接线 | `agent_spec.py:493-538`、`control_plane/settings.py:700-721` |
| worker 合成:继承父 spec,剥 subagents/memory/triggers/skills/reflection/routing/knowledge;**`workflow.type` 原样继承**、只 clamp max_iterations;`policies`(approval_required_tools)**不剥** | `control_plane/subagent_runtime.py:109-159` |
| **子 run 审批黑洞(现状 bug)**:`_child_run.py` 486 行零 approval/interrupt 处理;worker 触发审批闸 → 子图写 `pending_approval` 路由 END → 父侧拿最后一个 values chunk 当 `outcome="success"`——审批请求不冒泡、不报错,**静默假完成**;此交互零测试覆盖 | `orchestrator/tools/_child_run.py:146-176`、`graph_builder/builder.py:1225-1290,1546-1550` |
| 审批机制刻意不用 LangGraph 原生 `interrupt()`(resume 会重跑整节点),走 `pending_approval` 状态通道 + checkpoint | `graph_builder/_approval.py:5` |
| 层 1 委派提醒是现存唯一「plan 状态 → 执行行为」耦合点(≥2 未完成条目→一次 hide_from_ui 提醒,去重键 `delegation_nudge_plan_hash`) | `graph_builder/builder.py:195,1404-1421,1839-1855` |
| UI:`workflow.type` 在 RunBudgetSection「步数/流程」小节(select 三值);`dynamic_workers` 在 SecuritySection;i18n 四键约定(_label/_brief/_impact/_default);模板字段分层表 `agent_template_resolve.py:69-70`(workflow 与 dynamic_workers 均 CAPABILITY) | `manifest-editor/groups/RunBudgetSection.tsx:65-71`、`form_model.ts:867-914` |
| `execution_mode` / `plan_first` 全仓零代码引用(仅 ROADMAP 两处文档) | grep 全仓 |

---

## 3. 开关设计(用户 2026-08-28 两点要求的落法)

### 3.1 字段

```yaml
spec:
  workflow:
    type: plan_execute        # 开启后被联动强制
    execution_mode: plan_first  # 新字段,Literal["standard", "plan_first"],默认 "standard"
```

- 放 `WorkflowSpec` 内(语义=工作流执行形态;UI 同小节联动最直观)。`extra="forbid"` 意味着老 control-plane 遇到新 manifest 会 422——**先发含字段的后端再允许配置**,与历史新增字段同纪律。
- **不合并进 `workflow.type` 第四值**:type 的三值语义是图拓扑(挂不挂 planner),execution_mode 是执行纪律(分发轮),正交维度混进一个枚举 UI 联动讲不清;且存量 manifest 三值兼容面不动。

### 3.2 硬联动(开启时,确认 Modal 后一次写入)

| 配置项 | 联动 | 理由 |
|---|---|---|
| `workflow.type` | react/custom → **plan_execute** | plan_first 依赖 planner 节点产出初始计划 |
| `dynamic_workers.enabled` | false → **true** | 分发轮的执行体就是 worker,关着等于自相矛盾 |

一次写入 = 单个 manifest 版本(配置历史单版本可 diff,不出现「半开」中间版本)。

### 3.3 确认 Modal(admin-ui)

开启开关不立即写值,先弹确认 Modal:

**「将自动修改」区**(硬联动,红字/高亮):
- 工作流类型:react → plan_execute
- 动态子智能体:关闭 → 开启

**「建议检查」区**(不自动改,列清单):
- **token 成本**:大白话提示——多智能体并行执行的 token 消耗可达单智能体的 ~15 倍(Anthropic 实测量级),确认业务价值配得上;
- `workflow.max_iterations`:分发轮 + 汇总轮会消耗迭代,过小的值可能不够;
- `policies.deadline` / 超时:并行 worker 受全局 deadline 管,过短会截断在飞 worker;
- **reflection 协同**:reflect 修改计划后,分发循环按新计划继续(新增 delegate 步骤会再次触发分发轮);
- **触发器**:定时触发的 run 同样按 plan_first 执行(orchestrator 对 run 来源无感),确认定时任务的成本可接受。

确认 → 三字段一次写入;取消 → 什么都不改。

### 3.4 关闭开关

只写 `execution_mode: standard`,**不回退** `workflow.type` 与 `dynamic_workers.enabled`(开启后用户可能已手动调过这两项,自动回退会吞用户配置)。UI 出一行提示:「已关闭结构化执行;工作流类型仍为 plan_execute、动态子智能体仍开启,如需调整请手动修改」。

### 3.5 双层防护

- **UI 层**:Modal 联动一次写入(如上)。
- **后端层**:`AgentSpecBody` model_validator 硬校验——`execution_mode == "plan_first"` 时,`workflow.type != "plan_execute"` 或 `dynamic_workers.enabled == false` → ValidationError,经 manifest loader 投影成 422 `MANIFEST_INVALID`(带字段级 loc/msg)。**不静默归一**:直接改 YAML 绕过 UI 的用户会得到明确报错而非被偷改配置。
- **平台开关例外**:平台级 `enable_dynamic_workers=false` 属运维态,不参与 manifest 校验(否则运维关闸会让存量 manifest 全部变非法);构建时若平台闸关闭,plan_first **运行时降级为 standard** 并打 warning 日志 + 计数(见 §7)。

---

## 4. 运行时设计:结构化分发循环

### 4.1 PlanStep 扩展(protocol)

```python
class PlanStep(BaseModel):  # frozen
    id: str
    description: str
    status: PlanStepStatus = "pending"
    execution: Literal["delegate", "inline"] = "inline"   # 新增,默认 inline
```

- 默认 `inline` → 存量 checkpoint / SSE 帧 / `PUT /plan` 载荷全部向后兼容(缺字段=inline)。
- `update_plan` 工具 schema 同步加可选 `execution` 字段;**enum 节点必须显式带 `"type": "string"`**(B-34 moonshot 严格校验教训,守卫测试 `test_tool_schema_vendor_strict.py` 会逮)。
- 标注者:①plan_first 下的 planner prompt 强化——把层 0 形状判据(≥3 同构独立子项/通读长材料/探索查找;反例:写操作与拍板/不自足)写进 `_PLANNER_SYSTEM`,要求每步输出 execution 标注;②主模型运行中经 `update_plan` 修改计划时也可标。standard 模式下字段合法但无行为(advisory 现状不变)。

### 4.2 分发轮(dispatch turn)——核心新造物

图拓扑不加新节点,在现有 agent 循环的**进 agent 前**路由处加判据(与层 1 提醒同一挂点,复用其模式):

**触发判据**:`execution_mode == "plan_first"` ∧ plan 存在 ∧ `pending 且 execution=delegate` 的步骤数 ≥ 1 ∧ 本 plan-hash 未分发过(去重键 `plan_first_dispatch_plan_hash`,新 checkpoint 通道,hash 含 delegate 步骤集合、**刻意不含 status**——与 `delegation_nudge_plan_hash` 同纪律:标进度不重触发,结构性改计划才再触发)。

**分发轮行为**:
1. 注入一条 hide_from_ui 合成指令:「以下 delegate 步骤,本轮只能通过 spawn_worker 完成(可多个并行);为每步写 fully self-contained 的 task——写明标识符、范围、该用哪些工具、期望输出格式;含写操作或最终拍板的步骤不在此列」;
2. **工具注册表收窄**:该轮传给 LLM 的 tools 只有 `{spawn_worker, update_plan}`(保留 update_plan 让模型能在分发前微调计划,如把误标 delegate 的写操作步骤改回 inline)——这是「委派由结构保证」的落点:模型没有 inline 执行工具可选;
3. worker 并行度天然受既有三闸管(per-run budget 16 / 并发 semaphore 3 / 平台 delegation gate),`spawn_worker.is_parallel_safe=True` 已支持同轮并行派发;
4. 结果帧回来 → 退出分发轮,恢复完整工具注册表,普通循环做汇总/校验/写操作/拍板。

**降级路径**(永不阻断 run,与 planner 解析失败降级同哲学):分发轮里模型不发任何工具调用、直接输出文本 → 重试一次(再注入一条更硬的指令);仍抗拒 → 打 `dispatch_degraded` 计数 + warning,该批 delegate 步骤降级 inline,run 继续。`tool_choice="required"` 强制作为增强项,按厂商支持度探测后再上(glm/kimi 支持面未验证,不做一期依赖)。

### 4.3 worker 侧收敛

`synthesize_worker_spec` 追加两条剥离(现状只 clamp max_iterations、type 原样继承):
- `execution_mode` 强制 `standard`——worker 是聚焦执行体,不需要也不应该再跑分发轮(深度封顶已防递归,但省掉的是无意义的 planner 调用与分发判据);
- `workflow.type` 归一 `react`——worker 的 task 已是拆好的单步,再跑一次 advisory planner 纯浪费一次 LLM 调用。

### 4.4 reflection / 触发器 / 取消

- **reflection 协同**(拍板):reflect 修改 plan → plan-hash 变 → 分发判据下轮自然重新评估,新 delegate 步骤触发新分发轮。零额外机制。
- **触发器 run**:orchestrator 图对 run 来源无感,plan_first 天然对触发器 run 生效(Modal 建议清单里已提示成本)。
- **取消/deadline**:worker 已共享父 `CancellationToken` + `deadline_at`(现状),分发轮不引入新语义。

---

## 5. 审批 × 在飞 worker(PR-4,历史高危区)

### 5.1 现状 bug(无论 B-35 与否都该修)

侦察实锤:worker 继承父 `policies.approval_required_tools`(合成时不剥),`ask_for_approval` 无条件注册;worker 内触发审批闸 → 子图写 `pending_approval` 路由 END → `_child_run.py` 只识别 成功/MaxSteps/Cancelled 三种出口,把它当 `outcome="success"` 返回——**审批被静默吞掉,worker 假完成**。零测试覆盖。

### 5.2 一期方案:子 run 审批 = 软拒(结构化返回)

`run_child_to_result` 增加第四种出口识别:final state 含 `pending_approval` → 不装成功,返回 ToolResult:

> `[worker halted: tool 'X' requires human approval, which is unavailable inside a worker; handle this sub-task in the main conversation instead]`

- meta 带 `worker_approval_blocked: true` + 工具名;打新计数器(§7);
- 主模型得到明确信号,把该子任务收回主线执行——审批语义天然回到主线,与层 0 判据「写操作与拍板留主线」自洽;
- 分发轮指令已要求写操作步骤标 inline(§4.2),此路径是兜底而非常态。

### 5.3 否决:审批冒泡到父 run(一期不做,远期可再议)

父 run PAUSED → 用户批准 → 定位子 run resume → 父恢复。否决理由(如实摆,均为侦察证实的现状约束):
- 双层 checkpoint resume:子 run 有独立 `sub_thread_id`,审批 resume 缝(`aupdate_state` as_node)要跨父子两层图各做一次,现有审批面(API/前端/对外 SSE)全部只认单层 run;
- 并行 worker 的 PAUSED 语义无解干净:一个 worker 等审批时,同批其他在飞 worker 是继续跑(deadline 继续烧)还是挂起(无挂起机制,只有取消)?任一选择都造新状态机;
- 软拒方案已把审批场景收回主线,冒泡的增量价值 = 省一次「主线重做该子任务」,与复杂度不成比例。

---

## 6. UI 变更清单(admin-ui)

| 位置 | 变更 |
|---|---|
| RunBudgetSection「步数/流程」小节 | 新增 `workflow.execution_mode` 开关(switch,默认关);开启走确认 Modal(§3.3),关闭走提示(§3.4) |
| i18n | 新字段四键齐全(_label/_brief/_impact/_default)+ Modal 全部文案,en/zh-CN 双份 |
| PlanCard / PlanStepList / TurnPlanCard | delegate 步骤加徽标(如「委派」chip);PlanEditForm 支持编辑 execution |
| form_model.ts | `executionMode` 读写映射 + `normalizeForSubmit` 联动写入三字段 |
| groups.ts 搜索关键词 | budget 组追加 `plan_first` / `结构化执行` |
| 模板分层表 | `agent_template_resolve.py` 无需新条目(字段在 workflow 块内,已是 CAPABILITY tier)——PR-1 里用测试钉住这个假设 |

---

## 7. 观测

| 指标/事件 | 含义 |
|---|---|
| `expert_work_plan_first_dispatch_total` | 分发轮触发次数 |
| `expert_work_plan_first_dispatch_degraded_total` | 模型抗拒、delegate 批次降级 inline 次数(此计数高 = 结构闸被模型言语绕过,要回头看指令/thinking) |
| `expert_work_worker_approval_blocked_total` | 子 run 撞审批闸软拒次数(§5.2) |
| 既有 `expert_work_dynamic_worker_spawned_total` | 最终效果指标:plan_first 开启的 agent 上应显著非零 |
| run_event | 分发轮合成指令 hide_from_ui;worker 帧沿用现有 worker_timeline |

**验收探针**:sop2-designer 同款取数型任务(probe_dynamic_worker_v3.py 配方),plan_first 开启后 `spawned_total` 前后差 > 0 为过;glm-5.3 与 kimi-k3 各跑;degraded 计数为 0 为优。

---

## 8. PR 切分与工作量(5 PR,约 7-9 人日)

| PR | 内容 | 估时 | 风险 |
|---|---|---|---|
| PR-1 protocol 地基 | `WorkflowSpec.execution_mode` + `AgentSpecBody` 联动校验(§3.5)+ `PlanStep.execution` + `update_plan` schema 扩展(enum 带 type)+ worker 合成剥离(§4.3)+ 存量兼容测试(老 checkpoint/老 manifest 反序列化) | 1-1.5 天 | 低;`extra="forbid"` 部署顺序注意(§3.1) |
| PR-2 开关联动 UI + 后端 422 | RunBudgetSection 开关 + 确认 Modal(硬联动/建议清单)+ 关闭提示 + form_model 三字段一次写入 + i18n 双语 + 组件测试 | 1.5-2 天 | 中;Modal 一次写入要保住「单版本可 diff」 |
| PR-3 分发循环 | planner prompt 强化(形状判据+execution 标注)+ 分发轮(触发判据/去重 hash 通道/工具收窄/合成指令/降级重试)+ 单测(触发/去重/降级/reflection 改 plan 再触发) | 2-2.5 天 | 中高;工具收窄要过既有 registry 断言 |
| PR-4 审批 × worker | `_child_run` 第四出口(pending_approval → 软拒 ToolResult)+ 计数器 + 分发指令写操作边界文案 + 首个「worker 撞审批闸」测试(现状零覆盖) | 1.5-2 天 | **高**;审批/中断是历史高危区,改 `_child_run` 出口判据要全分支终审 |
| PR-5 观测 + 前端 plan 卡 + 验收 | 三计数器 + PlanCard delegate 徽标 + PlanEditForm execution 编辑 + e2e + 真栈探针跑双模型 + 文档 | 1 天 | 低 |

依赖:PR-2/PR-3/PR-4 都依赖 PR-1;PR-3 与 PR-4 可并行(不同文件面);PR-5 收尾。

---

## 9. 否决方案汇总

| 方案 | 否决理由 |
|---|---|
| 纯代码 dispatcher 节点(不经 LLM,代码遍历 plan 直接调 worker) | planner 的 step description 是一句话,不满足 task self-contained 要求(标识符/工具/输出格式);PlanStep 无依赖图,代码无法判断并行安全;task 展开必须由持有上下文的主模型做 |
| 审批冒泡到父 run(一期) | 见 §5.3 |
| `workflow.type` 加第四值 `plan_first` | 图拓扑与执行纪律两个正交维度混进一个枚举;存量三值兼容面被搅动;UI 联动语义(react→plan_execute 自动改)讲不清 |
| 开启时静默归一不一致配置(后端自动改而非 422) | 绕过 UI 直改 YAML 的用户会被偷改配置,违反「配置所见即所得」;422 带字段级报错更诚实 |
| 关闭时自动回退 type/dynamic_workers | 会吞用户开启后的手动调整;提示手动改是唯一不丢信息的选择 |
