# 子 Agent 上下文继承设计(B-37)

- 状态:设计已拍板(2026-08-28),开发中
- 关联:B-35 plan_first(#1356-#1360)、B-36 弹性 worker 预算(#1368)
- 原则拍板人:项目负责人(2026-08-28 会话)

## 一、问题

### 1.1 实证

测试环境会话 `abf70030-374e-4a51-9def-7dec47d3d04a`(run `d73fc7e3`,sop2-designer
生成 14 天秋季养生方案):

- 主 Agent `list_dir("style")` → `read_file` 读取 `style/PLAN_STYLE.md` 与
  `style/render_plan.py`,随后 **把风格锚整段手抄进 `spawn_worker` 的 task 文本**
  (「必须遵守的既有风格锚(不得违反)」一节:16:9、封面 LOGO 高 0.72″、
  标题 40pt 主色、正文行数 ≤7 用 13pt / 8-9 行 11.5pt / ≥10 行 10.5pt……)。
- 「PPT 内容排版设计」worker 全程只调用 `write_file` + `save_artifact`,
  **从未访问 `style/`**;真正的 pptx 渲染发生在 worker 返回之后,由
  **主 Agent 自己 `exec_python` 执行 `style/render_plan.py`** 完成。
- 即:表面上「子 Agent 做 PPT」,实际是「子 Agent 写设计说明,主 Agent 做 PPT」。

### 1.2 根因

`synthesize_worker_spec`(`services/control-plane/src/control_plane/subagent_runtime.py`)
把 worker spec 的 `skills` 置空:

```python
"skills": [],
```

连带后果:`skill_view` 工具的注册条件是 `activated_skill_names` 非空
(`agent_factory.py:945`),技能列表空 → **连按需查询技能的入口都不注册**。
worker 在技能维度是完全空白:没有摘要、没有正文、没有查询工具。

sop2-designer 绑定 13 个技能,其中包含 **pptx / docx / pdf / xlsx**。做 PPT 的
worker 一个都拿不到,只能依赖主 Agent 抄来的规则文本工作。

**这不是 sop2 一家的问题**:任何绑定了技能并启用动态子 Agent 的租户,其 worker
都处于同一状态。

### 1.3 现状对照(哪些继承、哪些不继承)

| 维度 | 现状 | 说明 |
|---|---|---|
| 工具(含 MCP) | ✅ 继承 | 仅剥 `manage_task`;三种 MCP 池 provider 全传;平台 `dynamic_worker_allowed_toolsets` 默认空 = 全继承 |
| 工作区 | ✅ 共享 | per-(tenant, user),worker 继承同一 `user_id`,文件读得到 |
| 模型 | ✅ 继承 / 可覆盖 | `dynamic_workers.model` 覆盖 |
| 防御(judge / tool budget) | ✅ 继承 | B-26 已对齐 |
| **技能** | ❌ 剥空 | 本设计要修的核心 |
| 系统提示词 | ❌ 不继承 | 平台生成 worker 模板(**符合业界共识,不改**) |
| 记忆 / 触发器 / 静态子 Agent / reflection / routing / knowledge | ❌ 剥空 | 状态或递归语义,本设计不动 |

## 二、业界调研结论(2026-08-28)

覆盖 Claude Code / CrewAI / LangGraph+Deep Agents / OpenAI Agents SDK /
Google ADK / AutoGen-Magentic-One,外加 Anthropic 与 Cognition 的工程博客、
MAST 论文(arXiv 2503.13657)。

### 2.1 共识:子 Agent 的系统提示词一律独立生成

五个框架**无一**支持父→子系统提示词继承。Deep Agents 字段表直接写
"Does not inherit from main agent";OpenAI Agents SDK 无继承模型;
ADK / CrewAI 均为 per-agent 定义。**我们剥父提示词的做法是对的,不改。**

### 2.2 但每家都有一个「共享约定」的独立位置,由框架自动下发

| 框架 | 共享约定的家 | 下发方式 |
|---|---|---|
| Claude Code | CLAUDE.md 分层文件 + 子 Agent 的 `skills:` 预载 | 启动自动加载;子 Agent 加载「同一套 MCP 和技能配置」 |
| CrewAI | crew 级 skills / knowledge / memory | 配一次,团队内所有 Agent 自动生效 |
| Google ADK | `global_instruction` 字段(→ `GlobalInstructionPlugin`) | 框架注入;注意坑:目前**仅根 LlmAgent 生效**(google/adk-python#997) |
| LangGraph | Runtime context(**自动传播给所有子 Agent**)/ Store + `@dynamic_prompt` 中间件 | 每次模型调用前重新拼装 |
| OpenAI Agents SDK | 共享动态指令函数 / 版本化平台 prompt id / `RunConfig.call_model_input_filter` | 运行级注入 |

**骨架一致:约定不写在某个 Agent 的角色定义里,而是挂在「比单个 Agent 高一层的
容器」上,由框架在组装提示词时自动送到每个 Agent 手里。**

### 2.3 没有任何一家能自动识别「提示词里哪段该共享」

CrewAI:写进 backstory 的就是私有的,要共享请写进 crew 级技能。
ADK:写进 instruction 的是本 agent 的,要全局请写进全局指令字段。
OpenAI:要共享就自己抽成公共前缀。

**业界对「规矩混在提示词里」的统一答案是「写错地方了,搬到共享位置去」。
不存在零迁移的标准解法。** 我们要选的不是「迁不迁」,而是共享位置做成什么形态。

### 2.4 抄写不是一定错,错在没有规格

Anthropic 多智能体研究系统的修法**不是取消转述**,而是规定必备字段:
objective / output format / guidance on tools and sources / task boundaries。
原文:"Without detailed task descriptions, agents duplicate work, leave gaps,
or fail to find necessary information."

MAST 论文干预实验:**改进角色规格说明 +9.4% 成功率**;加高层目标验证 +15.6%。
(7 个框架 1600+ 轨迹,失败率 41%–86.7%;disobey task spec 11.8%、
step repetition 15.7%。)

### 2.5 写操作应留主线

Cognition 作者修正后的立场:"multi-agent systems work best today when **writes
stay single-threaded** and the additional agents contribute **intelligence
rather than actions**."

**sop2 现状(worker 出设计方案、主 Agent 渲染落盘)恰好符合这一最佳实践**,
但它是「被迫」(worker 没技能渲染不了)而非「设计」。补上技能后应在文档中
明确:渲染这类写操作继续留主线,技能是为了让 worker 的**设计判断**专业。

## 三、设计原则(拍板)

> 「这会抬高使用者门槛,使用者可能忘记去配。你的 Claude Code 就没有让我去做特殊配置。」
> —— 项目负责人,2026-08-28

1. **平台能力缺口先改默认行为,不设计成让配置者去配。** opt-in 开关有人会忘,
   忘了不报错、只是产出静默跑偏,比统一的坏更糟。
2. **能默认对的不给开关;开关只用于收窄/优化**(省钱、加速),不用于打开基础可用性。
3. **本次不新增任何面向配置者的配置项。** 配置页已在整理中(#1368 七处),
   新增配置项本身即负债。
4. 运维回滚阀(env)不算配置项——配置者不可见,只在事故时由运维使用。

## 四、方案

### 4.1 改动一:worker 继承 manifest 技能(核心)

`synthesize_worker_spec` 不再置空 `skills`,改为继承父 spec 的 `skills`。

**为什么技能是正确的容器**:配置者给 Agent 绑技能,语义就是「这个 Agent 需要
这些本事和规矩」;worker 是它派出去干活的分身。这与 Claude Code
(「子 Agent 加载同一套 MCP 和技能配置」)、CrewAI(crew 级技能全员共享)一致。
不新增字段、不新增配置项。

**成本可控**:技能是渐进披露的。惰性技能(`lazy_load=True`)在提示词里只产生
一行 `<available-skills>` 摘要,正文需 worker 主动调 `skill_view` 才加载。
实测 sop2 那 13 个技能全部惰性:常驻约 1–3K 字符,正文合计 9.7 万字符按需拉取。

**基础设施已就绪**(无需额外接线):`make_worker_build_fn` 调用 `build_agent`
时已传 `skill_resolver` / `skill_store` / `skill_asset_store` /
`skill_activity_recorder`(BUG-19b #1302 的遗留修复)。技能种子文件按
`agent_key = sanitize_agent_key(spec.metadata.name)` 命名空间落盘,worker 的
`metadata.name` 是 `{parent}-worker`,自带独立 key,种子路径与提示词里声明的
`dir=` 同源,自洽。

**连带生效**:`skill_view` 的注册条件(`activated_skill_names` 非空)自然满足,
worker 获得按需查询技能正文的入口。

### 4.2 改动二:继承来的技能走软失败

`_load_skills` 现有语义:manifest 声明的技能遇到**模型不匹配**
(`required_models` 非空且不含本 agent 模型)或**工具名冲突**时 **raise**,
构建失败。理由正当:配置者显式声明了,静默忽略比失败更糟。

但 worker 的技能是**继承来的、配置者没有为 worker 声明过**,而 worker 常跑
不同的模型(`dynamic_workers.model`,如 sop2 用 glm-5.3-flash)。若某技能限定了
父模型,worker 继承它会让**整个委派构建炸掉**——不是少一个技能,是 spawn_worker
整体失败。

**方案**:`_load_skills` 增加 `skills_inherited: bool = False`;为 `True` 时,
模型不匹配 / 工具冲突 → **skip + log**,不 raise(与既有 `evolved` 自动挂载
技能的软失败语义同源)。`build_agent` 透传该参数,worker 构建路径传 `True`。

现状核查:sop2 那 14 个技能版本 `required_models` **全为空**,`tool_names`
**全为空**,不受影响;风险仅存在于其他租户将来配置的技能。

### 4.3 改动三:worker 提示词点明共享工作区

平台生成的 worker 系统提示词增加一句(英文,与现有模板同语言):worker 与派它的
编排者共享一个持久工作区,编排者在任务里提到的路径可直接用文件工具读取。

**治什么**:worker 现在不知道自己与主 Agent 共用工作区。即便主 Agent 在任务里
写了「按 style/PLAN_STYLE.md 办」,worker 也可能因为不确定该文件是否在自己这边
而跳过。这一句让「主 Agent 给路径、worker 自己读」这条路走得通——读到的永远是
最新版锚文件,而非主 Agent 记忆里的版本。

这是用户级偏好(跟人走的工作区文件,如 sop2 的 style 锚)这条腿的通路;
与 4.1 的 Agent 级约定(跟 Agent 走的技能)构成两层,对应 Claude Code 的
用户级 / 项目级 CLAUDE.md 分层。

### 4.4 改动四:spawn_worker 工具描述补规格四要素

工具描述是模型做决策的现场(B-35 层 0/2 已实证有效:两个模型的思考原文都直接
引用了工具描述里的判据)。补入 Anthropic 的四要素要求:每个子任务须写明目标、
输出格式、可用工具与数据源、任务边界;并提示「需遵守工作区既有约定时,用路径
引用而非复制文件内容」。

### 4.5 运维回滚阀

平台 settings `worker_inherit_skills: bool = True`(env,**不进 UI**)。
技能继承是影响所有租户的行为变化,需要不发版即可回滚的手段。为 `False` 时
`synthesize_worker_spec` 保持现状(置空)。

## 五、不做什么(及理由)

| 方案 | 不做的理由 |
|---|---|
| **全量继承父系统提示词** | **非标准**:五个框架无一如此。会造成角色冲突(worker 以为自己是编排者)、稀释注意力(CrewAI 明确警告),靠框定文字消解不可靠 |
| **提示词里标记「共享约定块」** | 违背设计原则 3:要求配置者标记 = 抬高门槛 + 会忘。且技能已提供标准位置 |
| **per-agent 配置「继承哪些技能」** | 同上。默认全继承即正确;若将来有成本诉求,再加**收窄**开关 |
| **平台约定的工作区约定文件(CLAUDE.md 模式)** | 我们工作区是 per-(租户,用户),而 Agent 的规矩应跟 Agent 走。同一 Agent 服务十个员工,规矩不该有十份。用户级偏好已有工作区文件承载(4.3) |
| **worker 继承记忆** | 记忆涉及写入语义,worker 无状态是有理的设计。真要做也应只读继承,优先级低于本批 |
| **worker 继承父的进化技能(auto-attach)** | `_fetch_evolved_skills` 按 `agent_name` 查,worker 名为 `{parent}-worker` 天然查不到。保持最小改动 |

## 六、影响面与风险

- **不绑技能的 Agent**:零变化(继承空列表 = 现状)。
- **绑技能的 Agent**:worker 提示词增加 `<available-skills>` 摘要行(每技能约
  100–200 字符),多一个 `skill_view` 工具;worker 首次沙箱 acquire 时多落一批
  技能种子文件到 `{SANDBOX_SKILLS_ROOT}/<worker agent_key>/`。
- **token**:惰性技能常驻成本约 1–3K 字符/worker;正文按需。相比多智能体本身
  ~15× 的成本量级,增量可接受。
- **注意力**:CrewAI 警告大段注入稀释注意力——这正是只继承摘要、不继承正文的理由。
- **回滚**:env 阀(4.5)。

## 七、验收

**必须用行为探针,不能问 worker「你加载技能了吗」**——Claude Code 社区明确记录过
子 Agent 会编造「我已加载规则」;同 [[description-is-not-the-thing]]。

单测层:
1. `synthesize_worker_spec` 继承父 skills(父 3 个 → worker 3 个);父无技能时为空。
2. 继承技能遇模型不匹配 → 跳过而非 raise;manifest 直接声明路径保持 raise(现状钉住)。
3. worker 构建后 `skill_view` 已注册、提示词含 `<available-skills>` 摘要。
4. worker 提示词含共享工作区说明;`spawn_worker` 描述含四要素。
5. env 阀为 `False` 时 worker spec 的 skills 为空(现状字节级不变)。

真栈层(测试环境,canary 租户探针,**绝不动对接方在用的 agent**):
6. 派一个绑了 pptx 技能的 canary agent,任务需要 PPT 排版判断;检查 worker 子 run
   的事件流中**出现 `skill_view` 调用**(而非 worker 自称用了技能)。
7. 对照组:同 agent 关闭 env 阀,worker 事件流中无 `skill_view`。
