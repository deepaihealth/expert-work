# Stream CM — 上下文与记忆管理强化（设计先行）

> **背景**：2026-06-09 基于系列文章《Harness 工程》对 OpenClaw / deer-flow / Hermes 做了上下文与记忆管理的源码级对比（`docs/research/2026-06-09-context-memory-management-comparison.md`），并对 Expert Work 现状做了 10 维取证 + 2024–2026 论文/工程博客二次评估（`docs/research/2026-06-09-expert-work-context-memory-improvement-framework.md`，v2 含外部证据）。本 Stream 把框架报告里确认的改进条目落成可执行设计。
>
> **总基调（已拍板）**：**② 混合——DB 为真相源 + workspace 文件投影**，用**单向错时双流**实现（turn 末 DB→file 投影 / turn 始 file→DB 受控 ingest），**不做对称双向同步**（外部证据判为反模式）。
>
> **设计先行规则**（[memory:design-first-iteration]）：所有总体架构 / 跨切面接口 / Mini-ADR 在本文件锁定；每条 gap PR 在对应章节基础上做局部细化。
>
> **零债收尾规则**（[memory:zero-tech-debt]）：每条交付收尾 6 条全过 —— 无 TODO / 测试达标 / 文档同步 / 可观测齐全 / CI 全绿 / bug 不遗留。
>
> **本文件状态**：CM-0（地基，§2）+ CM-1（运行时 error-as-guidance，§3）+ CM-2（working memory 滑动窗口，§4）+ CM-3（压缩前 flush，§5）+ CM-4（reranker 接通，§6）+ CM-5（可恢复压缩，§7）+ CM-6（时间衰减+MMR，§8）+ CM-7（结构化摘要+写入显式操作，§9）+ CM-8（plan UI 通道+resume ingest 修复，§10）+ CM-9（effort 档位+撞限升档，§11）+ CM-N5（评测基线，§12）+ CM-10（跨厂商思考深度统一，§13）+ CM-11（事件驱动升档：工具错/目标变更触发，§14）详设已锁定。

---

## 1. 范围 & 边界

### 1.1 In-scope（映射框架报告 A/B/C/N 档）

| ID | 框架条目 | Gap | 交付 | 优先级 | Mini-ADR |
|----|---------|-----|------|--------|---------|
| **CM-0** | C0 + N1 | 状态内化、无文件投影；workspace 卷已有但状态不可见、不可手改 | 状态↔workspace 文件投影 + 单向错时双流 + recitation 复诵 | **先行** | CM-A1…A8（本文详设） |
| CM-1 | A1 | 运行时主循环工具失败无 grounded 恢复建议（SE-12 是离线 skill 进化，L-4 只覆盖文件 mutation） | 通用工具失败→结构化恢复 advisory 注入主循环 | P0 | CM-B1…B6（§3 详设） |
| CM-2 | A2 | 无"保留最近 N 轮"廉价前置闸，轻溢出每次走 LLM 摘要 | `agent_node` compressor 前加滑窗截断（保 ToolCall↔ToolResult 配对） | P0 | CM-C1…C6（§4 详设） |
| CM-3 | A3 | compressor 丢弃中段前无 flush，`memory_writeback` 只在 run 末 | 压缩前回调 → 复用 writeback 通道中途落盘 | P1 | CM-D1…D6（§5 详设） |
| CM-4 | B5 | rerank_provider/model 配置预留但检索路径无 rerank 调用 | memory recall Hybrid 召回后接 cross-encoder rerank | P1 | CM-E1…E6（§6 详设） |
| CM-5 | B6 | 超大工具结果 char-cap 截断丢弃，不可找回 | 超限结果外部化 workspace 文件 + 虚拟引用 footer + read 类豁免（"可恢复压缩"通用原则） | P1 | CM-F1…F6（§7 详设） |
| CM-6 | B4 | 对称 RRF，无 MMR 去冗余、无时间衰减 | 时间衰减进 retrieve()（两 store）+ MMR 在 orchestrator rerank 后殿后（管线顺序修正见 §8） | P1 | CM-G1…G6（§8 详设） |
| CM-7 | B7 | `<context-summary>` 缺"背景非指令"强语义、二次压缩链式重生成、writeback 直写同义堆积矛盾不废弃 | 摘要 preamble+三段结构+增量更新 ① + run 末 writeback 显式 ADD/UPDATE/DELETE/NOOP ② | P2 | CM-H1…H6（§9 详设） |
| CM-8 | C8 | plan 无 UI 通道（前后端全缺）+ approval resume 不经 ingest（暂停期 PLAN.md 编辑丢失）+ approval E2E 残债；~~modify UX 缺口~~ 已被早期 H.x 实现（§10 范围修正） | plan 读写 API + RunDetail PlanPanel（非 RUNNING 可写）+ resume ingest 修复 + approval E2E | P2 | CM-I1…I6（§10 详设） |
| CM-9 | C9 + N4 | adapter 零 thinking/effort 支持 + 撞限无升档（opus-4-8 还有 temperature 400 隐患）；~~iteration budget~~ ~~指纹去重~~ 已实现，~~per-run plan mode~~ 砍（§11 范围修正） | `ModelSpec.effort/adaptive_thinking` + 目录能力位 + 撞限双信号升档（escalated caller） | P2 | CM-J1…J6（§11 详设） |
| CM-N5 | N5 | 检索/记忆改动无回归基线 | LongMemEval + LoCoMo 自测纳入 eval（贯穿 CM-4/6/7）：分层两态（检索层机械回归 + 端到端 QA 横比） | 贯穿 | CM-K1…K7（§12 详设） |
| **CM-10** | C9 延伸 | CM-9 effort/撞限升档 anthropic 专属，qwen 等 8 provider 拿不到增益 | 三形态能力位（effort/budget/toggle）+ factory 翻译层 + extra_body 通道 + 升档梯子 per 形态 | P2（2026-06-10 立项即做） | CM-L1…L7（§13 详设） |

### 1.2 设计选择对比（"为什么不全抄"）

| 外部 pattern | 借/不借 | 理由 |
|---|---|---|
| 文件作唯一真相源（OpenClaw bootstrap / Hermes MEMORY.md / Manus file-as-context） | ❌ 不借 | 与多租户查询/隔离/可检索冲突；Expert Work 选 DB 权威（STREAM-J §8） |
| 对称双向 file↔DB 同步 / 最后写赢 | ❌ **反模式** | Oracle/同步工程共识：单向 master→target；对称双写需 trump rules，已知脆弱点 |
| 进程级文件锁 + PID watchdog（OpenClaw） | ❌ 不借 | Expert Work 用 PG 事务 / advisory lock；并发已被 per-(tenant,user) 单 warm session 串行化 |
| **DB 权威 + 单向投影 + 受控 ingest**（Deep Agents CompositeBackend 思路） | ✅ 借 | 拿到文件透明/可干预红利，不丢 DB 权威；任一时刻单向流动 |
| **Recitation 复诵 todo 到上下文尾**（Manus/Claude Code） | ✅ 借 | 低成本抗 long-context lost-in-the-middle |
| 可恢复压缩"留引用不丢源"（Manus / Anthropic tool-clearing） | ✅ 借（CM-5） | 存储基底=workspace 文件（取证修正：artifact 表只是元数据注册，内容基底本就是 workspace 文件；ObjectStore M0 未接 artifact 内容） |
| Dreaming / 按日期 Episodic 编年落盘 | ❌ 不借 | 文章理想化；consolidator(transient→consolidated) 已覆盖等价价值 |

### 1.3 Out-of-scope（明确推迟）

| 推迟项 | 落地 | 备注 |
|-------|------|------|
| 知识图谱记忆（Zep/Graphiti bi-temporal KG） | 待评估 | 仅当出现强时序/事实纠正需求；收益曾被高估（Zep 84%→58%） |
| A-MEM 全量 Zettelkasten 自组织 | 观望 | 研究原型；其"结构化 note + 显式操作"思想已被 CM-7 局部吸收 |
| 可学习压缩 token / 硬 prompt 压缩（LLMLingua-2） | 前沿引用 | 前者需训练；后者作"LLM 摘要"低成本替代候选，非首选 |
| 推理层 KV-cache 压缩（PyramidKV 等） | 不在范围 | 属推理层，与应用层上下文管理正交 |
| 每 turn 强制 file→DB ingest（侦测 run 中途手改） | CM-0 后评估 | 默认 ingest 只在 run 始 + resume（见 CM-A4），成本/收益自测后再决定是否每 turn |

### 1.4 验收（每条 CM-x 独立 Exit）

每条 CM-x 收尾零债 6 条全过；CM-0 的具体验收见 §2.10。

---

## 2. CM-0 详细设计 —— 状态↔workspace 文件投影 + 单向错时双流（地基）

### 2.1 关键约束（接缝核准结论，已源码核准）

| 约束 | 事实 | file:line |
|---|---|---|
| **orchestrator/supervisor 无卷写权** | workspace 是 docker named volume，仅挂进沙盒容器；supervisor 只能用一次性只读容器 `read_volume_file` 读卷，**无写卷 API** | `expert-work-runtime/.../runtime_provider.py:109-122`（`--volume {vol}:/workspace`）；`sandbox-supervisor/.../docker_client.py:129-157`（`read_volume_file`，`{volume}:/ws:ro`） |
| **唯一卷写通道 = warm sandbox** | `file_ops.write_file` 走 `run_in_sandbox`，在 warm session 内跑 Python snippet（temp+atomic replace），毫秒级 | `orchestrator/.../tools/file_ops.py:12-18`（设计注释明示放弃专用 supervisor 文件 API，改 warm session snippet） |
| **turn 边界** | turn 始 = `agent_node` 入口；turn 末 = `tools_node` 返回前 | `graph_builder/builder.py:212`（agent_node）/ `:507`（tools_node return） |
| **run 边界** | `memory_recall` 在 run 启动一次（entry chain）；`memory_writeback` 在 run 结束一次（exit chain） | `graph_builder/memory.py:187-219` / `:240-319`；`builder.py:517-533` |
| **plan 持久化** | `update_plan` 返 `ToolResult.state_updates={"plan": ...}`，经 `TOOL_ALLOWED_STATE_KEYS` 白名单 merge 进 checkpointer | `tools/update_plan.py:140-145`；`tools/registry.py:192-194`（白名单 `{"plan","subagent_invocations","promoted_tools"}`） |
| **PlanStep 无 status** | `PlanStep` 仅 `id`+`description`，**无完成态**——TODO.md 勾选需补字段 | `protocol/plan.py:15-21` |
| **artifact 范式可参考** | 内容留卷、元数据进 `artifact` 表（orchestrator 可直接写 DB）；下载经 supervisor 代理 | `orchestrator/.../tools/artifact.py:1-13`；`artifact/base.py:29-49` |
| **audit 接口** | `AuditAction` 枚举 + `audit_log.append(entry)`；已有 `WORKSPACE_*` 系列 | `protocol/audit.py:100-105`；`audit_log/base.py:36-42` |

> **结论**：投影 DB→file **必须经 warm sandbox `file_ops` 写**（orchestrator 直接写卷不可行）；ingest file→DB 经 supervisor `read_volume_file` 只读读卷 + orchestrator DB 事务写。这与 artifact"内容在卷、元数据在 DB"范式一致。

### 2.2 同步模型：单向错时双流（CM-A2）

任一时刻只有单向流动，DB 恒为真相源：

```
run 启动
  └─ memory_recall 后、首个 agent_node：file→DB 受控 ingest（一次）
       读 /workspace/PLAN.md·TODO.md（经 read_volume_file）
       → diff vs 上次投影快照 → 校验（schema/size/注入扫描）
       → 有合法变更 ⇒ DB 事务写 AgentState.plan（DB 权威）+ audit STATE_INGESTED

每个 turn（agent_node 入口）
  └─ N1 recitation：把当前 PLAN/TODO 摘要注入上下文尾部（纯 in-context，不读写文件）

每个 turn 末（tools_node 返回前）
  └─ DB→file 投影（only-if-changed）
       若 plan 本 turn 变更（hash 比对）⇒ 经 file_ops 写 /workspace/PLAN.md·TODO.md
       + audit STATE_PROJECTED；未变则跳过（避免无谓 sandbox 往返）

run 结束（memory_writeback 后）
  └─ MEMORY.md 投影（一次）：把 consolidated/top recalled 记忆投影为只读视图
```

**为什么 ingest 默认只在 run 始 + resume，而非每 turn**（CM-A4）：Expert Work 的人机协同是 **approval-pause 驱动**（J.8），人最自然在 run PAUSED 或两次 run 之间手改文件。run 始 + resume-from-pause ingest 覆盖该场景且零每-turn 卷读成本。每-turn ingest 列入 §1.3 推迟项，自测成本/收益后再决定。

### 2.3 投影什么（path-addressable + compaction-stable 不变量）

| 文件 | 来源（DB 真相源） | 方向 | 备注 |
|---|---|---|---|
| `/workspace/PLAN.md` | `AgentState.plan`（goal + steps + status checkbox） | **双向（投影 + ingest）—— 唯一 ingest 源** | round-trippable：`render_plan_md`↔`parse_plan_md` 互逆；人改 goal/步骤/勾选 → ingest 回灌（CM-A5/A5b） |
| `/workspace/TODO.md` | plan steps + status | **单向只读投影** | 编辑被忽略（要改去改 PLAN.md）；保留为人友好的扁平 checklist 视图 |
| `/workspace/MEMORY.md` | `memory_item`（consolidated + top recalled） | **单向只读投影** | 不 ingest（记忆有独立写路径 memory_writeback + consolidator） |

> **设计细化（CM-A5b，对 §2.3 原"TODO.md 双向"的修正）**：原设计 PLAN.md + TODO.md 都可 ingest，双源回灌易冲突（人同时改两处谁赢）。收敛为 **PLAN.md 单一 ingest 源**——它带 status checkbox 可精确 round-trip，是唯一可编辑回灌的文件；TODO.md/MEMORY.md 只读。这消除双源歧义，且 parse 失败永远丢弃文件改动、DB 保持权威（CM-A8 安全）。

不变量：① **path-addressable**——固定路径，内容可随时从 DB 重建；② **compaction-stable**——投影文件不进 LLM 上下文（只 recitation 摘要进），故不受 compaction 影响、丢失可重建。

### 2.4 挂钩点（已核准位置）

| 钩子 | 位置 | 频率 | 可访问 |
|---|---|---|---|
| ingest（file→DB） | **entry-chain `workspace_ingest` 节点**（planner 后、agent 前），实现为独立节点而非 agent_node 入口判断——entry chain 每 ainvoke 跑一次 = run 始/resume，天然满足"一次"语义 | run 始一次 | state, config（tenant/user/run），返回 `{"plan": ...}` 回灌 |
| recitation（N1） | `agent_node` 内、plan 注入处附近（`builder.py:239-241`） | 每 turn | 当前 plan/todo，注入 messages 尾部 |
| 投影（DB→file） | `tools_node` 返回前（`builder.py:507`），only-if plan 变更 | 每 turn（条件触发） | state + accumulated_state（含本 turn plan 变更） |
| MEMORY.md 投影 | 同 tools_node 投影钩子（`recalled_memories` 在 state，only-if-changed 只写一次） | 随投影 | — |

投影/ingest 纯逻辑封装到 **`orchestrator/context/workspace_projection.py`**（`WorkspaceProjector` / `WorkspaceIngester`）；真沙盒读写适配（`SandboxWorkspaceWriter`/`SandboxWorkspaceReader`）在 `tools/file_ops.py`；ingest 节点在 `graph_builder/workspace_ingest.py`（`make_workspace_ingest_node`）。builder.py 仅在钩子点/entry chain 调用，保持节点函数瘦。

### 2.5 数据/协议变更

1. **`PlanStep` 加 status**（`protocol/plan.py`，CM-A5）：
   ```python
   PlanStepStatus = Literal["pending", "in_progress", "completed"]
   class PlanStep(BaseModel):
       id: str
       description: str
       status: PlanStepStatus = "pending"   # 新增，默认 pending 向后兼容
   ```
   - 无迁移：plan 存 AgentState（checkpointer dill），非 DB 表；旧 checkpoint 反序列化走默认值。
   - `update_plan` 工具支持设置 status；TODO.md 渲染 `- [x]/[ ]` 由 status 决定。

2. **`AgentState` 加投影游标**（`orchestrator/state.py`）：
   ```python
   last_projection_hash: NotRequired[str | None]   # 上次投影内容 hash，only-if-changed + ingest diff 基准
   ```
   覆写 reducer；用于 §2.2 的 hash 比对与 ingest diff。

3. **`AuditAction` 双处 Literal 同步加**（[memory:audit-literal-drift]——protocol + control-plane 两处都改）：
   - `STATE_PROJECTED = "state:projected"`、`STATE_INGESTED = "state:ingested"`。

4. **新模块**：`orchestrator/context/workspace_projection.py`（projector/ingester + 渲染/解析/校验纯函数，便于 unit 测）。

### 2.6 冲突与校验（DB 权威）

- **ingest 校验**：size 上限、Markdown→Plan 解析容错、**复用记忆写入的注入扫描**（`scan_for_threats(scope="strict")`，与 `_redact_memory` 同源）拒绝注入式文件内容。
- **漂移裁决**：ingest 时文件 vs DB 漂移 —— **DB 权威**，仅当文件存在合法显式编辑（diff vs `last_projection_hash` 且通过校验）才以 DB 事务回灌；解析失败/校验不过 ⇒ 丢弃文件改动 + 记 audit + 保留原文供人审查（不静默覆盖人的编辑：记 warn）。
- **无锁**：per-(tenant,user) 单 warm session 已串行化；DB 写用事务兜底。

### 2.7 N1 Recitation（已实现，CM-0 PR3）

**关键现状**：尾部复述机制**已存在** —— `_inject_plan`（builder.py）每 turn 用 `render_plan(plan)` 把 plan append 到上下文**尾部**（非 system，保 L-1 cache prefix），这正是 Manus/Claude Code 的 recitation 范式。所以 N1 不是新加一个注入（会与 `_inject_plan` 重复，违反 CM-A7），而是**让这个已有的尾部复述带上进度**，使注意力聚焦在未完成步骤：

- **`render_plan` 改 status-aware**：每步渲染 `- [ ]/[~]/[x] N. desc`（用 PR1 的 `PlanStep.status`），复述即进度。仅被 `_inject_plan` 消费，与 `planner.parse_plan` 独立，改格式安全。
- **`update_plan` 支持每步 status**：steps 接受 string（→pending）或 `{description, status}` 对象，让 **agent 能标记进度**（否则 status 只有人改 PLAN.md 才变 → recitation 进度空转，是弱版）。这闭合"agent 标记进度 → recitation 显进度 → 注意力聚焦"的环。
- **token gauge**：`expert_work_cm_recitation_chars`（gauge）观测每次复述字符数，盯 plan 膨胀。

> 设计修正：原 §2.7 设想"新加一个未完成项摘要注入并与 _inject_plan 去重"。落地发现 `_inject_plan` 已做尾部复述，去重的正解 = 不新增注入、直接增强 `render_plan` 进度可见性（CM-A7 本意）。

### 2.8 边界情况

| 场景 | 处理 |
|---|---|
| 无 warm sandbox（纯 chat agent，无沙盒工具） | 投影/ingest **best-effort no-op**，log debug，不起沙盒、不失败 run |
| 沙盒冷启动/不可用 | 投影 best-effort：记 `expert_work_cm_projection_failed_total`，不阻塞 turn |
| plan 为 None（非 plan_execute workflow） | 不投影 PLAN/TODO；MEMORY.md 仍可投影 |
| ingest 文件被删/不存在 | 视为无编辑，跳过 |
| 大文件/恶意内容 | size cap + 注入扫描拒绝（§2.6） |

### 2.9 可观测（零债"可观测齐全"）

- `expert_work_cm_projection_total{file}` / `expert_work_cm_projection_failed_total{file,reason}`
- `expert_work_cm_ingest_total{result="applied|rejected|noop"}` / `expert_work_cm_ingest_drift_total`
- projection / ingest p95 延迟（含 sandbox 往返）
- 每次 projected/ingested emit log（tenant/user/thread）+ audit（STATE_PROJECTED/INGESTED）
- recitation 注入 token 数 gauge（防膨胀）

### 2.10 测试 & 验收（CM-0 Exit）

- **unit（≥85%）**：plan→PLAN.md/TODO.md 渲染、Markdown→Plan 解析、status 勾选映射、hash only-if-changed 跳过、ingest 校验（注入/超限拒绝）、recitation 去重与尾部注入、no-sandbox no-op。
- **integration（关键路径 ≥70%，真沙盒）**：真 warm session 写卷→`read_volume_file` 读回往返；"人手改 PLAN.md → resume → ingest 回灌 DB"；漂移→DB 权威 + audit；解析失败→丢弃文件改动不覆盖。
- **零债 6 条**：无 TODO/FIXME；docs（本节）与实现一致；metric+log+trace+audit 齐全；CI 8/8 + CodeQL 无新增 high；bug 不遗留。
- **能力指标**：projection only-if-changed 命中率（plan 未变 turn 应跳过≈100%）；ingest 回灌正确性。

### 2.11 Mini-ADR（CM-0 锁定）

| ID | 决策 |
|---|---|
| **CM-A1** | 投影 DB→file 经 warm sandbox `file_ops` 写（orchestrator/supervisor 无卷写权，已核准）；ingest 经 `read_volume_file` 只读读 + orchestrator DB 事务写 |
| **CM-A2** | 单向错时双流：turn 末 DB→file 投影 / run 始 file→DB 受控 ingest；拒对称双向同步 |
| **CM-A3** | 投影 only-if-changed（plan hash 比对，存 `AgentState.last_projection_hash`），避免每 turn 无谓 sandbox 往返 |
| **CM-A4** | ingest 默认仅 run 始 + resume-from-pause（人机协同是 approval-pause 驱动）；每-turn ingest 推迟，自测后再定 |
| **CM-A5** | `PlanStep` 加 `status`（pending/in_progress/completed，默认 pending），支撑勾选；无迁移（plan 在 checkpointer 非 DB 表） |
| **CM-A5b** | **PLAN.md 单一 ingest 源**：带 status checkbox 可精确 round-trip（`render_plan_md`↔`parse_plan_md` 互逆），是唯一可编辑回灌的文件；TODO.md/MEMORY.md 只读。消除双源回灌冲突；parse 失败→丢弃文件改动、DB 权威（修正 §2.3 原"TODO.md 双向"） |
| **CM-A6** | `AuditAction` 加 `STATE_PROJECTED`/`STATE_INGESTED` —— 核准后 `AuditAction` 是**单源 StrEnum**（仅 protocol，无 control-plane 镜像；drift 只适用 `ResourceType` 这个双处 Literal）；投影 `resource_type` 复用现成 `user_workspace`，**不新增 ResourceType**。按项目纪律枚举成员随**发射 PR**加（STATE_PROJECTED→PR2 接线、STATE_INGESTED→PR2 ingest），避免"定义即未用" |
| **CM-A7** | recitation 注入 plan 尾部摘要、与 `_inject_plan` 去重；非 system 区保 L-1 cache prefix |
| **CM-A8** | 投影/ingest best-effort：无沙盒/冷启动/解析失败均不阻塞 run，记 metric+log；DB 始终权威，人的编辑校验不过则保留原文 + warn 不静默丢 |

### 2.12 PR 切分（CM-0）

1. **CM-0 PR1 — 投影纯核心（CI 全测）**：`context/workspace_projection.py`（render 纯函数 + `WorkspaceProjector` + `WorkspaceFileWriter` Protocol seam + only-if-changed，projector 收 `last_digest` 参数）+ `PlanStep.status`（CM-A5）+ 结构化日志可观测（与 compressor 同款，非 in-module Prometheus）+ unit。**不接图、不引未发射枚举、不加未读写的状态字段**（pure-core-先行，对齐 SE-4a/SE-5a）。
2. **CM-0 PR2a — 投影接线**（已实现）：真 `SandboxWorkspaceWriter`（在 `tools/file_ops.py`，包 `build_write_wrapper`/`run_in_sandbox`，结构化满足 `WorkspaceFileWriter`）+ `build_react_graph` 新增可选 `workspace_writer_factory`（per-turn 绑 ctx）+ `tools_node` return 前 best-effort 投影钩子（`_project_workspace_state`）+ `AgentState.last_projection_hash`（投影游标）+ `AuditAction.STATE_PROJECTED` 发射（`_emit_state_projected_audit`，resource_type=`user_workspace`）+ `expert_work_cm_projection_total{outcome}` + agent_factory gate（persistent_workspace ∧ supervisor_client）。**MEMORY.md 无需单独 run 末投影**——`recalled_memories` 在 state、tools_node 每 turn 投影、only-if-changed 保证只写一次。单测：SandboxWorkspaceWriter（RecordingSupervisorClient 真 snippet 往返）+ graph wiring（fake writer 驱动 build_react_graph）。
3. **CM-0 PR2b-i — ingest 纯核心（CI 全测）**（已实现）：`render_plan_md` 改带 status checkbox 使 PLAN.md round-trippable + `parse_plan_md`（render 的精确逆）+ `WorkspaceFileReader` Protocol seam + `WorkspaceIngester.ingest_plan`（读 PLAN.md→parse→仅当 ≠ current 才返回候选；read 失败/parse 失败/无变更均 no-op，永不 raise）+ TODO.md/MEMORY.md 转只读（CM-A5b）+ unit（round-trip / 勾选编辑 / 无 goal 或无 step→None / 无变更 no-op）。**不接图**。
4. **CM-0 PR2b-ii — ingest 接线**（已实现）：`SandboxWorkspaceReader`（`tools/file_ops.py`，包 `build_read_wrapper`/`run_in_sandbox`，not_found→None）+ `graph_builder/workspace_ingest.py:make_workspace_ingest_node`（entry-chain，planner 后 agent 前，每 ainvoke 一次=run 始/resume）+ `build_react_graph` 可选 `workspace_ingest_node` 参 + 注入扫描校验（`scan_for_threats(scope="strict")` 扫 goal+步骤描述，命中即拒、DB 权威）+ `AuditAction.STATE_INGESTED` 发射 + `expert_work_cm_ingest_total{outcome}` + agent_factory gate（同投影）。6 unit（SandboxWorkspaceReader 真 snippet 往返/not_found/io_error + graph wiring：人改回灌 / 无变更 no-op / 注入拒绝）。真 live-sandbox e2e 归 integration/手动（CI 无沙盒凭证，同 PR2a 边界）。
5. **CM-0 PR3 — N1 recitation**（已实现）：尾部复述机制已由 `_inject_plan` 提供 → PR3 让其**进度可见**：`render_plan` 改 status checkbox + `update_plan` 支持每步 status（agent 标记进度，闭合进度环）+ `expert_work_cm_recitation_chars` gauge + unit（render status / update_plan string|object status / 无效 status→pending）。**→ CM-0 全部完成**。

> 每个 PR 在本 §2 基础上局部细化；ITERATION-PLAN 增 Stream CM backlog，ship 后回填 `[x]`+PR 号（[memory:iteration-plan-sync]）。

---

## 3. CM-1 详细设计 —— 运行时 Error-as-Guidance（工具失败→grounded 恢复 advisory）

> **目标**：ReAct 主循环里任一工具失败时，按错误类型 + 工具能力，向模型注入一条**结构化、有倾向性、grounded 于真实执行信号**的恢复建议，对抗模型"瞎猜路径已生效 / 原样重试同一失败调用"。映射框架报告 A1（实证：结构化错误恢复率 >85% vs 模糊信号 17%）。
>
> **核心策略**：**泛化已有的 L-4 mutation advisory 机制**——不新造注入管道，而是把 L-4"仅 save_artifact 落盘校验 → `failed_mutations` 通道 → 下一 turn `<mutation-advisory>` 尾部注入 → 注入后 reset"这套**已验证的范式**，泛化到**所有工具的失败**，并把 L-4 的"mutation 未落盘"收敛为新分类器的一个错误类（零债，不留并行机制）。

### 3.1 关键约束（接缝核准结论，已源码核准）

| 约束 | 事实 | file:line |
|---|---|---|
| **工具失败已被三层 catch 转 ToolMessage** | `_invoke_tool` catch 工具自身异常、`_dispatch_tool` catch `ToolNotFoundError`(未知工具) 与 middleware block，全部 → `ToolMessage(content=_format_error(exc), status="error")`，**无分类、所有异常等价** | `graph_builder/builder.py:1090-1108`(`_invoke_tool`)、`:823-880`(`_dispatch_tool`)、`:1116-1120`(`_format_error`：`[tool error] {Type}: {summary≤500}`) |
| **L-4 注入范式（待泛化）** | `classify_mutation`(仅 `save_artifact`) → tools_node 收集 `failed_mutations` 进 state → 下一 agent_node `_build_mutation_advisory` 拼 `<mutation-advisory>` `HumanMessage` 注入**尾部**(保 L-1 cache prefix) → 持久化后 `failed_mutations:[]` reset | `tools/mutation_classifier.py:41-91`；`builder.py:508-525`(收集)、`:277-289`(注入)、`:347-354`(持久化+reset)、`:670-694`(`_build_mutation_advisory`) |
| **ToolSpec 带能力元数据** | `idempotent: bool`、`side_effect: "read_only"/"reversible"/"irreversible"`(`resolved_side_effect`)、`is_read_only` —— 让"是否建议重试"grounded（幂等/只读才安全重试，不可逆非幂等需先核实状态） | `tools/registry.py:40`(`SideEffectLevel`)、`:119/:128`(`side_effect`/`resolved_side_effect`)、`:113`(`idempotent`) |
| **dispatch 三种失败 outcome** | `_record_tool_metrics` 已区分 `ok/error/blocked`：`error`=工具/未知工具异常，`blocked`=middleware(approval/guardrail)拦截 | `builder.py:823`(blocked)、`:1093`(error)；`_tool_call_total{tool,outcome}` |
| **ToolResult 无 error 字段** | 错误走异常路径(非 ToolResult)；`ToolResult{content,meta,state_updates,refund_iterations}` 无 error/is_error —— CM-1 不改 ToolResult 契约，在 catch 点(有 `exc` 真类型)分类 | `tools/registry.py:197-238` |
| **失败仍计 1 步（不退款）** | catch 路径返回 `refund_inc=0`：失败的工具调用仍是 user-visible 尝试，照常消耗 iteration（与 L-5 退款语义一致，不动） | `builder.py:1100-1108`(error 返回 `0`) |
| **运行时无错误分类体系** | `errors.py` 仅 factory 期异常(`AgentFactoryError`/`SkillNotFoundError`)，主循环无任何 `ClassifiedError`/recovery —— CM-1 从零建（参考 Hermes 结构，不抄其 API-层 taxonomy） | `orchestrator/errors.py` |

> **结论**：CM-1 = 在 catch 点（`exc` 真类型在手，信号最丰富）做**确定性分类** → 产出 `ClassifiedToolError` → 经统一 `tool_failures` 通道（泛化 `failed_mutations`）→ 下一 turn `_build_recovery_advisory` 拼 `<recovery-advisory>` 尾部注入。L-4 的 mutation 校验作为"未落盘"一类收敛进来。

### 3.2 设计：分类 → 通道 → 注入（泛化 L-4，三段）

```
tools_node（每个失败的工具调用）
  ├─ 错误路径(status="error")：在 _invoke_tool/_dispatch_tool catch 处，
  │    classify_tool_error(tool_name, args, exc, spec) → ClassifiedToolError
  └─ 成功但语义未落地：classify_mutation 现逻辑 → ClassifiedToolError(class="mutation_not_landed")
  ⇒ 聚合进 state["tool_failures"]: list[ClassifiedToolError]   ← 泛化 failed_mutations

下一个 agent_node 入口
  └─ 若 tool_failures 非空：_build_recovery_advisory(tool_failures) → HumanMessage
       注入 messages 尾部（非 system，保 L-1 cache prefix；与 N1 recitation 同区错开拼接）
  └─ 持久化该 advisory + response；返回 {"tool_failures": []} reset（单次注入，不重复）
```

不变量沿用 L-4：① 注入在**尾部 HumanMessage**（不污染 system，保 prompt-cache）；② **单次注入即 reset**（下一步无残留）；③ 失败信号在 state 上跨节点传递（tools_node 产、agent_node 消费）。

### 3.3 工具错误 taxonomy（grounded、确定性，无 LLM）

分类**纯由真实信号推导**（异常类型名 + 错误文本模式 + `ToolSpec` 能力），不调模型、不靠内省——直接落实框架报告修订①"注入须源自真实工具/执行信号"。

| `error_class` | grounded 信号 | 恢复倾向（advice 模板核心） | retryable |
|---|---|---|:---:|
| `unknown_tool` | `ToolNotFoundError` | 该工具不存在；从可用工具里选，**勿原样重试** | 否 |
| `invalid_arguments` | `ValueError`/校验类异常 + args 被拒文本 | 参数被拒(附 why)；**修正参数**再调，勿重复同一调用 | 否（改参后可） |
| `blocked_by_policy` | dispatch outcome=`blocked`（middleware/approval/guardrail） | 被策略/审批拦截；**等待审批或上报用户**，勿绕过重试 | 否 |
| `resource_not_found` | `FileNotFoundError`/"not found"/404 类文本 | 目标(路径/id)不存在；**先核实存在性**再操作 | 否（核实后可） |
| `permission_denied` | `PermissionError`/auth/403 类文本 | 权限不足；**勿暴力重试**，上报 | 否 |
| `transient` | `TimeoutError`/连接/沙盒不可用/5xx 类文本 | 暂时性失败；**幂等/只读可重试一次**(读 `spec`)，仍失败则上报 | 是（受 spec 约束） |
| `mutation_not_landed` | L-4 现逻辑：成功态但写未落地 | 路径**未**含所请求内容，勿假定已生效；重试或上报（沿用 L-4 文案） | 是 |
| `unknown` | 兜底（不可分类） | 原因不明；检视错误、**考虑替代手段**，避免原样重试 | 否（保守） |

分类实现 = 异常类型名优先 + 错误文本小写模式匹配兜底（确定性 if/elif 链，无正则灾难）。新增类只在此表扩。

### 3.4 恢复建议构造（grounded 约束）

- **retryable 受 `ToolSpec` 约束**：即便 `transient`，也仅当 `spec.idempotent` 或 `resolved_side_effect=="read_only"` 才建议"可安全重试"；不可逆非幂等工具一律"重试前先核实状态"。这让"建议重试"不会诱导模型对不可逆副作用工具盲目重放。
- **advice 是模板化结构文本**（错误类 → 固定话术 + 注入工具名/参数摘要/错误摘要），非自由生成；保证可预测、可测、token 受控。
- **不含 fingerprint 升档**：同工具同参连续失败的"停止重试/换路"升档属 **CM-9 N4（loop 指纹去重）**，CM-1 advice 文案仅以"勿原样重试"软提示，不引入 per-thread 指纹存储（避免越界，保 surgical）。

### 3.5 与 L-4 收敛（零债，不留并行机制）

框架报告写明 CM-1"**泛化 L-4**"。落地为**收敛而非并行**：

- `MutationOutcome{tool_name,path,landed,error}` 的失败语义 → 映射为 `ClassifiedToolError(error_class="mutation_not_landed", tool_name, summary=error, path)`。
- `failed_mutations` 状态键 → 由统一 `tool_failures: list[ClassifiedToolError]` 取代；`classify_mutation` 现逻辑保留为"未落盘"这一类的**贡献者**（成功路径专用：status 非 error 但写未落地，这是 CM-1 错误路径覆盖不到的语义失败，必须保留）。
- `_build_mutation_advisory` 的 `<mutation-advisory>` 文案 → 并入 `_build_recovery_advisory` 的 `<recovery-advisory>` 块，mutation 类保留原措辞（行为不回归，L-4 测试相应更新）。
- 因是 checkpointer 内 transient 每-step 状态（非 DB 表、每 agent step reset），**无迁移**；旧 checkpoint 的 `failed_mutations` 键由 reducer 默认 `[]` 兜底。

### 3.6 数据/协议变更

1. **新模块 `orchestrator/tools/error_classifier.py`**（与 `mutation_classifier.py` 真同层 = `tools/`；PR1 误放 `graph_builder/`，PR2 接线时发现 `AgentState` 运行时需 `get_type_hints` 解析 `ClassifiedToolError`，从 `graph_builder/` 运行时导入会触发 `state→graph_builder→builder→state` 环，故 PR2 `git mv` 到 `tools/` 下层根治）：
   ```python
   ToolErrorClass = Literal[
       "unknown_tool", "invalid_arguments", "blocked_by_policy",
       "resource_not_found", "permission_denied", "transient",
       "mutation_not_landed", "unknown",
   ]
   @dataclass(frozen=True)
   class ClassifiedToolError:
       tool_name: str
       error_class: ToolErrorClass
       summary: str               # 错误摘要（≤cap）
       retryable: bool
       advice: str                # 模板化恢复建议（注入 advisory 的正文）
       path: str | None = None    # mutation 类沿用
   def classify_tool_error(tool_name, args, exc_or_message, spec) -> ClassifiedToolError: ...
   def render_recovery_advisory(failures: list[ClassifiedToolError]) -> str: ...  # 纯函数，拼 <recovery-advisory>
   ```
2. **`AgentState` 通道泛化**（`orchestrator/state.py`）：`failed_mutations: NotRequired[list[MutationOutcome]]` → `tool_failures: NotRequired[list[ClassifiedToolError]]`（覆写 reducer，每 agent step reset；docstring 更新 L-4→CM-1 由 L-4 泛化）。
3. **不改 `ToolResult`/`ToolSpec`/`Tool` 公共协议**——分类在 catch 点用 `exc`+`spec`。`_dispatch_tool`/`_invoke_tool` 的**内部**返回元组从 3 元扩到 4 元（追加 `ClassifiedToolError | None`，成功路径为 `None`），随之更新 `_run_call`/`_bounded`/`results` 标注与 `test_tool_audit` 的解构（机械）。
4. **无 `AuditAction`/`ResourceType` 变更**——advisory 是 in-context 引导，非审计事件（失败已由 `_tool_call_total{outcome}` + tool audit 记录）。

### 3.7 边界情况

| 场景 | 处理 |
|---|---|
| 一个 tool batch 多个失败 | 全部分类，`_build_recovery_advisory` 合并为单个 `<recovery-advisory>` 多行块 |
| 失败 + 成功混合 | 仅失败项进 advisory；成功项正常 ToolMessage |
| 分类不出明确类 | 落 `unknown`，给保守 advice（检视+勿原样重试），retryable=False |
| advisory 过长（多失败） | 每条 summary 截断（沿用 `_format_error` 的 500 cap 思路），整块 char gauge 观测 |
| `mutation_not_landed`（mutation 工具失败） | `_classify_tool_failure` **优先**跑 L-4 mutation 校验：已知 mutation 工具（save_artifact）无论 raise 还是成功态未落地，都归 `mutation_not_landed`（带 path，比泛型 error 更可操作）；非 mutation 工具才回落 catch 点的 `classified` |
| 非 plan_execute / 无 plan | 与 plan 无关，照常工作（CM-1 不依赖 plan） |

### 3.8 可观测（零债"可观测齐全"）

- `expert_work_cm_tool_error_total{error_class, tool}`（counter）—— 按类 × 工具计失败，看哪些工具/错误类高频。
- `expert_work_cm_recovery_advisory_chars`（gauge）—— 每次注入 advisory 字符数，防膨胀（与 N1 `expert_work_cm_recitation_chars` 同款）。
- 复用现有 `expert_work_tool_call_total{tool,outcome}`（raw 失败计数，不重复造）。
- 每次注入 emit log（tenant/user/thread + error_class 列表）；失败本身已有 tool audit，不新增 audit。

### 3.9 测试 & 验收（CM-1 Exit）

- **unit（≥85%）**：每种异常类型/文本 → 正确 `error_class`+`retryable`+advice；`transient` × (idempotent/read_only/irreversible) → retry 建议差异；`blocked` outcome → `blocked_by_policy` 不建议重试；`mutation_not_landed` 收敛正确（沿用 L-4 用例语义）；`render_recovery_advisory` 多失败合并/截断；空失败 → 无 advisory；注入后 reset。
- **integration（关键路径，真 graph）**：scripted LLM + 一个会 raise 的工具 → 下一 agent turn 尾部含 `<recovery-advisory>` 且含正确 class/advice；与 L-4 e2e 等价迁移（save_artifact 失败仍产出 advisory）。
- **零债 6 条**：无 TODO/FIXME；§3 与实现一致；metric+log 齐全（无新增 audit 因失败已记）；CI 8/8 + CodeQL 无新增 high；L-4 行为不回归。

### 3.10 Mini-ADR（CM-1 锁定）

| ID | 决策 |
|---|---|
| **CM-B1** | 分类**确定性、无 LLM**：纯由异常类型名 + 错误文本模式 + `ToolSpec` 能力推导（grounded，落实框架报告修订①"注入须源自真实执行信号"，不靠模型内省） |
| **CM-B2** | **泛化而非并行 L-4**：统一 `tool_failures` 通道取代 `failed_mutations`；`<recovery-advisory>` 块取代 `<mutation-advisory>`；mutation 校验作为 `mutation_not_landed` 一类收敛进来（成功路径语义失败专用，保留） |
| **CM-B3** | 分类在 **catch 点**做（`exc` 真类型在手，信号最丰富），不在 tools_node 事后从 ToolMessage 文本反推 |
| **CM-B4** | 注入沿用 L-4 不变量：尾部 `HumanMessage`（保 L-1 cache prefix）+ 单次注入即 reset；不改 `ToolResult`/`ToolSpec`/`Tool` 契约 |
| **CM-B5** | retry 建议**受 `ToolSpec.idempotent`/`side_effect` 约束**：仅幂等/只读才建议"安全重试"，不可逆非幂等→"重试前核实状态"，防诱导对副作用工具盲目重放 |
| **CM-B6** | fingerprint 升档（同工具同参连续失败→强制停/换路）**不在 CM-1**，归 CM-9 N4；CM-1 advice 仅以"勿原样重试"软提示，不引入 per-thread 指纹存储（保 surgical） |

### 3.11 PR 切分（CM-1）

1. **CM-1 PR1 — 分类器纯核心（CI 全测）**：`graph_builder/error_classifier.py`（`ToolErrorClass` taxonomy + `ClassifiedToolError` + `classify_tool_error` 纯函数 + `render_recovery_advisory` 纯函数 + retry 受 spec 约束逻辑）+ unit（每类分类 / retry×spec 矩阵 / advisory 渲染合并截断）。**不接图、不动 state、不碰 L-4**（pure-core-先行，对齐 CM-0 PR1 / SE-4a 节奏）。
2. **CM-1 PR2 — 接线 + L-4 收敛（端到端）**（已实现）：模块 `git mv` graph_builder/→tools/（破 import 环）；catch 点(`_invoke_tool`/`_dispatch_tool`)调 `classify_tool_error` → 4 元元组带出 `ClassifiedToolError`；`_classify_tool_failure` 优先 mutation 校验、回落 catch 分类 → 聚合进 `tool_failures`；`AgentState` `failed_mutations`→`tool_failures` 泛化；agent_node `_build_recovery_advisory`/`render_recovery_advisory` 取代 `_build_mutation_advisory`（`<recovery-advisory>`，path 保留）+ 注入/reset 不变；`expert_work_cm_tool_error_total{error_class,tool}` + `expert_work_cm_recovery_advisory_chars`；迁移 L-4 测试（test_mutation_advisory→test_recovery_advisory，含"失败只读工具现在也 advise"的 CM-1 增量用例）+ test_state/test_system_byte_stable/test_tool_audit 更新。977 orchestrator 回归绿。**→ CM-1 完成**。

> 每个 PR 在本 §3 基础上局部细化；ITERATION-PLAN 增 CM-1 backlog，ship 后回填 `[x]`+PR 号（[memory:iteration-plan-sync]）。

---

## 4. CM-2 详细设计 —— Working Memory 滑动窗口（compressor 前的廉价前置闸）

> **目标**：在 `agent_node` 入口、compressor preflight **之前**，加一道**按用户轮次裁剪、廉价、无 LLM**的滑动窗口闸。轻溢出场景直接裁到"最近 N 轮"即可压回阈值下，**省掉 compressor 的 LLM 摘要调用**；compressor 退为只处理裁剪后仍重溢出的第二道闸。映射框架报告 A2（OpenClaw `limitHistoryTurns`，生产 bug #1084 验证配对完整性是硬约束）。
>
> **核心策略**：**token-gate 的轮次裁剪**——仅当估算 prompt 超阈值才裁，未溢出**完全 no-op（零行为变更）**；裁剪只在 **HumanMessage 边界**切，天然保 ToolCall↔ToolResult 配对；**保首轮（初始任务）+ 最近 N 轮**，对抗"裁掉原始目标"。

### 4.1 关键约束（接缝核准结论，已源码核准）

| 约束 | 事实 | file:line |
|---|---|---|
| **compressor 是唯一现有压缩闸，且是 LLM 重武器** | `agent_node` 仅在所有注入后调 `context_compressor.should_compress` → `compress`（调摘要 LLM）；无任何"保留最近 N 轮"的廉价前置裁剪 | `graph_builder/builder.py:317-318`、`context/compressor.py:228-272` |
| **裁剪只作用当次 prompt，不改 checkpoint** | `agent_node` return 只 append `response`（`{"messages":[response]}`），从不重写 history；compressor 对 `messages` 的裁剪只影响本次 LLM 调用，checkpoint 里完整 history 保留 → **window 裁掉的中间轮在 checkpoint 无永久丢失**，下一 turn 从 checkpoint 重新加载再裁 | `builder.py:363-380`（return 只 append）、`:317-318`（compress 只改局部 `messages`） |
| **leading SystemMessage 必须冻结（L1 不变量）** | compressor `_split` 把 leading `SystemMessage` 排除在 head/tail 之外原样保留；window 必须沿用同一不变量，永不裁系统前缀 | `context/compressor.py:146-159` |
| **复用估算器，不另造** | `estimate_tokens(messages)=total_chars//4` 已是 compressor 的 preflight 估算；window 的 token-gate 复用同一函数，保口径一致 | `context/compressor.py:94-105` |
| **注入块在 window 之后加** | plan(J.1)/memory(J.3)/recovery-advisory(CM-1) 注入都在 `messages=list(state["messages"])` 之后；window 放在**注入之前**，只裁原始 history，绝不裁当前 turn 的引导块 | `builder.py:279`（取 history）、`:282-308`（注入） |
| **compressor 同款配置范式** | `ContextCompressionPolicy` 经 `PolicySpec.context_compression` → factory 构造 `ContextCompressor` 传 `build_react_graph`；window 镜像此范式（新 `WorkingMemoryPolicy` → `WorkingWindow` → 新参数） | `agent_spec.py:420-449`、`agent_factory.py:478-488`、`builder.py:201` |

> **结论**：CM-2 = 新增 `context/working_window.py`（与 compressor 同层），在 `agent_node` 取 history 后、注入前插一道 token-gate 轮次裁剪。因裁剪不改 checkpoint，**无永久信息损失**，与 CM-3（压缩前 flush）解耦——CM-3 是为 compressor 的有损摘要服务，window 本身不需要它兜底。

### 4.2 设计：token-gate → 轮次裁剪 → compressor 兜底（三段）

```
agent_node 入口： messages = list(state["messages"])
  │
  ├─ CM-2 working_window.apply(messages)        ← 新增第一道闸（本设计）
  │     ├─ should_trim? estimate_tokens >= threshold_tokens     （未溢出 → 原样返回，零行为变更）
  │     └─ 溢出 → trim_to_recent_turns：
  │           leading systems（冻结）
  │           + [首轮 first turn]（keep_first_turn，含初始任务目标）
  │           + 最近 max_recent_turns 个用户轮（HumanMessage 边界切）
  │           丢弃中间老轮（仅本次 prompt 视图；checkpoint 完整）
  │
  ├─ 注入 plan / memory / recovery-advisory（不受 window 影响）
  │
  └─ compressor.should_compress(messages)?      ← 退为第二道闸
        裁剪后仍 >= compressor 阈值（如最近 N 轮里有超大 tool result）→ LLM 摘要中段
        裁剪后 < 阈值 → should_compress=False，省掉 LLM 调用 ✓
```

两闸协同：window 与 compressor 默认同 `threshold_pct`（0.7）。window 先跑（注入前），多数轻溢出裁完即 < 阈值 → compressor 跳过。仅"最近 N 轮自身仍超窗"的重溢出才落到 compressor。

### 4.3 ToolCall↔ToolResult 配对保证（OpenClaw #1084 硬约束）

裁剪点**恒为 HumanMessage 索引**。论证：良构 ReAct history 中，`AIMessage(tool_calls)` 后紧跟对应 `ToolMessage`，一个 `HumanMessage` 永远是新一轮的开始、**绝不出现在 tool_use↔tool_result 之间**。因此：

- 保留"从某 HumanMessage 起的后缀"→ 后缀内所有 tool 对完整。
- 首轮片段 `remainder[首个HumanMessage : 第二个HumanMessage]` 以 HumanMessage 始、HumanMessage 前止 → 完整。

退化与去重：① 无 HumanMessage（纯系统/AI 序列，无法定义轮次）→ no-op 全留；② 用户轮总数 ≤ `max_recent_turns`(+首轮)→ no-op；③ 首轮片段与最近 N 轮窗重叠 → 不重复拼首轮，直接返回 `leading + remainder[窗起:]`。三种边界均有单测。

### 4.4 数据/协议变更

1. **新模块 `orchestrator/context/working_window.py`**（与 `compressor.py` 同层 `context/`，复用其 `estimate_tokens` + leading-system 分离逻辑，无环）：
   ```python
   @dataclass(frozen=True)
   class TrimResult:
       messages: list[BaseMessage]
       dropped_turns: int          # 裁掉的用户轮数（0 = no-op）

   def trim_to_recent_turns(
       messages: Sequence[BaseMessage], *, max_recent_turns: int, keep_first_turn: bool
   ) -> TrimResult: ...            # 纯函数、token-unaware、按 HumanMessage 边界切

   @dataclass(frozen=True)
   class WorkingWindow:
       context_window: int
       threshold_pct: float = 0.7
       max_recent_turns: int = 20
       keep_first_turn: bool = True
       @property
       def threshold_tokens(self) -> int: ...
       def should_trim(self, messages) -> bool: ...     # token-gate（复用 estimate_tokens）
       def apply(self, messages) -> TrimResult: ...      # gate 通过才 trim，否则原样
   ```
2. **新 `WorkingMemoryPolicy`**（`agent_spec.py`）挂 `PolicySpec.working_memory`（`extra="forbid"`，与 `context_compression` 并列）：`enabled: bool=True` / `threshold_pct: float=0.7(gt 0, le 1)` / `max_recent_turns: int=20(gt 0)` / `keep_first_turn: bool=True`。默认值保守：典型对话 ≤20 轮或未溢出 → no-op，**对现有 manifest 零行为变更**。
3. **factory 接线**（`agent_factory.py`）：`wm_policy = spec.spec.policies.working_memory`；`enabled` 则构造 `WorkingWindow(context_window=spec.spec.model.context_window, ...)` 传 `build_react_graph(working_window=...)`。
4. **builder 接线**（`graph_builder/builder.py`）：新参数 `working_window: WorkingWindow | None = None`；`agent_node` 在 `messages = list(state["messages"])` 后、plan 注入前：`if working_window is not None: result = working_window.apply(messages); messages = result.messages` + emit metric。
5. **不改 `AgentState` / `ToolResult` / `ToolSpec`**——window 是纯 prompt 视图裁剪，无状态通道、无 checkpoint 变更、无迁移。

### 4.5 边界情况

| 场景 | 处理 |
|---|---|
| 未溢出（estimate < 阈值） | `should_trim=False` → 原样返回，`dropped_turns=0`，零行为变更 |
| 无 HumanMessage（纯系统/AI 序列） | 无法定义轮次 → no-op 全留（安全退化） |
| 用户轮 ≤ max_recent_turns(+首轮) | no-op，全留 |
| 含 tool_calls 的轮被裁 | 切点恒在 HumanMessage 边界 → 保留部分无悬空 tool_use/tool_result（单测覆盖） |
| 首轮片段与最近 N 轮窗重叠 | 去重，直接 `leading + remainder[窗起:]`，不重复拼首轮 |
| disabled | factory 不构造 WorkingWindow，`build_react_graph(working_window=None)` → 与 CM-2 前完全一致 |
| window 裁后仍超 compressor 阈值 | compressor 第二道闸接手摘要中段（协同，非互斥） |

### 4.6 可观测（零债"可观测齐全"）

- `expert_work_cm_working_window_trim_total{outcome}`（counter）—— `outcome=trimmed/noop`，看 window 实际触发率（高 trimmed 率 ⇒ 省了多少 compressor LLM 调用）。
- `expert_work_cm_working_window_dropped_turns`（gauge）—— 最近一次裁掉的用户轮数，观测裁剪深度。
- 复用 compressor 既有 `estimate_tokens`，不新造估算 metric；compressor 自身的压缩日志不动 → 通过"trim 触发但 compressor 未触发"的差值即可读出 LLM 节省量。

### 4.7 测试 & 验收（CM-2 Exit）

- **unit（≥85%，纯核心）**：token-gate（未溢出 no-op / 溢出触发）；轮次裁剪保留首轮 + 最近 N 轮；HumanMessage 边界切**不破 tool_use↔tool_result 对**（含 tool_calls 轮被裁）；leading SystemMessage 冻结；无 HumanMessage / 轮数不足 / 首轮与窗重叠三种边界 no-op 或去重；`dropped_turns` 计数正确。
- **integration（真 graph）**：scripted LLM + 构造一段长 history（>N 轮且超阈值）→ 发给 LLM 的 prompt 只含 首轮+最近 N 轮 且 tool 对完整；checkpoint 仍含完整 history（下一 turn 可重新加载）；与 compressor 协同（裁后 < 阈值 → compressor 不触发）。
- **零债 6 条**：无 TODO/FIXME；§4 与实现一致；metric 齐全；CI 8/8 + CodeQL 无新增 high；现有 compressor / agent_node 行为不回归（disabled + 未溢出双路 no-op 验证）。

### 4.8 Mini-ADR（CM-2 锁定）

| ID | 决策 |
|---|---|
| **CM-C1** | **token-gate 轮次裁剪**：仅当 `estimate_tokens >= threshold` 才裁，未溢出完全 no-op（零行为变更）；不做无条件裁剪，避免改变未溢出对话的 prompt |
| **CM-C2** | 裁剪点**恒为 HumanMessage 边界**，天然保 ToolCall↔ToolResult 配对（OpenClaw #1084），不引入 tool-pair 修复逻辑（在正确的边界切就无需修复） |
| **CM-C3** | **保首轮 + 最近 N 轮**：呼应 compressor head/tail 哲学，对抗"裁掉初始任务目标"；首轮是廉价的目标锚（与 CM-0 recitation 互补，不依赖其开启） |
| **CM-C4** | **只裁当次 prompt 视图，不改 checkpoint**（沿用 compressor 同款语义）：被裁的中间轮无永久丢失，与 CM-3 压缩前 flush 解耦（CM-3 服务 compressor 的有损摘要，window 不需要它兜底） |
| **CM-C5** | window 与 compressor **协同非互斥**：window 第一道（注入前、廉价）、compressor 第二道（注入后、LLM），默认同阈值，多数轻溢出由 window 解决省 LLM，重溢出 compressor 兜底 |
| **CM-C6** | 复用 `compressor.estimate_tokens` + leading-system 分离逻辑（DRY，口径一致），不另造估算；window 不改 `AgentState`/`ToolResult`/`ToolSpec`，无迁移 |

### 4.9 PR 切分（CM-2）

1. **CM-2 PR1 — 滑窗纯核心（CI 全测）**（已实现）：`context/working_window.py`（`TrimResult` + `trim_to_recent_turns` 纯函数 + `WorkingWindow` dataclass + token-gate，复用 `compressor.estimate_tokens`）+ 13 unit（token-gate / 保首轮 / 配对完整 / leading 冻结 / 三种边界 / dropped_turns / 不可变）。**不接图、不动 protocol**（pure-core-先行，对齐 CM-0 PR1 / CM-1 PR1 节奏）。
2. **CM-2 PR2 — 接线（端到端）**（已实现）：`WorkingMemoryPolicy` → `PolicySpec.working_memory`（保守默认 ⇒ 现有 manifest 零行为变更）；factory 构造 `WorkingWindow` 传 `build_react_graph`（新 `working_window` 参）；`agent_node` 取 history 后、注入前插 `working_window.apply`（`outcome=trimmed/noop` 计数 + dropped_turns gauge）；`expert_work_cm_working_window_trim_total{outcome}` + `expert_work_cm_working_window_dropped_turns`；3 集成测（长 history 裁剪 + checkpoint 完整 + 未溢出 no-op + working_window=None 原路径）+ 4 protocol policy 测（默认/自定义/边界拒绝/extra-forbid）。991 orchestrator + 311 protocol 回归绿。**→ CM-2 完成**。

> 每个 PR 在本 §4 基础上局部细化；ITERATION-PLAN 增 CM-2 backlog，ship 后回填 `[x]`+PR 号（[memory:iteration-plan-sync]）。

---

## 5. CM-3 详细设计 —— 压缩前 flush（中段被摘要吞掉前抢救进长期记忆）

> **目标**：compressor 把对话中段 LLM 摘要后**丢弃**前，先把中段的耐久要点 flush 进长期记忆（`memory_item`），让长任务跨多次压缩不丢关键决策。映射框架报告 A3（deer-flow `summarization_hook` 压缩前 flush + Anthropic structured note-taking + OpenClaw pre-compaction memory flush，已是标准范式）。
>
> **核心策略**：**复用 `memory_writeback` 的抽取管道**——不新造记忆写入逻辑，而是把 writeback 节点内联的"render→LLM 抽取→parse→embed→write + blocked/DLQ 处理"抽成共享函数，run-末 writeback 与压缩前 flush 都调它；compressor 暴露一个 `on_pre_compaction(middle)` 回调，在 `_compress_once` 丢弃中段前 await 它。

### 5.1 关键约束（接缝核准结论，已源码核准）

| 约束 | 事实 | file:line |
|---|---|---|
| **中段被摘要后即丢弃，无 flush** | `_compress_once` 把 `split.middle` 交摘要 LLM → 换成单条 `<context-summary>` SystemMessage，中段原文不再进上下文、也不落盘 | `context/compressor.py:274-293`（`_compress_once`） |
| **summary 是 in-context 易逝、非耐久** | 摘要只在本次 prompt，后续轮可能被再压缩；不进 `memory_item`、不可跨 run 检索 → 中段关键决策无耐久副本 | `context/compressor.py:288-293` |
| **writeback 只在 run 末触发，且抽取的是压缩后的 state** | run 末 `memory_writeback_node` 从 `state["messages"]` 抽取——此时中段已是 summary（原文已丢），故 run-末抽取**抓不到**被压缩吞掉的中段细节 | `graph_builder/memory.py:240-319`；compress 只改局部 prompt 不改 checkpoint，但中段一旦被摘要替换，后续 state 即不含原文 |
| **writeback 抽取管道可复用** | `memory_writeback_node` 内联：`_render_trajectory` → `llm_caller` 抽取 → `parse_extracted_memories` → `embedder.embed` → `memory_store.write`，含 `MemoryInjectionBlockedError`/DLQ/best-effort 处理 | `graph_builder/memory.py:250-319`、`:58-125`（_render/parse 辅助） |
| **compress 在 agent_node 内调用，有 token+config** | `agent_node` 行 256 取 `token=cancellation_token(config)`，行 317-318 调 `compress`；tenant/user/thread 可经 `configurable_uuid(config, ...)` 解析（flush 回调内部解析，免 reorder） | `graph_builder/builder.py:256/317-318`、`graph_builder/_config.py:50`（`cancellation_token`）、`memory.py:244-248`（config→uuid） |
| **flush 与 writeback 同 gate（per-user + memory 启用）** | 两者都仅在 `memory.long_term` 启用 + `MemoryEnv` 有 store+embedder + 运行带 `user_id` 时有意义；factory `_build_memory_nodes` 已是此 gate | `agent_factory.py:1151-1189`；writeback no-op when no user_id（`memory.py:246-247`） |

> **结论**：CM-3 = 抽 `flush_messages_to_memory` 共享函数（行为保持地从 writeback 节点提取）+ compressor `compress(on_pre_compaction=...)` 回调；factory 在 memory 启用时构造一个 config-bound flush 回调传 `build_react_graph`，agent_node 调 compress 时绑定该回调。flush 抽取的是**即将丢弃的中段** `split.middle`（run-末 writeback 抓不到的内容）。

### 5.2 设计：共享抽取核心 + compressor 回调（两段）

```
context/compressor.py  ContextCompressor.compress(messages, on_pre_compaction=None)
  └─ _compress_once：split → 若有 on_pre_compaction：await on_pre_compaction(split.middle)   ← 丢弃前 flush
                     → 摘要中段 → 换成 <context-summary> → 返回（中段原文此刻才丢）

graph_builder/memory.py  flush_messages_to_memory(messages, *, store, embedder, llm_caller,
                                                   tenant_id, user_id, thread_id, token, dlq) -> int
  └─ render→LLM 抽取→parse→embed→write + blocked/DLQ/best-effort（从 writeback 节点抽出，二者共用）
  memory_writeback_node：解析 config → 调 flush_messages_to_memory → 返回 {}（变薄，行为不变）

agent_factory  _build_memory_nodes：memory 启用 + cc_policy.flush_before_compaction 时，
  构造 pre_compaction_flush 回调（绑 store/embedder/llm/dlq）→ build_react_graph(pre_compaction_flush=...)

graph_builder/builder.py  agent_node：调 compress 时若 pre_compaction_flush 非空，
  绑 async _flush(mid)=pre_compaction_flush(mid, config, token) 传 compress(on_pre_compaction=_flush)
  + 发 expert_work_cm_precompaction_flush_total{outcome} / expert_work_cm_precompaction_flush_memories
```

不变量：① flush 在中段**丢弃前**（即便后续摘要 LLM 失败抛 `ContextOverflowError`，要点已落盘）；② flush **best-effort**——`flush_messages_to_memory` 吞非 cancel 异常返 0，绝不阻断压缩或 run（沿用 writeback 语义）；③ 抽取**中段** `split.middle`（run-末 writeback 抓压缩后 state、抓不到原文，二者互补不重复）；④ 仅 memory 启用 + per-user 才动（gate 与 writeback 一致）。

### 5.3 与 writeback 的复用收敛（零债，不留并行抽取逻辑）

- `memory_writeback_node` 内联的 render→抽取→embed→write + `MemoryInjectionBlockedError`/DLQ/best-effort，**抽成模块级 `flush_messages_to_memory`**（参数化 messages/tenant/user/thread/token/store/embedder/llm/dlq，返回写入条数）。
- `memory_writeback_node` 变薄：解析 config→调共享函数→`return {}`，**行为完全保持**（现有 writeback 测试不改即过）。
- 压缩前 flush 复用同一函数，仅传入 `split.middle` 而非全 trajectory，且 tenant/user/thread 由 agent_node 的 config 解析。
- 共享函数内部 warning 日志措辞泛化（`memory.flush_blocked`/`memory.flush_failed`），summary 行（count）由各 caller 用自己 label 记（`memory.writeback`/`memory.precompaction_flush`），区分两路来源。

### 5.4 数据/协议变更

1. **`graph_builder/memory.py`**：新增模块级 `async def flush_messages_to_memory(...) -> int`（抽取核心）；`memory_writeback_node` 改调它。无新 import 之外的依赖。
2. **`context/compressor.py`**：`ContextCompressor.compress` + `_compress_once` 增可选 `on_pre_compaction: Callable[[Sequence[BaseMessage]], Awaitable[None]] | None = None`（默认 None ⇒ 与 CM-3 前完全一致）。`ContextCompressor` 本身**不持** store/embedder（保持 context 层纯净、不反向依赖 memory 层）——回调由上层注入。
3. **`build_react_graph`**：新参数 `pre_compaction_flush: PreCompactionFlush | None = None`（`PreCompactionFlush = Callable[[Sequence[BaseMessage], RunnableConfig, CancellationToken], Awaitable[int]]`）。agent_node 调 compress 时绑定 config+token。
4. **`ContextCompressionPolicy`**（`agent_spec.py`）增 `flush_before_compaction: bool = True`——memory 启用时默认开（A3 是 P1 默认能力）；memory 未启用则 factory 不构造回调，字段无副作用。
5. **factory `_build_memory_nodes`**：memory 启用 + `flush_before_compaction` 时构造 `pre_compaction_flush` 回调（绑 env.store/embedder/llm/dlq），随返回值传到 `build_react_graph`。
6. **不改 `AgentState`/`MemoryItem`/`MemoryStore` 契约**——flush 写的就是普通 `MemoryItem`（`source_thread_id` 标来源），与 writeback 同构。

### 5.5 边界情况

| 场景 | 处理 |
|---|---|
| memory 未启用 / 无 user_id | factory 不构造回调（或回调内 config 解析 user_id=None 即 no-op）→ 压缩照常，零 flush |
| 中段为空（head+tail 覆盖全部） | `_compress_once` 此前已抛 `ContextOverflowError`；flush 在 split 后、空中段不调回调（无内容可抽） |
| 摘要 LLM 失败 | flush 在摘要**前**已完成 → 要点已落盘；摘要失败照常抛 `ContextOverflowError` |
| flush 自身失败（抽取/embed/write 异常） | `flush_messages_to_memory` 吞非 cancel 异常返 0 + DLQ（有则入队）→ 压缩与 run 不受影响 |
| 抽取被 strict scanner 拒（`MemoryInjectionBlockedError`） | 共享函数捕获 → log + 返 0（与 writeback 一致，run 不受影响） |
| 多 pass 压缩 | 每个 `_compress_once` 丢弃各自中段前 flush（各 pass 中段不同，无重复）；上限 `max_passes` |
| 取消（RunCancelledError） | 共享函数 re-raise，沿 token 正常取消（不吞） |

### 5.6 可观测（零债"可观测齐全"）

- `expert_work_cm_precompaction_flush_total{outcome}`（counter）—— `outcome=flushed/empty/failed`，看压缩前 flush 触发与成败。
- `expert_work_cm_precompaction_flush_memories`（gauge）—— 最近一次压缩前 flush 写入的记忆条数。
- 共享函数内 warning（blocked/failed）+ 各 caller 的 count info 日志（`memory.precompaction_flush count=N` / `memory.writeback count=N`）区分来源。
- 复用现有 memory write 路径的 audit（`MemoryItem` 写入照常触发既有审计），不新增 audit。

### 5.7 测试 & 验收（CM-3 Exit）

- **unit（≥85%）**：`flush_messages_to_memory` 抽取核心（抽取→embed→write / 空抽取→0 / blocked→0 / 异常→DLQ 或 0 / 取消 re-raise）；`memory_writeback_node` 重构后行为保持（现有 writeback 测试不改即过）；compressor `on_pre_compaction` 在丢弃中段**前**被调、入参是 `split.middle`、回调失败不阻断压缩、None 回调与旧行为一致。
- **integration（真 graph）**：scripted LLM + 构造超阈值长 history → 触发 compaction → fake store 收到中段抽取的 `MemoryItem`；memory 未启用时零 flush（`pre_compaction_flush=None` 原路径）；`flush_before_compaction=False` 关闭。
- **零债 6 条**：无 TODO；§5 与实现一致；metric+log 齐全（无新增 audit）；CI 8/8 + CodeQL 无新增 high；writeback 行为不回归（共享重构后现有测试全过）。

### 5.8 Mini-ADR（CM-3 锁定）

| ID | 决策 |
|---|---|
| **CM-D1** | **复用 writeback 抽取管道**：抽 `flush_messages_to_memory` 共享函数，run-末 writeback 与压缩前 flush 共用；不新造第二套记忆写入逻辑（零债，writeback 行为保持） |
| **CM-D2** | flush 抽取**即将丢弃的中段** `split.middle`（run-末 writeback 抓压缩后 state、抓不到原文，二者互补不重复） |
| **CM-D3** | flush 在中段**丢弃前** + 摘要 LLM **前**（要点先落盘，摘要失败也不丢）；**best-effort**（吞非 cancel 异常，绝不阻断压缩/run） |
| **CM-D4** | compressor **不反向依赖 memory 层**：`ContextCompressor` 仅暴露 `on_pre_compaction` 回调，store/embedder 由上层（factory→agent_node）注入，保 context 层纯净 |
| **CM-D5** | flush gate 与 writeback 一致（memory 启用 + per-user）；`ContextCompressionPolicy.flush_before_compaction` 默认 True（memory 启用时即生效），memory 未启用则无副作用 |
| **CM-D6** | tenant/user/thread 由 flush 回调**内部从 config 解析**（agent_node 免在 compress 前 reorder tenant 解析）；写入即普通 `MemoryItem`（`source_thread_id` 标来源），不改 `MemoryItem`/`MemoryStore` 契约 |

### 5.9 PR 切分（CM-3）

1. **CM-3 PR1 — 共享抽取核心 + compressor 回调（CI 全测）**（已实现）：`graph_builder/memory.py` 抽 `flush_messages_to_memory`（行为保持地从 `memory_writeback_node` 提取，节点变薄；`log_label` 区分两路来源、writeback 日志串字节不变）；`context/compressor.py` `compress`/`_compress_once` 增 `on_pre_compaction` 回调（丢弃中段前且摘要前 await）+ 导出 `PreCompactionHook` + 7 unit（抽取核心各路径 / writeback 重构不回归 / compressor 回调时序+入参+None 等价）。**不接 factory、不动 policy**（pure-core 先行，对齐 CM-1/CM-2 PR1）。
2. **CM-3 PR2 — 接线（端到端）**（已实现）：`ContextCompressionPolicy.flush_before_compaction`（默认 True）；`memory.py` `make_pre_compaction_flush`（config-bound 回调，no-user no-op）；factory `_build_memory_nodes` 返 3 元、write_back ∧ flush_before_compaction 时构造回调传 `build_react_graph`；agent_node 调 compress 时绑定 config+token 的 `on_pre_compaction`（`outcome=flushed/empty` 计数 + memories gauge）；`expert_work_cm_precompaction_flush_total{outcome}` + `expert_work_cm_precompaction_flush_memories`；2 集成测（compaction 触发 flush 到 fake store + memory 未启用零 flush）+ 3 protocol policy 测（默认/关闭/extra-forbid）。1000 orchestrator + 78 protocol 回归绿。**→ CM-3 完成**。

> 每个 PR 在本 §5 基础上局部细化；ITERATION-PLAN 增 CM-3 backlog，ship 后回填 `[x]`+PR 号（[memory:iteration-plan-sync]）。

---

## 6. CM-4 详细设计 —— Reranker 接通长期记忆召回（"配置都摆好就差接线"）

> **目标**：把已有的 cross-encoder reranker 接进长期记忆召回（J.3）——Hybrid（向量+全文+RRF）召回**更宽的候选**后，用 reranker 重排到 top-k，再注入上下文。映射框架报告修订③ B5（cross-encoder rerank 是 RAG 末段公认最高 ROI 一环，+33~40% 准确率 / 仅 +120ms；多跳查询 ROI 最强）。优先级由 P2 **提到 P1**。
>
> **核心事实（已源码核准）**：reranker 基建**已完整存在且已接入知识检索（J.5）**——`Reranker` Protocol + `LLMReranker` + `ResolvingReranker`/`DynamicResolvingReranker` + `resolve_reranker`（control-plane），`KnowledgeRetriever` 已"宽召回→rerank→top-k"。**唯一缺口**：J.3 长期记忆召回 `memory_recall_node` 直接返回 RRF top-k、未接 rerank。CM-4 = **复用同一 reranker 抽象与实例**接进记忆召回，**不新造任何 reranker**。

### 6.1 关键约束（接缝核准结论，已源码核准）

| 约束 | 事实 | file:line |
|---|---|---|
| **reranker 抽象已存在** | `Reranker` Protocol（`rerank(query, documents, top_k, tenant_id) -> list[int]`）+ `LLMReranker`（LLM 排序、解析失败回落输入序、绝不破检索） | `tools/knowledge.py:54-119` |
| **知识检索已用 reranker（待镜像的范式）** | `KnowledgeRetriever`：宽召回 `recall_limit=20` → RRF fuse → `_rerank`（无 reranker 直接 fused[:limit]；有则 rerank 候选 content → 重排 top-k） | `tools/knowledge.py:122-187` |
| **control-plane 已构造一个 reranker** | `reranker = DynamicResolvingReranker(...)` 已建并传给知识 retriever；读 live 平台 rerank 配置、无凭证降级到 fused 序 | `control-plane/app.py:834-840`；`runtime.py:541-573`（`resolve_reranker`/`DynamicResolvingReranker`） |
| **记忆召回是唯一未接 rerank 的检索路径** | `memory_recall_node`：embed→`memory_store.retrieve(query_embedding, query_text, limit=top_k)`（内部 Hybrid+RRF）→ redact → 直接返回，**无 rerank** | `graph_builder/memory.py:187-219` |
| **reranker 经 Env 注入（embedder 同款范式）** | `MemoryEnv{store, embedder, dlq, tenant_config_store}` 由 control-plane 构造注入 factory；embedder 即 `DynamicResolvingEmbedder`——reranker 应循同一注入路径加 `MemoryEnv.reranker` | `agent_factory.py:163-187`（MemoryEnv）、`app.py:862-869`（构造） |
| **召回 best-effort、per-user gate** | recall 失败 log+继续（无记忆）；无 user_id no-op；rerank 接入须保持此契约（rerank 失败降级到 RRF 序，不丢全部记忆） | `graph_builder/memory.py:193-216` |
| **rerank 抽象在 tools 层（可被 graph_builder 下依赖）** | `Reranker` 在 `tools/knowledge.py`，control-plane runtime 已从此处导入 → `graph_builder/memory.py` 导入 `Reranker` 是向下依赖、无环 | `tools/knowledge.py` 仅依赖 `tools/registry`+`llm`，不依赖 graph_builder |

> **结论**：CM-4 = `make_memory_recall_node` 加可选 `reranker` 注入；召回**更宽候选**（`max(top_k, _MEMORY_RERANK_RECALL_LIMIT)`，仅当 reranker 存在）→ rerank 重排到 top_k → redact。`MemoryEnv.reranker` 字段 + factory 透传 + control-plane 把**已建的** `DynamicResolvingReranker` 一并传给 `MemoryEnv`（一行激活）。复用知识检索同一 reranker 抽象与实例，零新造。

### 6.2 设计：宽召回 → rerank → top-k（镜像知识检索）

```
memory_recall_node（reranker 注入，可选）
  embed(task) → memory_store.retrieve(limit = max(top_k, 20) if reranker else top_k)  ← 宽召回
  └─ reranker 存在：reranker.rerank(query=task, documents=[m.content], top_k, tenant_id)
       → 按返回 index 重排 → 取 top_k
       （rerank 内部已 best-effort：LLMReranker 解析失败回落输入序；
         ResolvingReranker 无凭证降级 fused 序；外再裹一层 → 失败回落 candidates[:top_k]）
  └─ reranker 为 None：candidates[:top_k]（= 现状，零行为变更）
  → redact 最终 top_k → recalled_memories
```

不变量：① reranker 为 None ⇒ **零行为变更**（仍 retrieve(top_k) 直接返回，不宽召回）；② rerank 在 **redact 前**（对原始 content 判相关性，redact 最终 top_k）；③ rerank **best-effort 降级到 RRF 序**（绝不因 rerank 失败丢全部记忆）；④ per-user gate / 召回 best-effort 契约不变；⑤ **复用知识检索同一 reranker 实例**（control-plane 一个 `DynamicResolvingReranker` 喂知识 + 记忆两路）。

### 6.3 数据/协议变更

1. **`graph_builder/memory.py`**：`make_memory_recall_node` 加可选 `reranker: Reranker | None = None`（从 `orchestrator.tools.knowledge` 导入 `Reranker`，向下依赖无环）；召回宽度 `recall_limit = max(top_k, _MEMORY_RERANK_RECALL_LIMIT)` 当 reranker 存在；新增 `_rerank_memories` 私有助手（镜像 `KnowledgeRetriever._rerank`，含 best-effort 回落 `candidates[:top_k]`）。`_MEMORY_RERANK_RECALL_LIMIT = 20`（对齐知识检索）。
2. **`MemoryEnv`**（`agent_factory.py`）加 `reranker: Reranker | None = None` 字段（embedder 同款注入语义）。
3. **factory `_build_memory_nodes`**：`make_memory_recall_node(..., reranker=env.reranker)` 透传。
4. **control-plane `app.py`**：`MemoryEnv(..., reranker=reranker)`——`reranker`（`DynamicResolvingReranker`）已在 `:834` 为知识检索构造，CM-4 仅多传一处（一行激活，无新建 reranker）。
5. **不改 `MemoryStore.retrieve` 契约**——宽召回只是传更大的 `limit`；不改 `Reranker` Protocol；不改 `MemoryItem`。
6. **可观测**：`uplift_metrics` 加 `record_memory_rerank(*, outcome)`（`reranked`/`skipped`/`degraded`），与现有 `record_memory_retrieval` 并列。

### 6.4 边界情况

| 场景 | 处理 |
|---|---|
| reranker 为 None（未配置/无凭证降级 None） | 不宽召回、retrieve(top_k) 直接返回 → 现状，零行为变更 |
| 候选数 ≤ top_k | rerank 仍可调（重排顺序），或直接返回——top_k 截断即全集，rerank 仅改序，调用无害 |
| rerank 抛异常 | 外层 best-effort 捕获 → 回落 `candidates[:top_k]`（RRF 序），记 `degraded`；绝不丢全部记忆 |
| reranker 内部无凭证（ResolvingReranker） | reranker 自身降级返回 fused 序 index → 等价不重排 |
| 召回为空 | rerank 不调，返回空（现状） |
| 取消（RunCancelledError） | rerank 在召回 try 内，cancel 照常 re-raise（不吞） |

### 6.5 测试 & 验收（CM-4 Exit）

- **unit（≥85%）**：`make_memory_recall_node` 注入 fake reranker（返回逆序 index）→ 召回结果按 rerank 序、截到 top_k；宽召回 limit 正确（reranker 存在取 `max(top_k,20)`、不存在取 top_k）；rerank 抛异常 → 回落 RRF 序不丢记忆；reranker=None → 与现状字节一致；rerank 在 redact 前。
- **integration（真 graph）**：scripted reranker + InMemoryMemoryStore 多条记忆 → recall 注入的 `recalled_memories` 顺序由 reranker 决定；control-plane MemoryEnv 携带 reranker（PR2 验证传入）。
- **零债 6 条**：无 TODO；§6 与实现一致；metric（`record_memory_rerank`）齐全；CI 8/8 + CodeQL 无新增 high；reranker=None 路径不回归（现有 memory recall 测试不改即过）。
- **效益数字（诚实标注）**：+33~40% 是厂商级数字；Expert Work 叠加增益（pgvector+RRF+rerank）业界无统一 benchmark，**自测留给 CM-N5（LongMemEval/LoCoMo）**，不在 CM-4 内声称具体百分比。

### 6.6 Mini-ADR（CM-4 锁定）

| ID | 决策 |
|---|---|
| **CM-E1** | **复用既有 `Reranker` 抽象（`tools/knowledge.py`），零新造**；control-plane 一个 `DynamicResolvingReranker` 实例同时喂知识检索（J.5）与记忆召回（J.3） |
| **CM-E2** | **宽召回→rerank→top-k**，镜像 `KnowledgeRetriever`；仅当 reranker 存在才宽召回（`max(top_k,20)`），否则 retrieve(top_k) 直返（零行为变更） |
| **CM-E3** | rerank **best-effort 降级到 RRF 序**：reranker 内部已回落（LLM 解析失败 / 无凭证），外再裹一层 → 失败回落 `candidates[:top_k]`，绝不因 rerank 丢全部记忆 |
| **CM-E4** | reranker 经 **`MemoryEnv.reranker` 注入**（embedder 同款），factory 不构造、只透传；`None` → 无 rerank（现状） |
| **CM-E5** | rerank 在 **redact 前**（对原始 content 判相关性，redact 最终 top_k）；per-user gate / 召回 best-effort 契约不变 |
| **CM-E6** | 效益自测留给 **CM-N5**（LongMemEval/LoCoMo）；CM-4 不在文档声称具体增益百分比（厂商数字不横比，[memory:N5 纪律]） |

### 6.7 PR 切分（CM-4）

1. **CM-4 PR1 — 记忆召回接 rerank（orchestrator，CI 全测）**（已实现）：`make_memory_recall_node` 加可选 `reranker` + 宽召回 `max(top_k,20)` + `_rerank_memories`（best-effort 回落 `candidates[:top_k]`）；`MemoryEnv.reranker` 字段；factory 透传 `env.reranker`；`record_memory_rerank{outcome=reranked/degraded}` + 5 unit（重排+截 top_k / 宽召回 limit / None 零变更 / 失败降级不丢记忆 / 空召回跳过）。1005 orchestrator + 302 common 回归绿。
2. **CM-4 PR2 — control-plane 激活（last mile）**（已实现）：`app.py` `MemoryEnv(..., reranker=reranker)`（复用 `:834` 已建的 `DynamicResolvingReranker`，喂知识检索与记忆召回两路）+ orchestrator factory 透传测（`_build_memory_nodes` 把 `MemoryEnv.reranker` 传到 recall 节点、spy reranker 被调）+ 文档标 done + ITERATION-PLAN 回填。**→ CM-4 完成**。

> 每个 PR 在本 §6 基础上局部细化；ITERATION-PLAN 增 CM-4 backlog，ship 后回填 `[x]`+PR 号（[memory:iteration-plan-sync]）。

---

## 7. CM-5 详细设计 —— 可恢复压缩（超大工具结果外部化 + 虚拟引用）

> **目标**：把工具结果截断从"永久丢弃"变为"可恢复"——完整输出落 workspace 文件，ToolMessage 保留截断内容 + 引用 footer，agent 需要时用既有读工具换回。映射框架报告 B6 + 修订 N3（Manus 法则"压缩必须可恢复——丢正文留引用"；Anthropic tool result clearing 是同方向的官方背书）。
>
> **框架报告假设修正（2026-06-10 用户拍板）**：报告原句"已有 artifact 表 + ObjectStore，天然适配"经源码取证不成立——artifact 表只是**元数据注册**（`ArtifactVersionRow.path_in_workspace` 指向 workspace 卷文件，内容基底本就是 workspace 文件），ObjectStore M0 未接 artifact 内容（`archived_object_key` 不填充），agent 也没有 read_artifact 工具。故 CM-5 存储基底 = **workspace 文件**（= artifact 的实际内容基底），**不入 artifact 表**（避免工具结果残片污染用户产物列表）、**不动 ObjectStore**。

### 7.1 关键约束（接缝核准结论，已源码核准）

| 约束 | 事实 | file:line |
|---|---|---|
| **截断在工具内部、策略各异（领域知识）** | bash/exec_python：`format_sandbox_outcome` head-trim 20k；http：body 20k tail + headers 4k 分离 cap；mcp：middle-trim 20k 留头尾；web_search：单条结果 4096；read_file：sandbox snippet 内 `text[:cap]`（全文不出 supervisor 线） | `tools/sandbox.py:38,314-322`、`tools/bash.py:138`、`tools/http.py:53-54,216-236`、`tools/mcp.py:63,642-660`、`tools/web_search.py:36,232`、`tools/file_ops.py:177` |
| **截断后完整结果即丢、LLM 不知情** | `ToolResult.meta["truncated"]` 在工具内生成，但 ToolMessage 只装 `content`+`tool_call_id`，meta 提取后丢弃；完整原文不落任何地方 | `graph_builder/builder.py:584,1232-1237`、`tools/registry.py:198-227` |
| **首批 4 工具的完整输出在 orchestrator 内存可得** | bash/exec_python：supervisor 返回**全量** `SandboxOutcome`，cap 在 orchestrator 侧 `format_sandbox_outcome`；http：`_format` 前全量 body 在内存；mcp：middle-trim 前全量在内存 | `tools/sandbox.py:314-322`、`tools/http.py:216`、`tools/mcp.py:642` |
| **CM-0 writer 范式可整体复用** | `WorkspaceFileWriter` Protocol（`write(*, rel, content)`）+ `SandboxWorkspaceWriter`（sandbox 原子写、自动建父目录）+ `workspace_writer_factory` 已从 factory 流到 builder（gate：persistent_workspace ∧ supervisor_client）+ best-effort 钩子先例 `_project_workspace_state` | `context/workspace_projection.py:130-134`、`tools/file_ops.py:759-774`、`agent_factory.py:120,595-621`、`builder.py:243,632,1126-1156` |
| **恢复工具已存在，不新造** | `read_file`（20k cap；**B-136 起**带 `offset` / `limit` 按行分页，没读到文件末尾时结果后会注明已给的行范围和下一个 `offset`——此前无 offset 且截断不告知模型；exec_python/bash 切片/grep 仍可用）；`list_dir` 可发现 `.tool_results/` | `tools/file_ops.py:460-513` |
| **ToolResult 可向后兼容扩展** | frozen dataclass + 默认值字段追加不破现有构造点 | `tools/registry.py:198-227` |

> **结论**：CM-5 = `ToolResult` 加可选 `full_content` 字段（4 个工具截断时带出全文）+ tools_node 中央 best-effort 外部化（复用 CM-0 `workspace_writer_factory`，零新注入参数）+ ToolMessage 引用 footer。截断策略不动、无 schema 变更、无新工具。

### 7.2 设计：工具截留 → 中央外部化 → 引用 footer（三段）

```
工具内（截断策略不动，一行增量）
  按既有领域策略截断 content（head/tail/middle/分离 cap 各异）
  截断发生时：ToolResult(content=截断版, full_content=完整渲染, meta={"truncated": True, ...})

tools_node（中央，best-effort，复用 CM-0 writer）
  _invoke_tool 返回后：
    full_content 存在 ∧ workspace_writer_factory 存在 ∧ spec 非 read_only
      → rel = ".tool_results/<run_id>/<call_id>-<tool>.txt"
      → writer.write(rel=rel, content=full_content[:_OVERFLOW_MAX_CHARS])   # sandbox 原子写
      → ToolMessage.content += "\n\n<tool-result-overflow>output truncated; full output
         (N chars) saved to .tool_results/…; read it in pages with read_file (offset /
         limit), or use exec_python / bash to inspect
         </tool-result-overflow>"
    写失败 / 无 writer / read_only → 维持现状（inline "...[truncated]" marker 仍在）
```

**职责分工（CM-F1，用户确认"不考虑成本也是最优"）**：截断**策略**是领域知识——web_search 按单条结果截、http 头/体分离 cap、skill_view 中段截留头尾、read_file 必须在 sandbox snippet 内截（否则超大文件全量过 supervisor HTTP 线）——统一 middleware 会抹掉这些语义，且 read_file 的 snippet 内截根本无法中央化。真正跨切面的只有**溢出后的处置**（外部化 + 引用），归中央一处。

不变量：① 未截断 ⇒ `full_content=None` ⇒ 零行为变更；② 无 writer（无持久 workspace / 无 supervisor）⇒ 现状截断，与今日字节一致；③ 外部化 best-effort——写失败不影响 run、不吞 cancel；④ footer 仅在**写成功**后追加（绝不引用不存在的文件）；⑤ read 类豁免双保险（见 7.4）。

### 7.3 数据/协议变更

1. **`tools/registry.py`**：`ToolResult` 加 `full_content: str | None = None`。契约：仅当 `content` 被截断**且**工具持有完整原文时填（与 `content` 同构的未截断渲染）；read_only 工具**禁止**填。无 protocol 包变更（ToolResult 是 orchestrator 内部类型）。
2. **首批 4 工具**（各一处增量）：`format_sandbox_outcome`（bash/exec_python 共用）截断时带出全量渲染；`HTTPTool._format` 截断时带出全量渲染（headers+body 未截版）；`MCPTool` middle-trim 时带出全量。
3. **新模块 `tools/overflow.py`**（纯函数，与 `tools/error_classifier.py` 同层级先例）：`overflow_rel_path(run_id, call_id, tool_name) -> str`、`render_overflow_footer(rel, total_chars) -> str`、`_OVERFLOW_MAX_CHARS = 2_000_000` 上限保护（超限只写前 2M + 文件尾注记）。
4. **`graph_builder/builder.py`**：tools_node 在 ToolMessage 构造处接外部化钩子 `_externalize_tool_overflow`（async、best-effort）；**复用既有 `workspace_writer_factory` 参数**——零新 build_react_graph 参数、gate 与 CM-0 投影完全一致。
5. **可观测**：`expert_work_cm_tool_overflow_total{outcome=externalized/degraded, tool}` counter + `expert_work_cm_tool_overflow_chars` gauge（builder 内定义，CM-2/CM-3 同款）+ 结构化日志 `tool.overflow {tool, rel, chars}`。不发 audit（agent 自身 workspace 的运行时产物，非平台代写状态；metrics+log 足够，区别于 CM-0 投影的 STATE_PROJECTED）。
6. **不加 policy 开关**：gate 行为 = workspace_writer_factory 存在与否（CM-0 投影同款先例）；无 manifest/policy 变更。

### 7.4 Read 类豁免（防 persist→read→persist 循环，设计铁律）

| 层 | 保险 |
|---|---|
| 工具契约 | read_only 工具（read_file/list_dir/skill_view/web_search/knowledge_search/list_artifacts…）**不填** `full_content`：web_search 单条自带 URL（Manus 式"丢正文留 URL"已天然满足）；read_file/skill_view 源文件可重读（且 read_file 全文根本不出 sandbox） |
| 中央双保险 | tools_node 外部化前检查 `spec.resolved_side_effect == "read_only"` ⇒ 跳过（防未来工具误填；测试锁死） |

循环场景推演：agent `read_file` 读回 `.tool_results/` 大文件 → read_file 是 read_only → 即使再截断也不外部化 → 无循环。

### 7.5 边界情况

| 场景 | 处理 |
|---|---|
| 未截断（绝大多数调用） | `full_content=None`，中央钩子零开销跳过 |
| 无持久 workspace / 无 supervisor（writer factory=None） | 现状截断，字节一致 |
| 写失败（sandbox 不可达/IO 错） | log + `outcome=degraded`，content 保持截断版（inline marker 仍在），run 不受影响 |
| 超大全文（如 500MB stdout） | `_OVERFLOW_MAX_CHARS=2M` 截顶写入 + 文件尾注记（防巨量 payload 过 supervisor HTTP 线） |
| 取消（RunCancelledError/CancelledError） | 外部化不吞 cancel，照常 re-raise |
| 并行工具调用 | 路径含 `call_id` 唯一，无写冲突 |
| `.tool_results/` 累积 | 生命周期复用 workspace 既有留存机制（每日备份 / 90 天 archive，J.15）；agent/用户可自删；**不新造清理机制**（明确 non-goal，非遗留债） |
| 工具本身 error（ToolMessage status=error） | 错误路径不外部化（错误摘要已有 500 cap + CM-1 advisory 负责） |

### 7.6 可观测（零债"可观测齐全"）

- `expert_work_cm_tool_overflow_total{outcome,tool}`：externalized / degraded 两值。
- `expert_work_cm_tool_overflow_chars`：最近一次外部化的全文字符数 gauge。
- 结构化日志 `tool.overflow`：{tool, rel, chars}；写失败 `tool.overflow_failed` WARNING。

### 7.7 测试 & 验收（CM-5 Exit）

- **unit（≥85%）**：4 工具截断时 `full_content` = 未截断渲染、未截断时 None；read_only 工具不填（锁契约）；`overflow_rel_path`/`render_overflow_footer` 纯函数；`_OVERFLOW_MAX_CHARS` 截顶；中央钩子——写成功加 footer、写失败 degraded 不加、read_only 跳过、cancel re-raise。
- **integration（真 graph + fake writer）**：超限 bash 输出 → ToolMessage 带 `<tool-result-overflow>` footer + fake writer 收到完整全文；无 writer → 与现状字节一致；checkpoint 里 ToolMessage 含 footer（被截内容可在后续 turn 经引用恢复）。
- **零债 6 条**：无 TODO；本 §7 与实现一致；metrics/log 齐全；CI 8/8；`full_content=None` 路径零行为变更（现有工具测试不改即过）。

### 7.8 Mini-ADR（CM-5 锁定）

| ID | 决策 |
|---|---|
| **CM-F1** | **截断策略留工具内（领域知识），中央只管溢出处置**——非成本折中而是职责正确：per-result cap / 头体分离 / middle-trim / sandbox 内截各有语义，middleware 统一化会抹掉且部分（read_file）无法中央化（2026-06-10 用户确认） |
| **CM-F2** | **存储基底 = workspace 文件** `.tool_results/<run_id>/<call_id>-<tool>.txt`；不入 artifact 表（防污染用户产物）、不接 ObjectStore（修正框架报告假设：artifact 内容基底本就是 workspace 文件） |
| **CM-F3** | **首批 bash/exec_python/http/mcp**（截断即永久丢失的 4 类）；web_search（URL 即引用）、read_file/skill_view/list_dir（源可重读）豁免；read_only 双保险防 persist→read→persist 循环 |
| **CM-F4** | **复用 CM-0 `workspace_writer_factory` + gate**（persistent_workspace ∧ supervisor_client），零新注入参数；无 sandbox 降级现状截断 |
| **CM-F5** | best-effort 铁律：写失败不影响 run、cancel re-raise、`_OVERFLOW_MAX_CHARS=2M` 上限防巨量 payload 过线；footer 仅写成功后追加 |
| **CM-F6** | **恢复路径 = 既有 read_file/exec_python/bash**（不新造 read 工具、不加 policy 开关、不发 audit）；`.tool_results/` 生命周期归 workspace 既有留存机制 |

### 7.9 PR 切分（CM-5）

1. **CM-5 PR1 — 纯核心（不接图）**（已实现，PR #508）：`ToolResult.full_content` 字段 + 4 工具截断时带出全文（bash/exec_python 经 `format_sandbox_outcome`、http、mcp）+ `tools/overflow.py` 纯函数（rel path / footer / 2M 上限）+ unit tests。
2. **CM-5 PR2 — 接线（收尾 CM-5）**（已实现）：`_invoke_tool` 成功路径接 `_externalize_tool_overflow` best-effort 钩子（复用 `workspace_writer_factory`，与 stage 内其他工具并行）+ metrics/日志 + 集成测（真 graph + fake writer / 无 writer 字节一致 / 写失败降级 / read_only 双保险）+ ITERATION-PLAN 回填。**→ CM-5 完成**。

> 每个 PR 在本 §7 基础上局部细化；ITERATION-PLAN 增 CM-5 backlog，ship 后回填 `[x]`+PR 号。

---

## 8. CM-6 详细设计 —— 检索质量：时间衰减 + MMR 去冗余

> **目标**：长期记忆召回从"纯相关性对称 RRF"升级为"新近度加权 + 结果多样性"——时间衰减让久未使用的记忆让位于新近记忆（OpenClaw temporalDecay 半衰期 30 天范式），MMR 去掉高度同质的候选（λ=0.7）。映射框架报告 B4（组件级有据：MMR/衰减各自经典；**叠加增益业界无统一 benchmark，自测留 CM-N5**，本设计不声称数字）。
>
> **管线顺序修正（2026-06-10 用户拍板，偏离范围表原句）**：范围表原句"`memory/sql.py:retrieve()` RRF 后加 MMR + 时间衰减"写于 CM-4 之前；CM-4 已把 cross-encoder rerank 放在 retrieve **之后**（orchestrator 侧）——若 MMR 在 retrieve 内做，rerank 按纯相关性重排会打散 MMR 的多样性选择，白做。修正为：**时间衰减进 retrieve()**（所有召回部署受益，含无 reranker），**MMR 放 orchestrator 召回管线末段**（rerank 之后）。最终管线：宽召回（RRF+衰减）→ rerank（相关性）→ MMR（去冗余）→ top_k → redact，每段职责单一。

### 8.1 关键约束（接缝核准结论，已源码核准）

| 约束 | 事实 | file:line |
|---|---|---|
| **RRF 融合在 Python 侧、两 store 镜像实现** | sql：两条独立 SELECT（HNSW 向量 + GIN 全文各取 `_HYBRID_RECALL_LIMIT=20`）→ `rrf_fuse` 融合 row id → `[:limit]`；in-memory 同构（jieba 关键词 + cosine） | `persistence/memory/sql.py:27,180-218`、`memory/memory.py:20,98-134` |
| **`rrf_fuse` 只返回排序项、不带分数** | `rrf_fuse(rankings, k=60) -> list[T]`——衰减加权需要分数 ⇒ 需并列新增带分版本，**不破现有调用方**（knowledge J.5 也用它） | `expert-work-common/search/rrf.py:29-55` |
| **两 store 各有 hybrid + 纯向量两条路径** | `query_text` 为 None/空 ⇒ 纯向量路径（cosine 距离序），无 RRF——衰减须两路都覆盖才一致 | `sql.py:115-186`、`memory.py:122-135` |
| **候选 embedding 随行返回（MMR 无阻碍）** | `_row_to_item` 转 `embedding=tuple(float,...)`；`MemoryItem.embedding` 必有 | `sql.py:30-52`、`protocol/memory_item.py:28-96` |
| **时间戳字段齐全** | `created_at` / `last_used_at`（初值=created_at，retrieve 访问刷新）/ `last_reviewed_at`；衰减用 `last_used_at`（被用即保鲜） | `models/memory_item.py:48-57`、migration 0017:53-57 |
| **CM-4 管线已定召回结构** | recall node：embed → `retrieve(limit=宽召回)` → `_rerank_memories`（best-effort）→ top_k → redact；`_MEMORY_RERANK_RECALL_LIMIT=20`；query embedding（`vectors[0]`）在手——MMR 可直接复用 | `graph_builder/memory.py:59,216-286` |
| **retrieve() 唯一在线调用方是 recall 路径** | consolidator 走 `list_purge_candidates` 非 retrieve；knowledge J.5 是独立 retriever——衰减只影响记忆召回，无旁路风险 | `sql.py:481-506` |
| **协议签名 sweep 范围** | retrieve 签名**不变**（衰减是内部重排）⇒ tools/eval doubles 无需同步改（[memory:protocol-sweep]） | `persistence/memory/base.py:53-72` |

> **结论**：CM-6 = expert-work-common 新增带分 RRF + 衰减 + MMR 纯函数；两 store retrieve() 内部接衰减（hybrid+纯向量两路）；orchestrator recall 节点 rerank 后接 MMR。签名零变更、协议零变更、schema 零变更。

### 8.2 设计：衰减进 retrieve，MMR 殿后（管线四段）

```
retrieve()（两 store 镜像）
  hybrid： vector top20 + keyword top20 → rrf_fuse_scored → score *= decay_factor(now - last_used_at)
  纯向量： similarity = 1 - cosine_distance/2 → score *= decay_factor(...)
  → 按衰减后分数排序 → [:limit]

decay_factor(age) = 0.5 + 0.5 * 2^(-age_days / 30)        # ∈ (0.5, 1]
  半衰期 30 天（OpenClaw 范式）；带 0.5 floor——老记忆最多减半、
  绝不被埋死（canonical 事实如用户偏好可能数月前写入，纯指数衰减
  180 天即衰到 1.6%，会制造"平台失忆"）

memory_recall_node（orchestrator，CM-4 结构上加一段）
  retrieve(limit=宽召回 20) → rerank（可选；改为全序输出 top_k=len(候选)，
    不再截断——否则 MMR 只能在已截集合内重排，无法从宽池换入多样候选，
    去冗形同虚设；最终截断职责移交 MMR 段，PR2 局部细化）
  → mmr_select(query_embedding, candidates, k=top_k, λ=0.7)   ← 新增，殿后
  → redact → recalled_memories
```

不变量：① retrieve 签名不变、返回仍是排序后 `list[MemoryItem]`；② 衰减只重排**候选窗口内**的相对顺序（宽召回 20 内），不改召回集合本身；③ MMR best-effort——异常回落输入序 `[:top_k]`（与 rerank 降级同款契约，绝不丢全部记忆）；④ MMR 在 redact 前（用原始 embedding）；⑤ `last_used_at` 刷新语义不动（自增强回路被"衰减只作用于排序、相关性仍是主因子"抑制，且 floor 限幅）。

### 8.3 数据/协议变更

1. **`expert-work-common/search/rrf.py`**：并列新增 `rrf_fuse_scored(rankings, k=60) -> list[tuple[T, float]]`（`rrf_fuse` 改为其薄包装，行为字节不变；knowledge J.5 调用方零感知）。
2. **`expert-work-common/search/decay.py`（新）**：`temporal_decay_factor(*, age: timedelta, half_life: timedelta = 30d, floor: float = 0.5) -> float`。
3. **`expert-work-common/search/mmr.py`（新）**：`mmr_select(*, query_embedding, candidates: Sequence[tuple[T, Sequence[float]]], k, lambda_=0.7) -> list[T]`——贪心 MMR：`λ*sim(q,c) - (1-λ)*max sim(c, selected)`，cosine；候选 embedding 维度不一致/为空时跳过该候选。
4. **`persistence/memory/sql.py` + `memory/memory.py`**：retrieve() 两路接衰减（`datetime.now(UTC)` 取齐一次；`last_used_at` 为 naive 的兼容按现有行为处理）；`_HYBRID_RECALL_LIMIT` 不变。
5. **`graph_builder/memory.py`**：`_mmr_memories` 私有助手（best-effort，镜像 `_rerank_memories` 契约）；插在 rerank 后、redact 前；**宽召回条件从 `reranker is not None` 扩为恒宽**（MMR 默认开 ⇒ `recall_limit = max(top_k, 20)` 恒成立——SQL 本就各取 20 候选融合，增量成本为零，只是融合后截断更晚）。
6. **可观测**：`record_memory_mmr(*, outcome)`（`applied`/`degraded`，`expert_work_cm_memory_mmr_total`，uplift_metrics 与 `record_memory_rerank` 并列）；persistence 衰减为确定性纯变换，不加 metric（debug 日志足够）。
7. **无 policy / tenant_config / schema / 协议变更**：λ、半衰期、floor 为模块常数（同 rerank 先例——质量增强不开 knob，调参等 CM-N5 评测基线就位后再议）。

### 8.4 边界情况

| 场景 | 处理 |
|---|---|
| 纯向量路径（query_text 空 / 全文无命中场景） | similarity = `1 - cosine_distance/2` 后同样衰减——两路语义一致 |
| `last_used_at` 缺失/异常（理论不可能，NOT NULL） | `max(age, 0)`——未来时间戳按 0 龄处理（时钟偏移防御） |
| 候选 embedding 全相同（极端同质） | MMR 第二项恒高 → 自动只选 top1 后按 λ 权衡，仍返回 k 个（贪心不死锁） |
| 候选数 ≤ top_k | MMR 仍跑（只改序不减量）；零候选直接返回 |
| MMR 计算异常（维度不齐等） | best-effort 回落输入序 `[:top_k]`，记 `degraded`，绝不丢记忆 |
| reranker 与 MMR 同时存在 | 顺序 rerank→MMR：先按语义相关性重排，再在高相关集合内去冗余（设计本意） |
| 老部署回归担忧 | 衰减 floor 0.5 + 只作用候选窗口内排序 ⇒ 高相关老记忆仍可召回，只在同分竞争时让位新记忆 |

### 8.5 可观测（零债"可观测齐全"）

- `expert_work_cm_memory_mmr_total{outcome=applied/degraded}`（uplift_metrics）。
- persistence retrieve debug 日志保持现状（衰减为确定性变换，不另设 metric）。

### 8.6 测试 & 验收（CM-6 Exit）

- **expert-work-common unit**：`rrf_fuse_scored` 分数正确 + `rrf_fuse` 包装后行为字节不变（现有 rrf 测试不改即过）；`temporal_decay_factor`（0 龄=1 / 30 天=0.75 / ∞→floor 0.5 / 负龄钳 0）；`mmr_select`（同质候选被去冗、λ=1 退化为纯相关序、空候选/维度不齐跳过、k≥候选数全返）。
- **persistence unit（in-memory）+ integration（SQL）**：同相关性老/新两条记忆 → 新者排前；高相关老记忆仍胜低相关新记忆（floor 保护）；纯向量路径同样衰减；retrieve 签名/返回类型不变（现有 store 测试不改即过）。
- **orchestrator unit**：recall 节点冗余候选（近重复 embedding）被 MMR 去冗；MMR 异常 → 回落输入序不丢记忆；rerank→MMR 顺序（scripted reranker + spy）；恒宽召回 limit=20。
- **零债 6 条**：无 TODO；本 §8 与实现一致；metric 齐全；CI 8/8；叠加增益自测留 CM-N5（诚实标注，不声称数字）。

### 8.7 Mini-ADR（CM-6 锁定）

| ID | 决策 |
|---|---|
| **CM-G1** | **管线顺序：宽召回（RRF+衰减）→ rerank → MMR → top_k → redact**；MMR 必须在 rerank 后（否则被纯相关性重排打散，白做）——修正范围表"都在 retrieve()"原句（2026-06-10 用户拍板） |
| **CM-G2** | **时间衰减进 retrieve()**（两 store 镜像、hybrid+纯向量两路一致），所有召回部署受益（含无 reranker）；基于 `last_used_at`（被用即保鲜，频率×新近度复合信号） |
| **CM-G3** | **衰减带 floor**：`0.5 + 0.5 * 2^(-age_days/30)`——半衰期 30 天（OpenClaw 范式），老记忆最多减半绝不埋死（canonical 事实保护，防"平台失忆"） |
| **CM-G4** | **MMR λ=0.7（OpenClaw 范式）、cosine、greedy**；纯函数进 expert-work-common search 包与 rrf 并列；无 policy knob（同 rerank 先例，调参等 CM-N5 基线） |
| **CM-G5** | **召回恒宽 `max(top_k, 20)`**（MMR 默认开）；SQL 本就各取 20 候选融合，增量成本零；`rrf_fuse_scored` 并列新增、`rrf_fuse` 薄包装零破坏（knowledge J.5 零感知） |
| **CM-G6** | MMR **best-effort 降级输入序**（与 rerank 同契约）；衰减为确定性纯变换不设 metric；**叠加增益不声称数字，自测留 CM-N5** |

### 8.8 PR 切分（CM-6）

1. **CM-6 PR1 — 纯核心（expert-work-common，不接任何调用方）**（已实现，PR #511）：`rrf_fuse_scored`（`rrf_fuse` 薄包装化）+ `search/decay.py` + `search/mmr.py` + unit tests。
2. **CM-6 PR2 — 接线（收尾 CM-6）**（已实现）：两 store retrieve() 接衰减（hybrid+纯向量）+ orchestrator recall 节点 `_mmr_memories`（rerank 后、redact 前；rerank 改全序输出由 MMR 殿后截断，见 §8.2）+ 恒宽召回 + `record_memory_mmr` + persistence/orchestrator tests + ITERATION-PLAN 回填。**→ CM-6 完成**。

> 每个 PR 在本 §8 基础上局部细化；ITERATION-PLAN 增 CM-6 backlog，ship 后回填 `[x]`+PR 号。

---

## 9. CM-7 详细设计 —— 结构化摘要 + 记忆写入显式操作（A-MEM/Mem0 范式）

> **目标**：两个独立面（2026-06-10 用户拍板"①+② 全做，② 只在 run 末 writeback"）：
> ① **compressor 摘要强化**——`<context-summary>` 加"背景非指令"强语义（Hermes SUMMARY_PREFIX 范式，防模型把历史摘要当新指令重复执行）+ 结构化 sections + 二次压缩从"摘要的摘要"改为**增量更新前次摘要**（Hermes `context_compressor.py:659-660` 范式）；
> ② **记忆写入显式操作**——run 末 writeback 抽取后先检索相似既有记忆，LLM 判 `ADD/UPDATE/DELETE/NOOP` 再落库（Mem0 extract→update，2504.19413：结构化 note + 显式操作省 85~93% token、矛盾可废弃）。**这是真能力缺口非重复机制**：consolidator 只会合并簇新建 consolidated + 清噪声（`memory_consolidator.py:584-691`），没有"更新/废弃被新事实矛盾的旧记忆"能力——"喜欢浅烘"后来变"喜欢深烘"，今天两条都活着。

### 9.1 关键约束（接缝核准结论，已源码核准）

| 约束 | 事实 | file:line |
|---|---|---|
| **摘要无"背景非指令"语义** | `<context-summary>` 只是裸标签包 bullets；summariser prompt 要求 3-7 bullets 自由文本，无任何 reference-only 声明 | `context/compressor.py:66-75,317-320` |
| **二次压缩 = 链式摘要的摘要** | 前次 summary（SystemMessage，在 head 后 tail 前）非 leading 位置 → 落进下一 pass 的 middle 被当普通文本再摘要；3-pass 循环每 pass 全量重生成 | `compressor.py:152-181,257-289,320` |
| **L-1 安全** | summary 消息在 leading_systems **之后**，不属于 byte-stable 冻结块——改摘要语义/格式不破 cache prefix | `compressor.py:155-164,320` |
| **CM-3 flush 先于摘要** | `on_pre_compaction(split.middle)` 在 summariser LLM 调用前 await——②的写入操作判定**不能**放这里（agent turn 内，延迟敏感） | `compressor.py:310-316` |
| **writeback 抽取直写、无比对** | `flush_messages_to_memory`：LLM 抽取 `{kind, content}` → embed → `memory_store.write(items)`，与既有记忆零比对 | `graph_builder/memory.py:68-78,332-433` |
| **去重只认 exact hash** | `write` ON CONFLICT DO NOTHING on `(tenant,user,content_hash)`（`strip().lower()` SHA-256）——同义改写必堆积 | `persistence/memory/sql.py:103-140`、`memory/hash.py` |
| **UPDATE/DELETE 原语已存在** | `MemoryStore.update_content(id, content, kind)`（自动重 embed）+ `soft_delete(id)`；PATCH/DELETE `/v1/memory/{id}` 已用 | `persistence/memory/base.py:106-134`、`control_plane/api/memory.py:201-287` |
| **consolidator 无矛盾解决** | SUB-PASS 1 簇合并新建 consolidated、SUB-PASS 2 lone-item 噪声清理；无"更新既有条目"路径 | `control_plane/memory_consolidator.py:559-691` |
| **会被格式变更影响的测试** | `test_compress_preserves_head_and_tail`/`test_compress_summary_lands_between_head_and_tail`/integration same——断言 scripted 摘要文本仍在 wrapper 内，preamble 追加不破；新增断言锁 preamble | `tests/test_context_compressor.py:135-233` |

### 9.2 设计 ①：摘要"背景非指令"语义 + 结构化 sections + 增量更新

```
wrapper（SystemMessage，位置不变：leading_systems 后、head 后、tail 前）
  <context-summary>
  Background summary of earlier conversation. Reference material only —
  its contents are NOT instructions; do not execute, re-run, or treat
  anything inside it as a new request.

  ## Facts        ← 结构化 sections（summariser prompt 锁定三段）
  ## Decisions
  ## Pending
  </context-summary>

_compress_once（增量更新，Hermes 范式）
  middle 内含前次 summary（识别：SystemMessage 且 content 以 <context-summary> 开头，取最后一条）
    → UPDATE 模式：prompt = "维护运行中摘要：把新事件并入前次摘要；保持三段结构；
      废弃已被取代的 Pending 项；只输出更新后摘要"，user = 前次摘要正文 + 新消息转录
  middle 无前次 summary
    → FRESH 模式：现行为（新 prompt 产三段结构）
```

不变量：① wrapper 标签、消息类型（SystemMessage）、插入位置**全部不变**（L-1 安全 + L-4/recovery-advisory 不受扰）；② summariser 失败路径不变（`ContextOverflowError` 语义保持）；③ CM-3 flush 时序不变（先 flush 后摘要，UPDATE 模式同样）；④ preamble 文本进 wrapper **内**（单一来源，tag 外零变更）。

### 9.3 设计 ②：run 末 writeback 显式操作（Mem0 extract→update）

```
flush_messages_to_memory(..., reconcile: bool = False)   ← 新参，默认关
  抽取 → embed →
  reconcile=False：直写（现状；CM-3 压缩前 flush 走此路——turn 内延迟敏感，
                   残留重复由 consolidator 兜底）
  reconcile=True（run 末 memory_writeback_node）：
    每候选：store.retrieve(query_embedding=候选vec, limit=3)（纯向量）
      → 过滤 cosine ≥ _RECONCILE_SIM_THRESHOLD(0.80) 的近邻
    无近邻候选 → 直接 ADD（零 LLM 成本，多数路径）
    有近邻 → 一次批量 LLM 调用（复用 writeback llm_caller）：
      输入：[{候选 content/kind, 近邻 [{id, content}]}]
      输出：[{index, op: ADD|UPDATE|DELETE|NOOP, target_id?}]
        ADD            候选写入（新信息）
        UPDATE target  update_content(target, content=候选, kind=候选)（同义/矛盾改写，自动重 embed）
        DELETE target  soft_delete(target) 且候选不写（旧事实被撤销，候选只是撤销事件）
        NOOP           候选跳过（与既有重复）
    解析失败 / LLM 异常 / 单 op 应用失败 → 该候选回落直写 ADD（best-effort，绝不丢记忆）
```

### 9.4 数据/协议变更

1. **`context/compressor.py`**：`_SUMMARY_PREAMBLE` 常量（背景非指令声明，wrapper 内）；summariser system prompt 改三段结构；`_compress_once` 识别 middle 中最后一条前次 summary → UPDATE 模式 prompt；无则 FRESH 模式。无签名变更。
2. **`graph_builder/memory.py`**：`flush_messages_to_memory` 加 `reconcile: bool = False`；新私有 `_reconcile_candidates`（近邻检索 + 批量 ops LLM + 应用，best-effort 全程）；`_RECONCILE_SIM_THRESHOLD = 0.80`；`memory_writeback_node` 按 policy 传 `reconcile=True`；`make_pre_compaction_flush` 保持 False。
3. **`agent_spec.py`**：`MemoryPolicy.reconcile_writes: bool = True`（默认开——能力优先；关闭即回现状直写）。factory 透传。
4. **存储零变更**：复用 `retrieve`/`write`/`update_content`/`soft_delete` 四原语；无 schema/migration；`MemoryItem` 不加字段（标题/标签等留 A-MEM 全量观望项，§1.3）。
5. **可观测**：`record_memory_reconcile(*, op)`（`expert_work_cm_memory_reconcile_total{op=add/update/delete/noop/degraded}`，uplift_metrics）+ compressor `summary.update_mode` 结构化日志。

### 9.5 边界情况

| 场景 | 处理 |
|---|---|
| 二次压缩 middle 含多条历史 summary（理论上限 pass 链） | 取**最后一条**作前次摘要，更早的并入新事件转录（链收敛为单条运行摘要） |
| UPDATE 模式下前次摘要超大 | 与现状同（transcript 无硬上限）；3-pass + `ContextOverflowError` 兜底不变 |
| reconcile 近邻检索失败 | 候选回落直写 ADD（与 recall best-effort 同款契约） |
| ops LLM 输出含越界 index / 未知 op / 缺 target | 该候选回落 ADD；其余候选照常应用 |
| UPDATE/DELETE 目标已被并发删除（update_content 返 None / soft_delete 返 False） | 记 degraded，候选回落 ADD（UPDATE 场景）或跳过（DELETE 场景） |
| update_content 的威胁扫描 | 复用存储层既有路径（PATCH API 同款）；blocked 异常 → 该候选 degraded 跳过，不阻断其余 |
| 取消（RunCancelledError） | reconcile 全程在 writeback try 内，cancel 照常 re-raise |
| `reconcile_writes=False` | 字节级回现状直写（zero behavior change 开关） |

### 9.6 可观测（零债"可观测齐全"）

- `expert_work_cm_memory_reconcile_total{op=add/update/delete/noop/degraded}`。
- compressor 结构化日志：`context.summary mode=fresh|update`（现有压缩日志扩一个字段）。

### 9.7 测试 & 验收（CM-7 Exit）

- **compressor unit**：wrapper 含 preamble（reference-only 字样）+ 三段结构 prompt；二次压缩 UPDATE 模式（scripted summariser 断言收到前次摘要正文 + 新转录、不再是裸链式全量）；middle 多条 summary 取最后；无前次 summary FRESH 模式；现有 head/tail/leading byte-stable/3-pass/CM-3 时序测试不回归。
- **writeback unit**：无近邻 → 直 ADD 零 LLM 调用；有近邻 → ops 应用四分支（ADD/UPDATE/DELETE/NOOP 各一）；解析失败回落直写；目标失踪 degraded；`reconcile=False` 与现状字节一致；pre-compaction flush 不 reconcile。
- **protocol**：`MemoryPolicy.reconcile_writes` 默认/关闭/extra-forbid。
- **零债 6 条**：无 TODO；本 §9 与实现一致；metrics/日志齐全；CI 8/8；token 节省数字不声称（Mem0 论文数字不平移，自测留 CM-N5）。

### 9.8 Mini-ADR（CM-7 锁定）

| ID | 决策 |
|---|---|
| **CM-H1** | **preamble 进 `<context-summary>` 内**、消息类型/位置/标签全不变——L-1 cache prefix 与 L-4 通道零扰动；"背景非指令"语义单一来源 |
| **CM-H2** | **二次压缩 = 增量更新前次摘要**（识别 middle 内最后一条 summary → UPDATE 模式），不再链式"摘要的摘要"；三段结构（Facts/Decisions/Pending）锁定 prompt |
| **CM-H3** | **显式操作只在 run 末 writeback**（2026-06-10 用户拍板）；CM-3 压缩前 flush 保持直写（turn 内延迟敏感），残留重复由 consolidator 兜底 |
| **CM-H4** | **ops 四值 ADD/UPDATE/DELETE/NOOP**（Mem0 范式）；无近邻（cosine < 0.80）直 ADD 零 LLM 成本；一切失败回落直写 ADD——宁可重复绝不丢记忆 |
| **CM-H5** | **复用存储四原语**（retrieve/write/update_content/soft_delete），零 schema 变更；`MemoryItem` 不加结构化字段（A-MEM 全量 Zettelkasten 维持观望，§1.3） |
| **CM-H6** | `MemoryPolicy.reconcile_writes` 默认 **True**（能力优先）；False 字节级回现状；token 节省不声称数字，自测留 CM-N5 |

### 9.9 PR 切分（CM-7）

1. **CM-7 PR1 — compressor 摘要强化（①）**（已实现，PR #514）：preamble + 三段结构 prompt + UPDATE/FRESH 双模式 + `summary.update_mode` 日志 + unit tests（含现有压缩测试不回归）。
2. **CM-7 PR2 — writeback 显式操作（②，收尾 CM-7）**（已实现）：`flush_messages_to_memory(reconcile=)` + `_reconcile_and_apply`（近邻 + 批量 ops + best-effort 应用；policy 字段落在 `LongTermMemorySpec.reconcile_writes`——实际类名修正，语义同 §9.4）+ factory 透传 + `record_memory_reconcile` + unit/protocol tests + ITERATION-PLAN 回填。**→ CM-7 完成**。

> 每个 PR 在本 §9 基础上局部细化；ITERATION-PLAN 增 CM-7 backlog，ship 后回填 `[x]`+PR 号。

---

## 10. CM-8 详细设计 —— Plan 的 UI 通道 + approval resume ingest 修复

> **范围修正（2026-06-10 用户拍板）**：范围表原句"approval `decision='modify'` 无法编辑 `proposed_args` 再提交"经取证**已过期**——ApprovalCard 已有完整 Edit arguments → Monaco JSON → "Approve with edits" → `{decision:"modify", modified_args}` 流程，后端链路完整（DTO 验证 + `apply_resume_decision` 重写 args），6 vitest + 2 Storybook 在册。**CM-8 不重做 modify UX**。修正后范围三件事：
> ① **plan/todo 的 UI 通道**——可视化 + 可编辑（仅非 RUNNING 可写），补齐 C8 双通道的 UI 一侧；
> ② **修 approval resume ingest 漏洞**——resume 走 `aupdate_state(as_node="agent")` 直入 tools 不经 entry chain ⇒ CM-0 的 PLAN.md ingest 在 pause→resume 周期不跑，暂停期人改 PLAN.md 丢失（§11 衔接原"J.8 pause→ingest 时机"假设不成立，本节修复）；
> ③ **补 approval 流程 Playwright E2E**（PR 7e 残债，`runs.spec.ts:7`）。

### 10.1 关键约束（接缝核准结论，已源码核准）

| 约束 | 事实 | file:line |
|---|---|---|
| **modify UX 已实现** | ApprovalCard：Edit arguments → Monaco JSON（invalid 禁 Approve、dirty 切 "Approve with edits"）→ `resumeRun {decision:"modify", modified_args}`；后端 `ResumeRequest` 验证 modify 必带 modified_args、`apply_resume_decision` 重写 gated call args | `ApprovalCard.tsx:122-243`、`api/runs.ts:73-89`、`control_plane/api/runs.py:93-106,638-644`、`_approval.py:189-227` |
| **plan 前后端全缺** | 前端零 plan 字段/类型/组件；run detail 端点不含 plan；plan 只在 SSE `updates` chunk 间接出现；**无任何外部写 plan 的 API** | `RunDetail.tsx`、`runs.ts:43-53`、`control_plane/api/runs.py:446-511`、`sse.py:315` |
| **resume 不经 entry chain** | resume `aupdate_state(..., as_node="agent")` → 图从 agent 重入直达 tools；entry chain（memory_recall→workspace_ingest→planner）只在 run 始跑 ⇒ 暂停期 PLAN.md 编辑不回灌 | `control_plane/api/runs.py:710-728`、`builder.py:670-683` |
| **control-plane 读写 checkpoint 的两种成本** | resume 先按 spec build agent 再 `built.graph.aupdate_state`（每请求 build，一次性可接受）；checkpointer 本身在 lifespan 可得 ⇒ **读**可经 `checkpointer.aget_tuple(config)` 直读 channel_values 零 build（3s 轮询友好） | `control_plane/api/runs.py:693-728` |
| **plan 数据形态已定** | `Plan{goal, steps[PlanStep{id, description, status}]}`，status ∈ pending/in_progress/completed（CM-0 PR3 已加）；CM-0 投影/ingest/recitation 全用它 | `protocol/plan.py`、`context/workspace_projection.py` |
| **前端基建** | AntD v5 + CSS 变量 token + Monaco + lucide；i18n en/zh-CN；RunDetail 3s `useStatusPolling`（活跃 + tab 可见）；Storybook/vitest/Playwright 三层在册 | `apps/admin-ui/package.json`、`theme/`、`RunDetail.tsx:105-110` |
| **接线点清单（SE-8）** | 无新页面 ⇒ router/Sidebar/CommandPalette 不动；要动 SDK + i18n 双语 + Storybook + vitest + Playwright + envelope 对账（runs.ts 既有 raw payload 风格） | [memory:SE-8 清单]、`runs.ts:58-66` |

### 10.2 设计 ①：plan 读写 API + RunDetail Plan 面板

```
读（轮询友好，零 build）
  GET /v1/sessions/{thread_id}/plan
    → checkpointer.aget_tuple({thread_id, tenant_id}) → channel_values["plan"]
    → 200 {goal, steps:[{id, description, status}]} / 204 无 plan
前端
  RunDetail 新 PlanPanel（EventStreamPanel 同级 Card）：
    goal 标题 + 步骤列表（status 图标 ○/◐/✓）+ 完成度
    数据接入 RunDetail 既有 3s useStatusPolling 节奏（活跃 + tab 可见）
写（低频，复用 resume 先例）
  PUT /v1/sessions/{thread_id}/plan   body = Plan DTO（protocol 模型验证）
    状态机 gate：thread 最新 run 为 RUNNING → 409（运行中外部写有被
      agent 下一 turn update_plan/投影覆盖的竞态，2026-06-10 用户拍板拒之）
    → build agent（resume 同款）→ built.graph.aupdate_state({plan}, as_node="agent")
    → DB 权威落定；下一 turn CM-0 投影自动同步 PLAN.md（单向流模型不变，
      UI 通道无需独立 reconciliation——与文件通道殊途同归于 DB）
  前端编辑：PlanPanel Edit 模式 = 结构化表单（goal 输入 + 步骤行内编辑/
    状态选择/增删），非 raw JSON（产品级基线）；RUNNING 时 Edit 禁用 + tooltip
```

### 10.3 设计 ②：approval resume ingest 修复（orchestrator 内部）

```
tools_node 入口（builder.py resume 分支）：
  state.approval_resume 非空 ∧ workspace_ingest_node 已接线
    → ingest_update = await workspace_ingest_node(state, config)   ← 一次
    → ingest_update 带 plan 候选 → 并入本批 result_dict（注入扫描/无变更
      no-op 语义复用 CM-0 原样）
  不动 entry chain、不动 control-plane resume 端点、不动 as_node 语义
```

暂停期两条人工干预通道就此对齐：改 PLAN.md（resume 时回灌）或 UI 编辑（PUT 直写 DB，PAUSED 非 RUNNING 可写）。

### 10.4 数据/协议变更

1. **control-plane `api/plan.py`（新）**：GET/PUT `/v1/sessions/{thread_id}/plan`；GET 经 checkpointer 直读（lifespan 注入）；PUT 经 build+`aupdate_state`（resume 同款 lazy import 纪律）；PUT 校验 protocol `Plan` 模型 + RUNNING→409 + tenant/user scope 同 runs 端点；响应 raw payload（对账 runs.ts 既有风格，无 envelope）。
2. **audit**：`AuditAction.PLAN_EDITED`——**protocol 与 control-plane 两处 Literal 同步加**（[memory:audit-literal-drift]）；PUT 成功发射（resource_type=`thread`，detail 含步骤数 delta）。
3. **orchestrator `builder.py`**：tools_node resume 分支调既有 `workspace_ingest_node`（已是 build_react_graph 参数，零新参数）；ingest 返回的 plan 并入 result_dict。
4. **admin-ui**：`api/plan.ts`（SDK：getThreadPlan/updateThreadPlan + Plan/PlanStep 类型）；`run_detail/PlanPanel.tsx`（只读视图 + 结构化编辑模式）；i18n `plan_panel.*` en/zh-CN 双语；Storybook（只读/编辑/空 plan）+ vitest + Playwright。
5. **无 schema/migration**；plan 仍只活在 checkpoint（DB 权威），不建新表。

### 10.5 边界情况

| 场景 | 处理 |
|---|---|
| thread 无 checkpoint / 无 plan | GET 204；PlanPanel 渲染空态（"agent 尚未制定 plan"） |
| PUT 时 run 恰好转 RUNNING | 状态读取与 aupdate_state 间的窗口竞态——agent 写最终覆盖，可接受（外部写本就 best-effort 干预）；状态机 gate 挡住绝大多数 |
| PUT body 步骤 id 重复/status 非法 | protocol `Plan` 模型验证 422 |
| PUT 含注入内容 | 与 ingest 同款 strict 扫描（goal+步骤描述），命中 422 拒绝（DB 权威不染） |
| resume 时 PLAN.md 没改 | ingest 无变更 no-op（CM-0 语义原样） |
| resume ingest 扫描命中 | 拒绝回灌、DB 权威，approval 流程照常继续（best-effort） |
| 非 plan_execute agent（无 planner） | plan 通常为 None → GET 204、面板空态；PUT 仍可写（plan channel 通用） |
| RUNNING 中看面板 | 只读实时（3s 轮询）+ Edit 禁用 tooltip 说明 |

### 10.6 可观测（零债"可观测齐全"）

- `PLAN_EDITED` audit（control-plane 标准 audit 管道）。
- control-plane 结构化日志 `plan.read`/`plan.edited`（无请求派生值进 extra，[memory:log-injection]）。
- orchestrator resume ingest 复用 CM-0 `expert_work_cm_ingest_total{outcome}`（零新 metric）。

### 10.7 测试 & 验收（CM-8 Exit）

- **control-plane**：GET 有/无 plan（200/204）+ scope 隔离；PUT 校验/409 RUNNING/注入 422/audit 发射/aupdate_state 落 checkpoint（fake graph）。
- **orchestrator**：resume 分支触发 ingest（spy ingest node）+ plan 候选并入 state + 无 ingest 接线零行为变更 + 扫描命中不回灌。
- **admin-ui vitest**：PlanPanel 只读渲染（含空态/状态图标）/ 编辑模式增删改 / RUNNING 禁用 / PUT 提交 payload 形状；Storybook 三态；Playwright：plan 面板可见性 + approval 全流程 E2E（PR 7e 残债，fixtures 按现状细化）。
- **零债 6 条**：无 TODO；本 §10 与实现一致；audit/日志齐全；CI 全绿；范围表 modify UX 标已实现不留幻影条目。

### 10.8 Mini-ADR（CM-8 锁定）

| ID | 决策 |
|---|---|
| **CM-I1** | **modify UX 已实现，CM-8 不重做**（范围表假设过期的显式修正记录）；残留的 approval E2E（PR 7e）并入本项收尾 |
| **CM-I2** | **plan 读写走 thread 级端点**：GET 经 checkpointer 直读（零 build、3s 轮询友好）；PUT 经 build+`aupdate_state(as_node="agent")`（resume 同款先例，低频）；DB 权威 → 下一 turn 投影自动同步 PLAN.md——UI 通道复用 CM-0 单向流模型，无独立 reconciliation |
| **CM-I3** | **写仅非 RUNNING**（409）——运行中外部写有被 agent 覆盖竞态（2026-06-10 用户拍板）；剩余 TOCTOU 窗口接受（agent 写终胜） |
| **CM-I4** | **resume ingest 修复在 tools_node resume 分支**调既有 ingest 节点——不动 entry chain、不动 control-plane resume 端点、不动 as_node 语义；CM-0 扫描/no-op 语义原样 |
| **CM-I5** | **前端结构化编辑**（goal + 步骤行内编辑/状态/增删），非 raw JSON；遵守 Admin UI 设计基线 10 条（dark-first / 非默认 AntD 堆砌） |
| **CM-I6** | `PLAN_EDITED` audit **protocol + control-plane 双 Literal 同步**；PUT 注入扫描与 ingest 同 strict 档（DB 权威不染） |

### 10.9 PR 切分（CM-8）

1. **CM-8 PR1 — 后端 plan API**（已实现，PR #517）：control-plane GET/PUT `/v1/sessions/{thread_id}/plan` + 409 状态机 + 注入扫描 + `PLAN_EDITED` audit（实情为单源 StrEnum，无双 Literal 镜像；resource_type 复用 `session`）+ tests。
2. **CM-8 PR2 — resume ingest 修复（orchestrator）**（已实现，PR #518）：tools_node resume 分支调 ingest + tests。
3. **CM-8 PR3 — 前端 PlanPanel**（已实现，PR #519）：SDK + 只读视图 + 结构化编辑 + i18n 双语 + Storybook + vitest。
4. **CM-8 PR4 — E2E（收尾 CM-8）**（已实现）：approval 全流程 + plan 面板 Playwright + ITERATION-PLAN 回填。**→ CM-8 完成**。

> 每个 PR 在本 §10 基础上局部细化；ITERATION-PLAN 增 CM-8 backlog，ship 后回填 `[x]`+PR 号。

---

## 11. CM-9 详细设计 —— Effort 档位 + 撞限升档

> **范围修正（2026-06-10 用户拍板）**：原范围 4 项中 2 项经取证**已实现**——iteration budget（`WorkflowSpec.max_iterations=12` + `MaxStepsExceededError` + L-5 refund，`builder.py:324`）与调用指纹去重（E.10.5 `LoopDetectionMiddleware`：标准化 args+sha256 指纹、窗口 3 连击、命中清 tool_calls + reminder，always-on，`expert-work-runtime/middleware/loop_detection.py`）。**per-run plan mode 开关砍掉**（显式设计决策：plan mode 能力本体已存在于 manifest 级 `workflow.type=plan_execute`；per-run 切换与"agent 即产品"形态相悖，且 build 缓存按 (tenant,name,version) 键控，per-run 变体改动面大收益边际——CM-J1 记录）。修正后两件事：
> ② **effort 档位接通**——adapter 零 thinking 支持（2023-06 手工 httpx body 无 thinking/output_config 字段），`ModelSpec` 无 effort 字段；
> **N4 撞限升档**——loop 命中 / 预算临近时现状只有 reminder / 直接失败，无"加算力重思路"的升档（用户拍板：双信号任一命中即升一档）。
> 顺带修**正确性隐患**：opus-4-7/4-8 已移除 `temperature`（发即 400，claude-api skill 核准），Expert Work adapter 无条件发 temperature 且目录含 `claude-opus-4-8` flagship——今天配 opus-4-8 的租户全量 400。

### 11.1 关键约束（接缝核准结论，已源码核准 + claude-api skill 核准线格式）

| 约束 | 事实 | file:line |
|---|---|---|
| **adapter 零 thinking 支持** | `AnthropicClient.messages()` 仅 model/system/messages/tools/max_tokens/temperature；HTTP body 同；`anthropic-version: 2023-06-01`（够用，effort/adaptive GA 无需 beta header） | `llm/providers/anthropic.py:71,99-147` |
| **线格式（claude-api skill 核准，非记忆）** | `thinking: {"type":"adaptive"}`（自动 interleaved，无 beta header）；`output_config: {"effort":"low"\|"medium"\|"high"\|"max"}`；`budget_tokens` 弃用不碰；**haiku-4-5 发 effort 会 400**；**opus-4-7/4-8 发 temperature/top_p/top_k 会 400** | claude-api skill §Thinking & Effort |
| **ModelSpec 无算力字段** | 仅 temperature/max_tokens/cache_enabled/context_window 等 | `protocol/agent_spec.py:74-152` |
| **provider 构造链** | `_build_provider(model, ...)` 读 ModelSpec → `AnthropicProvider(model=, max_tokens=, temperature=, cache_enabled=)` | `agent_factory.py:1234-1313` |
| **LLMCaller 协议窄** | `(messages, tools) -> AIMessage`，无 per-call 参数通道——升档不改协议（改 = 全仓 fakes/doubles sweep） | `llm/caller.py:20-37` |
| **loop 检测命中点可带出信号** | `after_llm_call` 链在 agent_node 内 invoke，`ctx.payload` 双向——middleware 可置 payload 标志，agent_node 链后读取写 state | `builder.py`（after_llm_call 调用处）、`loop_detection.py:165-175` |
| **预算信号本地可得** | agent_node 已有 step_count/max_steps | `builder.py:324` |
| **模型目录单源** | `ModelEntry(name, vision, context_window)` 已有 capability 字段先例（vision）——加 effort/sampling 能力同范式 | `protocol/model_catalog.py:40-46` |
| **prompt cache 不受档位影响** | thinking/effort 是请求参数不在 prompt 字节里（invalidation hierarchy：thinking 开关不破 tools/system 缓存）——升档零 cache 代价 | claude-api skill §Invalidation hierarchy |

### 11.2 设计 ②：effort 档位接通（manifest → provider → wire）

```
ModelSpec 新字段（默认全 None/False ⇒ 零行为变更）
  effort: "low"|"medium"|"high"|"max"|None = None     ← None 不发（API 默认）
  adaptive_thinking: bool = False                      ← True 发 thinking:{"type":"adaptive"}

model_catalog ModelEntry 加能力位（单源）
  supports_effort:   opus-4-8 ✓ / sonnet-4-6 ✓ / haiku-4-5 ✗
  supports_sampling: opus-4-8 ✗（4.7+ 移除）/ sonnet-4-6 ✓ / haiku-4-5 ✓

factory _build_provider gate
  effort 设置 ∧ 模型不支持 → AgentFactoryError（fail-fast 于 build，不让 400 漏到运行时）
  模型不支持 sampling → provider 不发 temperature（修 opus-4-8 现存 400 隐患）
  目录外模型（自定义 base_url 等）→ 不 gate，按配置原样发（兼容自托管网关）

adapter
  AnthropicClient.messages() 加 thinking/output_config 可选参；HTTP body 条件携带；
  temperature 改条件携带（send_sampling gate）
```

### 11.3 设计 N4：撞限升档（escalated caller，零协议变更）

```
factory：base effort 可升（< max ∧ 模型支持 effort ∧ adaptive_thinking 或 effort 已配）
  → 额外构造 escalated_llm_caller（同 model，effort 升一档：None/low→medium→high→max）
  → build_react_graph 新可选参 escalated_llm_caller

信号（用户拍板双信号，任一命中该 turn 切 escalated caller）
  ① loop 命中：LoopDetectionMiddleware 命中时置 ctx.payload["loop_detected"]=True；
     agent_node 在 after_llm_call 链后读取 → 写 AgentState.escalate_next=True；
     下一 turn agent_node 消费并 reset（同一动作连发 3 次 = 模型在瞪睖，加算力重思路）
  ② 预算临近：step_count ≥ ceil(0.75 × max_steps)（预算将尽，深思一次比多试几次便宜）

agent_node
  escalation = state.escalate_next ∨ step_count ≥ 0.75·max_steps
  caller = escalated_llm_caller if escalation ∧ escalated_llm_caller else llm_caller
  + expert_work_cm_effort_escalation_total{signal=loop/budget}
```

不变量：① 升档只影响该 turn 的 LLM 调用参数，prompt 字节不变（cache 零代价）；② 无 escalated caller（模型不支持/已是 max/未配 effort 基建）⇒ 现状；③ LLMCaller 协议零变更（fakes/doubles 零 sweep）；④ loop reminder 注入语义不变（升档是叠加不是替代）。

### 11.4 数据/协议变更

1. **protocol**：`ModelSpec.effort` + `ModelSpec.adaptive_thinking`（extra=forbid 下新字段，默认零行为变更）；`ModelEntry.supports_effort/supports_sampling` 能力位。
2. **orchestrator adapter**：`AnthropicClient` Protocol + `HTTPAnthropicClient` + fake 加 thinking/output_config 可选参（[memory:protocol-sweep]：全仓扫 doubles，含 tools/eval）；`AnthropicProvider` 构造参数 + body 条件字段；temperature 条件化。
3. **factory**：`_build_provider` 能力 gate + escalated caller 构造；`build_react_graph` 新可选参 `escalated_llm_caller`。
4. **runtime loop_detection**：命中时 `ctx.payload["loop_detected"]=True`（一行）。
5. **state**：`AgentState.escalate_next: NotRequired[bool]`（transient 通道，消费即 reset）。
6. **可观测**：`expert_work_cm_effort_escalation_total{signal}` + 结构化日志 `llm.effort_escalated`。

### 11.5 边界情况

| 场景 | 处理 |
|---|---|
| manifest 未配 effort/adaptive_thinking | 全部不发（现状字节一致）；escalated caller 不构造，升档信号空转 |
| haiku 配 effort | build 期 AgentFactoryError（fail-fast，不漏运行时 400） |
| opus-4-8 配 temperature | 不发 temperature（修复现存 400；日志 INFO 提示被忽略） |
| base effort=max | 无可升，escalated caller 不构造 |
| fallback 链（多 ModelSpec） | 每段 provider 按各自 spec/能力独立 gate；escalated caller 只对主 caller 构造（fallback 本身已是降级路径） |
| loop 命中 + 预算临近同 turn | 同一升档（一档封顶，不叠加） |
| 自定义 base_url 网关模型不在目录 | 不 gate 原样发（运营自负），日志 DEBUG |

### 11.6 测试 & 验收（CM-9 Exit）

- **protocol**：effort/adaptive_thinking 默认与取值校验、extra-forbid；catalog 能力位。
- **adapter unit**：body 含/不含 thinking、output_config、temperature 的全组合；fake client 记录断言。
- **factory**：haiku+effort 报错；opus-4-8 不发 temperature；escalated caller 档位推导（None→medium、low→medium、high→max、max→不构造）。
- **graph 集成**：loop 命中 → payload 标志 → escalate_next → 下 turn 用 escalated caller（spy caller 断言）+ reset；step 预算 75% 切换；无 escalated caller 零行为变更；metrics。
- **零债 6 条**：无 TODO；本 §11 与实现一致；可观测齐全；CI 全绿；效果数字不声称（升档收益自测留 CM-N5 范畴）。

### 11.7 Mini-ADR（CM-9 锁定）

| ID | 决策 |
|---|---|
| **CM-J1** | **per-run plan mode 开关砍掉**（显式决策非能力缺口：能力本体在 manifest `workflow.type`；per-run 切换与 agent-即-产品形态相悖 + build 缓存键控成本，2026-06-10 用户拍板）；iteration budget 与指纹去重标已实现 |
| **CM-J2** | **effort 走 `output_config.effort`、thinking 走 `adaptive`**（claude-api skill 核准线格式；budget_tokens 弃用不引入）；`ModelSpec.effort/adaptive_thinking` 默认 None/False 零行为变更 |
| **CM-J3** | **模型能力位进 model_catalog 单源**（supports_effort/supports_sampling）；build 期 fail-fast gate；**opus-4-8 不发 temperature**（修现存 400 隐患）；目录外模型不 gate |
| **CM-J4** | **升档 = escalated caller 切换**（factory 预构造高一档 caller，agent_node 按信号选用）——零 LLMCaller 协议变更、零 doubles sweep、prompt cache 零代价 |
| **CM-J5** | **双信号**（2026-06-10 用户拍板）：loop 命中（middleware payload 标志 → `escalate_next` transient 通道）∨ step_count ≥ 75% max_steps；一档封顶不叠加 |
| **CM-J6** | 升档只对主 caller（fallback 链已是降级路径不升档）；效果数字不声称，自测留 CM-N5 |

### 11.8 PR 切分（CM-9）

1. **CM-9 PR1 — effort 基建**（已实现，PR #522）：protocol 字段 + catalog 能力位 + adapter thinking/output_config/sampling 条件化 + factory gate + tests（含 opus-4-8 temperature 修复）。
2. **CM-9 PR2 — 撞限升档（收尾 CM-9）**（已实现）：escalated caller 构造 + loop payload 标志 + `escalate_next` 通道 + agent_node 切换 + metrics + 集成测 + ITERATION-PLAN 回填。实现细化：loop 命中即收尾当前 run（middleware 清 tool_calls），`escalate_next` 落 checkpoint 由**下一 run 首步**消费——跨 run 升档语义（人再推一把时 agent 加力重试）。**→ CM-9 完成**。

> 每个 PR 在本 §11 基础上局部细化；ITERATION-PLAN 增 CM-9 backlog，ship 后回填 `[x]`+PR 号。

---

## 12. CM-N5 详细设计 —— 评测基线（LongMemEval + LoCoMo 自测纳入 eval）

> **范围拍板（2026-06-10 用户）**：**分层全做** —— P0 检索层指标（零 LLM、机械回归）+ P1 端到端 QA（LLM judge、可横比）；端到端口径取**大档全量**（LongMemEval_S 全 500 题 + LoCoMo 10 对话 cat1-4 全 1540 题），与论文口径一致可直接横比 Mem0/Zep。
>
> **动机**：CM-4（reranker）/ CM-6（decay+MMR）/ CM-7（reconcile 写入）全部交付时按 N5 纪律**不声称数字**（厂商自报不可横比，Zep LoCoMo 84%→独立复核 58.44%，getzep#5）。本条出 Expert Work 自测数字作回归基线——此后任何检索/记忆改动有数可对。

### 12.1 关键约束（取证结论，2026-06-10 源码 + 一手数据集核准）

| 约束 | 事实 | 出处 |
|---|---|---|
| **eval 基建既有** | `CapabilityReport`/`load_cases`/`evaluate_set` 模式 + `ScriptedJudge`（CI）/`AnthropicHaikuJudge`（真跑，J-39 固定 haiku temp=0）双轨；`run_baseline.py` 不进 CI、`test_*.py` 进 CI | `tools/eval/_capability.py`、`_judge.py`、`run_baseline.py` |
| **检索链可独立调用** | `MemoryStore.retrieve`（hybrid RRF+decay）→ `_rerank_memories` → `_mmr_memories`，三段脱离 graph 可组装；依赖仅 Embedder/Store/可选 Reranker | `persistence/memory/base.py:53`、`orchestrator/graph_builder/memory.py:182-250` |
| **写入链可独立调用** | `flush_messages_to_memory(messages, *, memory_store, embedder, llm_caller, ..., reconcile)` —— CM-7 抽取+reconcile 真路径 | `graph_builder/memory.py:532` |
| **LongMemEval** | MIT；HF `xiaowu0162/longmemeval-cleaned` 裸 JSON（S=277MB / oracle=15.4MB，各 500 题）；自带 `answer_session_ids`（session 级）+ turn 级 `has_answer` ground truth → **检索召回指标零 LLM 可算**；30 题 abstention（`_abs` 后缀）官方检索评测跳过；端到端 judge 按题型用不同 prompt（`src/evaluation/evaluate_qa.py`） | github.com/xiaowu0162/LongMemEval |
| **LoCoMo** | **CC BY-NC 4.0（非商用）**；GitHub `data/locomo10.json` 2.8MB；10 对话 ×19-32 sessions×~588 turns；1986 QA（cat1 multi-hop 282 / cat2 temporal 321 / cat3 open-domain 96 / cat4 single-hop 841 / cat5 adversarial 446）；官方词法 F1 已被社区弃用，标准口径 = Mem0 式 LLM judge、**cat1-4 only**（cat5 计入即 Zep 84→58 争议根源） | github.com/snap-research/locomo、getzep#5 |
| **CI 无模型凭证** | 真 LLM/embedder 只能本地手动（[memory:ci-no-model-keys]）；CI 只能跑 fake + 确定性 | 既有事实 |
| **decay 时间不可注入** | 两 store `retrieve` 内部 `now=datetime.now(UTC)` 硬编码；但 decay 只依赖 age=now−anchor → **时间平移等价**（question_date 平移到真实 now，session 时间戳同步平移，age 不变，零协议变更） | `memory/sql.py:196`、`memory/memory.py:144` |
| **created_at 写入路径不对称** | `MemoryItem.created_at` caller 可设；InMemory `write` 原样保留；**SQL `write` payload 静默丢弃 created_at**（server default）——PR2 补全（item 显式带值才生效，生产路径 None ⇒ 行为不变） | `memory/sql.py:119-137`、`memory/memory.py:88-100` |
| **参考数字区间** | LoCoMo（J 口径，cat1-4）：full-context gpt-4o-mini 72.9% / Mem0 66.9% / 朴素 RAG ~61% / Zep 复核 58.4%——**低于 61% = 没跑赢朴素 RAG**；LongMemEval_S：full-context gpt-4o 60.2% / 好的检索系统 70%+ / oracle ~82% | Mem0 论文、Zep 论文、getzep#5 |

### 12.2 总体架构 —— 两层四态

```
tools/eval/longmem/                       ← 新子包（体量大于单文件能力评测，不平铺）
  download.py    数据集获取：HF resolve / GitHub raw 下载 + sha256 校验
                 → cache tools/eval/datasets/.longmem_cache/（gitignored）
  adapter.py     benchmark instance → Expert Work 形态：
                 session/turn 切块、时间平移（question_date→now，session date 同步平移）、
                 LoCoMo per-speaker user_id（Mem0 口径）
  retrieval.py   P0 检索层 harness：切块 → embed → store.write(created_at=平移后 session date)
                 → retrieve(+rerank+MMR 消融开关) → 指标
  metrics.py     Recall@k（session 级 / turn 级）、NDCG@k、MRR —— 纯函数
  endtoend.py    P1 端到端 harness：ingestion（flush_messages_to_memory, reconcile=True）
                 → 检索 top-k → reading prompt（带时间戳）→ LLM 回答 → judge
  judge.py       LongMemEval per-type 官方 judge prompt 移植 + LoCoMo Mem0 ACCURACY_PROMPT 口径
  run_longmem.py 运行器（不进 CI）：层/数据集/消融开关/采样/输出 baseline YAML
  test_*.py      CI 单测：loader/切块/时间平移/指标/judge prompt + fake-embedder
                 微型 synthetic fixture 全管线冒烟（确定性，不含原数据集数据）
tools/eval/baselines/longmem_baseline.yaml  ← 基线数字落盘（git 提交）
```

**层 1（P0）检索层 —— 机械回归闸**：ground truth 用 `answer_session_ids`/`has_answer`，零 LLM。**消融开关矩阵即归因机制**：`decay on/off × MMR on/off × rerank on/off × hybrid/vector`——CM-4/6 各组件增益独立可读，不靠端到端噪声归因。backend = `InMemoryMemoryStore`（镜像 SQL 行为；jieba keyword rank vs ts_rank 差异标注，baseline fingerprint 记 backend）；SQL+pgvector 真路径的 decay/时间戳语义由 CM-K7 integration 测试直接覆盖（runner 不再做 pg backend 开关——harness 双 backend 是重复面，归因在 in-memory 完成即可）。embedder 双态：fake keyword（CI 冒烟，确定性）/ 真 embedder（本地基线数字）。

**层 2（P1）端到端 QA —— 横比口径**：ingestion 走 `flush_messages_to_memory(reconcile=True)`（CM-7 真路径，逐 session 喂入）；reading 拿 top-k 记忆 + 时间戳组 prompt；judge 双口径——LongMemEval 移植官方 per-type prompt（temporal 容忍 off-by-one、knowledge-update 含新值即对、abstention 判拒答），LoCoMo 用 Mem0 ACCURACY_PROMPT、cat1-4、多次运行报均值±std。judge model = claude（既有 `AnthropicHaikuJudge` 模式）——**口径如实标注 judge 差异**（官方 gpt-4o judge），数字主要用于自测纵比，横比时注明。

> **PR3 实现期修订（口径细化）**：① LoCoMo 改**单记忆库 per 对话**（原文写 per-speaker 双库 Mem0 口径）——双库喂同一 transcript 经 LLM 抽取后内容趋同，只剩 2x 成本；Expert Work 产品形态本就是单 per-user 记忆，差异记入 baseline fingerprint。② 端到端层内 decay 中性（`flush` 写入即时间戳 now，全语料同期入库）——session 日期经 transcript 头注入、由抽取 LLM 编进记忆 content（Mem0 metadata-timestamp 等价物）；decay 归因职责在层 1（CM-K4 时间平移）。

### 12.3 数据/协议变更

1. **不动任何生产协议**：时间平移使 decay 验证零协议变更；检索/写入链按现有公开签名组装。
2. **SQL store 小补**：`write()` payload 尊重 `item.created_at`（显式设值才带，None ⇒ server default 不变）——既是 eval 需要，也是 `write(items)` 文档语义（"each item carries its own fields"）的正确性补全。
3. **tools/eval**：新增 `longmem/` 子包 + `datasets/.longmem_cache/` gitignore 条目 + `baselines/longmem_baseline.yaml`。
4. **依赖**：零新增 pip 依赖（裸 JSON 下载用标准库 / 已有 httpx；不引 HF datasets 库）。

### 12.4 边界情况

| 场景 | 处理 |
|---|---|
| LoCoMo license（CC BY-NC） | **不 vendor 进 repo**（商业 repo 再分发风险）；运行时下载 + cache；CI 用手工 synthetic fixture |
| LongMemEval 体积（S=277MB） | 同样不 vendor；下载 + sha256 校验 + cache；oracle（15MB）可选作快速档 |
| abstention 30 题 | 检索层跳过（官方口径，无 ground-truth 位置）；端到端保留（判拒答能力） |
| LoCoMo cat5 adversarial | 端到端**排除在 overall 之外单独报**（Zep 争议教训：算进 overall 即虚高） |
| LoCoMo cat3 answer 带 `;` 接理由 | judge 输入在 `;` 截断（官方口径） |
| 时间平移期间 wall-clock 流逝 | 跑批数小时 vs 半衰期 30 天，age 误差 <0.5%，忽略（标注） |
| 写入被 threat scan 拦截 | benchmark 原文可能踩 strict scan——计数上报 `blocked_writes`，不静默吞（数字旁标注损耗） |
| 真跑中断恢复 | run 脚本按 question_id 断点续跑（结果 jsonl 追加，已答跳过）——全量 $100+ 不能一断全重 |
| 消融开关与生产默认 | baseline 主数字 = 生产默认形态（hybrid+decay+MMR+rerank 全开）；消融行只为归因 |

### 12.5 测试 & 验收（CM-N5 Exit）

- **CI 单测**（确定性，无网络/无 key）：loader 解析两数据集 schema（fixture）、切块、时间平移不变量（平移前后 age 相等）、Recall/NDCG/MRR 纯函数、judge prompt 组装、LoCoMo cat 分流；fake-embedder 微型 fixture 跑通 P0 全管线（数字可复现断言）。
- **本地真跑**（手动）：层 1 真 embedder 全量出基线；层 2 全量出 QA 数字；两者写 `longmem_baseline.yaml`（含 config 指纹：embedder/judge model、k、开关组合、dataset sha256、commit、runs 数）。
- **回归纪律**：此后检索/记忆类 PR，本地重跑层 1（真 embedder）数字进 PR 描述对比基线；层 2 里程碑跑。
- **零债 6 条**：无 TODO；CI 测试齐；本 §12 与实现一致；`blocked_writes` 等损耗可观测；CI 全绿；基线数字落盘。

### 12.6 Mini-ADR（CM-N5 锁定）

| ID | 决策 |
|---|---|
| **CM-K1** | **分层两态**（2026-06-10 用户拍板）：P0 检索层零 LLM 机械回归（日常闸）+ P1 端到端 LLM judge（横比口径）——检索改动归因不靠端到端噪声 |
| **CM-K2** | **全量口径**（用户拍板大档）：LongMemEval_S 500 题 + LoCoMo 10 对话 cat1-4 全 1540 题，与论文口径一致；cat5/abstention 单独报不进 overall（Zep 争议教训） |
| **CM-K3** | **数据集不 vendor**：LoCoMo CC BY-NC license + LongMemEval 277MB 体积；下载脚本 + sha256 + gitignored cache；CI 用 synthetic fixture（零 license 暴露） |
| **CM-K4** | **时间平移代替 now 注入**：decay 只依赖相对 age → question_date 平移到真实 now、session 时间戳同步平移——CM-6 decay 可验且零协议变更、零 doubles sweep |
| **CM-K5** | **消融开关矩阵 = 归因机制**：decay/MMR/rerank/hybrid 独立开关；主基线 = 生产默认全开形态 |
| **CM-K6** | **judge = claude haiku 双口径移植**（LongMemEval per-type 官方 prompt + LoCoMo Mem0 ACCURACY_PROMPT）；judge 差异如实标注，自测纵比为主 |
| **CM-K7** | **SQL `write()` 尊重 `item.created_at`**（显式设值才带）：eval 需要 + 文档语义正确性补全，生产路径零行为变更 |

### 12.7 PR 切分（CM-N5）

1. **CM-N5 PR1 — 设计**（本 §12，已实现，PR #524）。
2. **CM-N5 PR2 — P0 检索层**（已实现，PR #525）：download/adapter/retrieval/metrics + 时间平移 + 消融开关 + SQL 时间戳补全（CM-K7 实现期扩两列：`last_used_at` 同补——decay anchor 是 `last_used_at or created_at`，只补一列则 SQL 路径 decay 永远 fresh）+ synthetic fixture + CI 单测。
3. **CM-N5 PR3 — P1 端到端（收尾 CM-N5）**（已实现）：endtoend（ingestion 经 `flush_messages_to_memory`，单库口径修订见 §12.2）/judge（双协议逐字移植）/anthropic_client（temperature 按目录 sampling 能力位条件发，CM-9 同源）/runner（断点续跑 jsonl + baseline YAML + fingerprint）+ `run_longmem.py` CLI + CI 单测 + ITERATION-PLAN 回填。
4. **CM-N5 PR4 — 真跑准备（Qwen 全栈 + 提速）**（已实现）：用户拍板全 Qwen 真跑（2026-06-10）——`openai_client`（DashScope compatible-mode 的 LLMCaller/TextJudge 适配器；judge 模型差异同 CM-K6 进 fingerprint）+ `CachedEmbedder`（sqlite content-hash 缓存：5 臂消融不再 5x 重复 embed ~35 万 doc；DashScope embedding batch<=10 内部分批并发）+ 两层 harness `concurrency` 参数（共享 semaphore + gather 保序，concurrency=1 与串行等价，CI 确定性不变；并发下 jsonl 写有锁）+ runner `--llm-provider/--concurrency`。
5. **基线真跑**：本地全 Qwen 全量（层 1 + 层 2），数字单独 commit 进 `baselines/`。

> 每个 PR 在本 §12 基础上局部细化；ITERATION-PLAN 增 CM-N5 backlog，ship 后回填 `[x]`+PR 号。

---

## 13. CM-10 详细设计 —— 跨厂商思考深度统一（effort 抽象推广到全 provider）

> **立项（2026-06-10 用户拍板，即刻执行）**：CM-9 的 effort 档位 + 撞限升档当前是 **anthropic 专属**——但 web 调研核准（2026-06-10，全 9 provider 逐家），"同模型调思考深度"在多数厂商有原生参数，且业界有成熟统一抽象先例（OpenRouter effort-budget 双轨 / LiteLLM per-provider 翻译表）。本条把 CM-9 增益推广到全厂商；CM-9 架构（中立 ModelSpec 字段 + 目录能力位 + escalated caller）**不推翻只扩展**。

### 13.1 关键事实（web 调研核准 2026-06-10，按 provider）

| Provider | 参数 | 形态 | 传法 |
|---|---|---|---|
| anthropic | `output_config.effort` + `thinking:adaptive` | **effort 多档** | 顶层（CM-9 已接） |
| openai / azure | `reasoning_effort`（minimal/low/medium/high/xhigh，按模型分级） | **effort 多档** | Chat Completions 顶层字段 |
| deepseek | `reasoning_effort`（V4 实际两档：low/medium→high，xhigh→max） | effort 两档 | 顶层 |
| qwen/DashScope | `enable_thinking` + `thinking_budget`（混合推理商业模型默认不思考） | **budget 连续** | 非标准字段（body 顶层 merge，SDK 走 extra_body） |
| doubao/火山 Ark | `thinking:{type: enabled\|disabled\|auto, budget_tokens}` | budget 连续 + 三态 | 同上 |
| glm/智谱 | `thinking:{type: enabled\|disabled}`（enabled=动态思考，无 budget） | **仅开关** | 同上 |
| kimi/Moonshot | K2.5+ `thinking:{type}`；k2-thinking 永远思考无开关 | 仅开关 | 同上 |
| self-hosted | `chat_template_kwargs.enable_thinking`（key per-model 漂移） | 仅开关（不进目录，见 13.4） | extra_body |
| 换算先例 | OpenRouter：`budget = clamp(max_tokens x ratio, 1024, 128000)`，ratio low=.2/medium=.5/high=.8/(x)high 上限=.95 | — | — |

接缝（源码核准）：`OpenAIClient.chat_completions` body 仅 model/messages/tools/temperature（`providers/openai.py:127-133`）；vendor 端点差异已由 `openai_compatible.py` 工厂封装；`_escalated_model` 有 `provider != "anthropic"` 短路（`agent_factory.py:1062`）；`ModelEntry.effort: bool` 是 CM-9 刚加的能力位、下游消费者仅 factory gate + `_escalated_model` + eval anthropic_client（全仓可控 sweep）。

### 13.2 设计 —— 三形态能力位 + 翻译层

```
能力位（替换，CM-L1）
  ModelEntry.effort: bool  →  ModelEntry.thinking: Literal["effort","budget","toggle"] | None
    effort = 厂商有原生多档（anthropic/openai/azure/deepseek）
    budget = 厂商是连续思考预算（qwen/doubao）→ 档位经比例换算
    toggle = 厂商只有开/关（glm/kimi）→ 升档退化为"开思考"一跳
    None   = 无运行时思考控制（haiku / 纯思考模型 / embedding 等）

ModelSpec 零变更（CM-L2）
  effort: low|medium|high|max     ← 语义升级为厂商中立"思考强度档位"
  adaptive_thinking: bool          ← 非 anthropic 语义 = 开思考、深度交厂商动态

翻译层（CM-L3）：factory _thinking_payload(model, entry) -> dict | None
  anthropic            （不走此层，CM-9 通道不动）
  openai/azure/deepseek {"reasoning_effort": effort}
  qwen                  {"enable_thinking": true, "thinking_budget": 换算(effort, max_tokens)}
  doubao                {"thinking": {"type": "enabled", "budget_tokens": 换算}}
                        adaptive_thinking 无 effort -> {"thinking": {"type": "auto"}}
  glm/kimi              {"thinking": {"type": "enabled"}}
  换算（CM-L4，OpenRouter 先例）: clamp(max_tokens x {low:.2, medium:.5, high:.8, max:.95}, 1024, 81920)

通道：OpenAIClient.chat_completions 加 extra_body: dict | None 参数 -> merge 进 body 顶层
（qwen/doubao/glm 的 thinking 参数都是 body 顶层非标准字段，一个通道全覆盖）；
OpenAIProvider 加 thinking_payload 字段（factory 构造时翻译好，complete 透传）。
```

### 13.3 升档推广（CM-L6）

`_escalated_model` 去掉 anthropic 短路，条件改目录 `thinking` 能力位：

- **effort/budget 型**：梯子照旧 `None/low→medium→high→max`（budget 型档位在翻译层换算成预算）
- **toggle 型**：退化为一跳——base 未配思考（effort=None ∧ ¬adaptive_thinking）→ escalated=effort:"high"（即"开思考"）；已开 → None 无可升
- 双信号（loop ∨ 75% 预算）、escalated caller 预构造、prompt cache 零代价——CM-9 机制全部不动

### 13.4 边界情况

| 场景 | 处理 |
|---|---|
| 目录外模型（自定义网关/azure/self-hosted） | anthropic 原样发（CM-9 既有：线格式唯一）；**openai-compat 系不发**（thinking 线格式 per-vendor 不唯一，盲发=运行时 400 风险）——与 CM-9 差异显式记录（CM-L5） |
| haiku / 纯思考模型（deepseek-reasoner、kimi-k2-thinking）配 effort | 目录 thinking=None → build 期 AgentFactoryError（fail-fast，CM-9 gate 推广） |
| glm/kimi 配 effort=low vs max | 同为"开思考"（toggle 无深度），档位差异静默归一——日志 DEBUG 标注 |
| deepseek 配 low/medium | 原样发 reasoning_effort，API 自行折 high（厂商行为，不在 Expert Work 折） |
| qwen max_tokens=4096（默认）的 max 档 | 换算 3891，clamp 下限 1024 兜底；想要深思考预算应调大 max_tokens——文档标注 |
| fallback 链跨厂商 | 每段按各自 spec+能力位独立翻译/gate（CM-9 既有语义） |
| temperature 与 thinking 同发 | 各厂商均接受——不强管，运营自配 |

### 13.5 测试 & 验收（CM-10 Exit）

- **protocol**：thinking 三形态取值 + 目录条目全厂商核准标注；`effort: bool` 引用全仓清零（[memory:protocol-sweep] 含 tools/eval）。
- **翻译 helper**：全厂商矩阵（每形态 x 每档位 x adaptive-only）+ 换算 clamp 边界。
- **adapter 线级**：extra_body merge 进 body（MockTransport 断言 qwen/doubao/glm 三种 payload）；不配 thinking 时 body 字节不变。
- **factory**：gate 各形态 fail-fast；目录外 openai-compat 不发；escalated 梯子 per 形态（含 toggle 一跳）。
- **零行为变更**：manifest 未配 effort/adaptive_thinking ⇒ 全厂商 body 与现状 byte-identical。
- **零债 6 条**；效果数字不声称。

### 13.6 Mini-ADR（CM-10 锁定）

| ID | 决策 |
|---|---|
| **CM-L1** | **三形态能力位替换**：`ModelEntry.effort: bool` → `thinking: Literal["effort","budget","toggle"] \| None`——CM-9 刚 ship 下游消费者全仓可控，替换优于双字段并存（[memory:audit-literal-drift] 双份漂移教训）；全仓 sweep |
| **CM-L2** | **ModelSpec 零变更**：effort/adaptive_thinking 语义升级为厂商中立；manifest 不感知厂商差异 |
| **CM-L3** | **翻译层在 factory + extra_body 统一通道**：`_thinking_payload()` per-provider 映射；`OpenAIClient.chat_completions(extra_body=)` merge 顶层——一个通道覆盖全部非标准字段 |
| **CM-L4** | **budget 换算用 OpenRouter 比例先例**：`clamp(max_tokens x {.2/.5/.8/.95}, 1024, 81920)`——有先例背书，不自造换算 |
| **CM-L5** | **目录外 openai-compat 不发 thinking**（线格式 per-vendor 不唯一）；anthropic 目录外原样发不变（线格式唯一）——与 CM-9 的差异显式记录 |
| **CM-L6** | **升档梯子 per 形态**：effort/budget 照旧四档；toggle 退化一跳（未开→high 即开思考）；`_escalated_model` 条件改 thinking 能力位 |
| **CM-L7** | **adaptive_thinking 非 anthropic 语义 = 厂商动态思考**：doubao→`type:auto`、qwen→`enable_thinking` 不带 budget、glm/kimi→`enabled`、effort 型→不发（API 默认） |

### 13.7 PR 切分（CM-10）

1. **CM-10 PR1 — 设计**（本 §13，已实现，PR #528）。
2. **CM-10 PR2 — 能力位 + 翻译层**（已实现，PR #529）：thinking 三形态替换 sweep + 目录全厂商标注 + `_thinking_payload` + `OpenAIClient.extra_body` + 线级测试（默认零行为变更，翻译层未接线）。
3. **CM-10 PR3 — gate + 升档推广（收尾 CM-10）**（已实现）：`_build_provider` 全 compat 分支接线 thinking_payload + gate 推广（厂商中立文案；toggle 档位归一 DEBUG 日志）+ `_escalated_model` 去 anthropic 短路 + toggle 一跳（实现注记：toggle 是唯一 untouched-manifest 也升档的形态——off→on 即其全部升档轴，与 anthropic untouched 默认已动态思考的语义对齐而非背离）+ ITERATION-PLAN 回填。**→ CM-10 完成**。

---

## 14. CM-11 详细设计 —— 事件驱动升档（动态算力分配的第二组触发）

### 14.1 关键事实 + 缺口

CM-9（§11）已落地"撞限升档"机制：`escalated_llm_caller`（升一档 effort 的同模型 caller）+ `AgentState.escalate_next` 通道 + 双信号（loop 检测 / budget 撞限）。CM-10（§13）把这套推广到全 provider。**机制完备，但触发信号只有两个**——都是"系统压力"信号（在打转 / 步数快用尽）。

驾驭工程的"动态算力分配"还要两个**任务语义**触发（业界 harness 的慢思考动态开关）：

- **微观**：上一批工具调用出现**真异常**（非 `transient` 抖动）→ 本 turn 深推理消化。当前 `tool_failures` 通道（CM-1）只驱动 `<recovery-advisory>` 注入，不驱动升档——模型拿到恢复建议但仍用基线 effort 浅想。
- **宏观**：plan 目标变更（planner 重规划，或人工编辑 PLAN.md 经 CM-0 ingest 回灌 `AgentState.plan`）→ 一次深推理重校执行策略。当前无任何"目标变更"信号。

### 14.2 设计 —— 复用 CM-9 机制，加两信号（零新基建）

```
agent_node（LLM 调用前，§11.3 升档 block 处叠加）：
  error_signal = any(f.error_class != "transient" for f in tool_failures)   # tool_failures 已在手（CM-1，行内）
  current_goal = plan.goal if plan else None                                # plan 已在手（J.1，行内）
  goal_signal  = current_goal ∧ prior_goal ∧ current_goal != prior_goal     # prior_goal = state.last_plan_goal
  escalate = loop_signal ∨ budget_signal ∨ error_signal ∨ goal_signal        # 四信号或，任一命中
  caller   = escalated_llm_caller if escalate ∧ escalated_llm_caller else llm_caller
  signal_label = loop / budget / error / goal（优先序，复用 expert_work_cm_effort_escalation_total{signal}）
return：last_plan_goal = current_goal   # 每 turn rebaseline → 一次变更只升一档 turn
```

新 state 通道 `AgentState.last_plan_goal: NotRequired[str | None]`：agent_node 每 turn 写当前 `plan.goal`（react 图恒 None）。下 turn 与活 plan goal 比对。**首个 plan turn 无 prior（缺省）→ 不升档**：初始分解 planner 已深想过（§8 CM-J 同理），避免每 plan_execute run 首轮误触发。

### 14.3 不变量

① 与 CM-9 同——升档只改该 turn 的 LLM 调用参数，prompt 字节不变（prompt cache 零代价）；② 无 escalated caller（模型不支持/已 max/未配 effort）⇒ 现状字节一致；③ 两新信号都在 LLM 调用**前**可知（`tool_failures` 来自上一 tools 批、`plan.goal` 在 state），故**本 turn**升档（区别于 loop_signal 的 arm-then-consume——loop 只在响应后可知）；④ `transient` 排除：仅可重试抖动不值深想，与 CM-B5 retryable 门控语义一致；⑤ react 图 plan 恒 None → goal_signal 恒 False，零行为变更。

### 14.4 受影响文件

1. **state**：`AgentState.last_plan_goal`（NotRequired，overwrite reducer）。
2. **builder**：§11.3 升档 block 加 `error_signal`/`goal_signal` + 四信号或 + signal label 优先序；两 return dict（middleware/plain 路径）加 `last_plan_goal=current_goal`。
3. **可观测**：复用 `expert_work_cm_effort_escalation_total{signal}`——新增 label 值 `error`/`goal`（非新 metric，无 catalog 漂移）。

### 14.5 测试 & 验收（CM-11 Exit）

`tests/test_effort_escalation.py` 扩展 4 测：非 transient 工具错本 turn 升档 / `transient` 不升档 / plan goal 变更升档且 rebaseline / 首个 plan 不升档。test_state 键集 +1。1152 orchestrator 单元回归绿。

### 14.6 Mini-ADR（CM-11 锁定）

| ID | 决策 |
|---|---|
| **CM-M1** | **复用 CM-9 机制加信号，非新建**：CM-9 的 `escalated_llm_caller`+`expert_work_cm_effort_escalation_total{signal}` 已是升档基建；CM-11 仅加 `error`/`goal` 两信号进同一 block，零新 caller/metric/graph 参数（保 surgical）。微观=非 transient 工具错本 turn 升档（信号在调用前可知）；宏观=plan goal 变更（`last_plan_goal` rebaseline）；首个 plan 不算变更（planner 已深想）。`transient` 排除对齐 CM-B5。新 label 值非新 metric → 无 [memory:audit-literal-drift] 风险。 |

---

## 15. 与既有 Stream 的衔接

- **Stream J**：复用 `user_workspace`（J.15 卷）、`memory_item`（J.3）、approval（J.8——pause→resume 的 ingest 时机由 CM-8 §10.3 修复补齐）、`update_plan`（K.8）。
- **Stream L**：投影钩子在 agent_node/tools_node，与 L-1 cache prefix（system 冻结）、L-2 compressor（CM-2/3 在此之上）共存；recitation 放非 system 区不破 L-1。**L-4 mutation advisory 被 CM-1 收敛**（`failed_mutations`→`tool_failures`、`<mutation-advisory>`→`<recovery-advisory>`，mutation 校验作为 `mutation_not_landed` 一类保留），非并行；L-5 退款语义不动（失败工具调用照常计步）。
- **Stream SE**：CM-1（运行时 error-as-guidance）与 SE-12（离线 skill 进化失败归因）是**两个层面**——SE 学习离线进化、CM-1 运行时即时恢复，不重叠。
- **Stream H（admin-ui）**：CM-8 文件投影 + UI 双通道依赖 CM-0 的 ingest 路径。
