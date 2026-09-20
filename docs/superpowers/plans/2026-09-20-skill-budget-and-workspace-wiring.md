# 技能体积护栏 + 工作区块接线 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development

**Goal:** 给技能索引段装上字符预算与「只降不丢」降级；把 B-84 PR-2 的工作区块从系统提示词改接到尾部隐藏消息通道。

**Spec:** `docs/superpowers/specs/2026-09-20-workspace-visibility-design.md`（工作区块部分）

---

## Global Constraints（两条流都受约束）

1. **降级/注入都不许改系统提示词的前缀字节。** 技能索引在系统提示词里 = prompt 前缀；任何按轮/按用户变化的内容会让整段下游缓存作废。既有决定见 `graph_builder/builder.py` 的 `_inject_plan` docstring（L1）。
2. **永不丢弃条目。** 降级只降到「只留名字」。名字是恢复路径的句柄 —— `skill_view(name)` 按名字加载。
3. **降级必须明写告诉模型**，并为这句话预留字符预算。
4. **单位用字符，不用 token。** token 计数要 tokenizer 调用，且跨模型不确定，做不成确定性判据。
5. 所有新常量必须有 `Final[int]` 类型标注与说明来源的注释。
6. 本地验证一律 `uv run --no-sync pytest`；worktree 里必须带 `UV_PROJECT_ENVIRONMENT` + `PYTHONPATH`，信绿前先打 `__file__` 确认测的是本 worktree。

---

## 流 A —— 技能体积护栏

### 实测前提（2026-09-20，测试环境，复核用）

| 项 | 值 |
|---|---|
| 活跃技能 | 57，**eager = 0** |
| 最大绑定数 | 19（ai-health-plan / ai-health-report） |
| 最大索引体积 | 6,129 字符（sop2-designer，13 个） |
| 全 57 个绑一个 agent | 18,571 字符 |
| 每条索引 | 288 ~ 471 字符 |
| `prompt_fragment` | 最大 46,741 / p90 19,181 / 中位 7,527 字符 |
| 利用率 | ai-health-plan 绑 19，298 个 run 里只有 4 个被 `skill_view` 打开过（共 6 次） |

### Task A1：闸 A —— eager 正文上架上限

**Files:** `services/control-plane/src/control_plane/api/_skill_moderation.py`(+测试)

- 新增 `MAX_EAGER_PROMPT_FRAGMENT_CHARS: Final[int] = 8_000`
- **只在 `lazy_load is False` 时生效**。lazy 的沿用既有 `MAX_PROMPT_FRAGMENT_BYTES = 256*1024`，不动。
  - 理由必须写进注释：实测 lazy 正文 p90 = 19,181、最大 46,741，一刀切 8,000 会打死存量；而 eager 正文是**每轮**进系统提示词的，256 KB ≈ 64k token。
- 现状 0/57 eager → 无需 grandfathering，这句也写进注释（并写明是 2026-09-20 实测）
- 报错文案要说清：超了多少字符、两条出路（改 `lazy: true`，或拆成多个技能）

**验收:** 一条测试钉 eager 超限拒绝；一条钉 lazy 同样长度**放行**（这条是防后人把条件改成无条件）。

### Task A2：闸 B —— 索引段字符预算 + 两档降级

**Files:** `services/orchestrator/src/orchestrator/agent_factory.py`（`_render_skill_summary` 与 `<available-skills>` 组装处）(+测试)

- `MAX_SKILLS_INDEX_CHARS: Final[int] = 18_000`
- 档 1（默认）：`<skill name="..." description="..." />`
- 档 2（超预算）：**只留名字**，所有条目都在，一条不少
- **没有档 3。** 只留名字仍超预算 → 照发 + `logger.warning`，**不丢**。（57 个只留名字约 1–2 KB，实际够不着；留着是兜底，注释写明这一档预期永不触发）
- 降级时块首加一行告知，预留 `DEGRADE_NOTICE_OVERHEAD: Final[int] = 200`，形如
  `⚠️ N skills listed by name only (index size budget). Call skill_view(name) to read any of them.`
- **判定与渲染只能依赖绑定集合的内容与顺序**，不许读 `user_id` / 时间 / 使用频次

**变异自证（每条必须先红后绿）:**
1. 把「只留名字」改成截断丢弃 → 必须有测试红
2. 把预算判定改成依赖 `user_id` 或当前时间 → 必须有测试红
3. 去掉降级告知行 → 必须有测试红
4. 同一绑定集合、两个不同租户/用户 → 渲染结果逐字相同

**不做:** 绑定数量硬上限。字节预算已隐含 38~62 条的天花板（18,000 ÷ 288~471），而我们从未观测过超过 19 的 agent，没有依据定数量阈值。

---

## 流 B —— PR-2 工作区块接线改造

**分支:** `feat/b84-workspace-prompt`（PR #1650，**改它，不要关掉重开**）

### 保留
`services/orchestrator/src/orchestrator/tools/workspace_tree.py` 与 `tests/test_workspace_tree.py` —— 渲染器已过审，原样留。

### 撤掉
`build_agent(..., workspace_user_id=...)` 签名改动，以及 `agent_factory.py` 里把块拼进系统提示词的全部代码与对应测试。

**理由（写进 PR 正文）:** 工作区快照是每轮都变的动态内容，放进系统提示词会
①跨用户 PII 泄漏（`AgentRuntime.get_agent` 缓存键的 `oauth_subject` 只在用户接了 OAuth 时才等于 user_id，注释原文 "the common no-OAuth agent stays shared across users"）、
②每轮打掉 prompt 前缀缓存、
③撞 B-70（每轮 SystemMessage 累积，`coalesce_system_messages` 按原始顺序拼接 → 模型读到 N 份快照且最旧的排最前）。
这三条里第②条正是 L1 当初把 plan 从 SystemMessage 挪到尾部消息的原因，见 `_inject_plan` docstring。

### 改成
接到**尾部隐藏 HumanMessage** 通道 —— 与 `_inject_plan`(L1) 和 B-67 §六「本轮输入」段同一条路。

**硬要求:**
- **每轮重取快照**，不是 run 起点取一次。（尾部消息本来就每轮变，不影响前缀缓存；`_inject_plan` 就是这么做的。宿主 NAS listdir 实测 8.3ms，而重打一个 21k 字符文件要约 6 秒）
- **只保留最新一段。** 更早轮次的快照是**错的**（它声称是"现在有什么"）。新增 `WORKSPACE_BLOCK_MARK` 到 `packages/expert-work-common/src/expert_work/common/conversation_channel.py`，照 `INPUTS_BLOCK_MARK` 的形状；提示词视图里剔除除最新一条之外的所有带标记消息。
- 位置在**尾部**，不得插进任何 `tool_call` ↔ `tool_result` 之间，不得开新轮（照 `_keep_latest_inputs_block` docstring 的口径）。
- 只改这一次的提示词视图，**检查点不动**（CM-C4）。
- 数据来源走 PR-2b 已合的宿主侧 NAS 读，不起沙箱。
- **不加任何入口判断 —— 所有走 agent 图的路径都生效。** 接线点是 graph_builder 的每轮尾部注入，而七个 `configurable` 构造点走的是同一张图：不加限制就自动全覆盖；加限制反而要多写一道条件去挡掉六条路径。
  - 委派子代与父代**共享同一个工作区作用域** —— `services/control-plane/tests/test_agent_key_plumbing.py` 的 `_LAUNCH_SITES` 注释原文：「子代干的是父 agent 的活，产物必须落在父的子树里，否则父读不到自己 worker 刚写的文件」。块内容对两者是同一份，无作用域与泄漏问题。
  - 同一份测试的 docstring 记着前车之鉴：「执行入口三个，规矩只写一处就漏两个」(#1373 + #1382)；计划登记四个、实测九个调用点 / 七个构造点，四个里三个函数名还是错的。
  - 真实 worker 事件里父代在**手抄文件清单**给 worker（`【必读文件】/workspace/style/PLAN_STYLE.md、render_plan.py、清风_…json`）—— 正是本功能要自动化掉的事；而 `render_plan.py` 就是被跨 run 重打 6 遍的那个文件。
  - 触发器路径**没有人在回路里**，是最不该扣掉环境信息的一条。
  - **原理由是归类错误，记在这里**：原写「归 B-37 单独治」，但 B-37 治的是「从父代继承」(skills / memory / 父 prompt)，而工作区是**共享的环境状态**，worker 自己 `list_dir` 本来就看得见。两件事不是一件事。

**变异自证:**
1. 让第二段快照留在提示词里 → 必须有测试红
2. 把块挪回 SystemMessage → 必须有测试红（钉「系统提示词逐字不含工作区块」）
3. 块插进 tool_call/tool_result 之间 → 必须有测试红
4. 两个用户同一 agent → 各自只看到自己的文件（跨用户隔离）
5. 委派子代跑起来时提示词里**有**块，内容与父代一致 → 在实现里加「只有主 run 才注入」的条件时必须红

### PR 正文必须包含
撤掉原接线的理由（上面三条，带 file:line），以及「渲染器保留、签名改动撤销」的说明。
