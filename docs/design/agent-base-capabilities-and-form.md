# Agent 基础能力补全 + 能力面表单化 + 文档读取

## Context

面向企业的 agent 平台,当前两个产品缺口:

1. **能力不完整** —— `exec_python`/`bash`/文件读写 全是 opt-in,默认不装。一个新建 agent 退化成「一问一答」,做不了数据处理、文件分析、跑脚本。用户拍板:**这些是底层基础能力,必须默认装、不可关**(没它能力下降 80%)。
2. **能力不可发现** —— 表单只露 7 个开关(name/model/prompt/memory/reflection/vision/web+http+mcp),而 agent spec 有 ~25 个能力面。用户「都不知道有哪些能力可以开」。其中 `dynamic_workers`(自主造临时 worker)**默认开但埋在 YAML**,行为是黑盒。
3. **读不了文档** —— 上传端点仅图片;agent 没有解析 PDF/Word/Excel 的受限路径。

本设计把能力面分三层落地:**Tier 1 默认基础能力(无开关)/ Tier 2 表单可见开关 / Tier 3 高级 YAML**,并补 `read_document` + 文档上传。

**无新 DB schema**(能力集是 build 期决策;Tier 2 的 spec 块已存在于 protocol;上传是 mime allowlist 扩展;`read_document` 是新工具)。最新 migration = `0099`。

---

## 能力面三层分类(已与用户逐项拍板)

### Tier 1 — 默认基础能力(恒装,表单无开关)
| 能力 | 工具 | 说明 |
|---|---|---|
| 代码执行 | `exec_python` | 沙箱内 Python(F.4);**核心能力,不可关** |
| Shell | `bash` | ≡ exec_python 套 `subprocess(shell=True)` 壳;安全面与 exec_python 相同(`bash.py:10-13`),随之恒装 |
| 文件读 | `read_file` / `list_dir` | 工作区只读 |
| 文件写 | `write_file` / `edit_file` | 工作区写 |
| 文档解析 | `read_document` **(新)** | 解析 PDF/Word/Excel/PPT/CSV/MD → 文本,沙箱内跑,无网 |
| 文档单页渲染 | `read_page` **(新,B-64)** | 按需把文档单页渲成 jpeg 供多模态/ask_image 看图;渲染范围见 `document_figures.RENDERABLE_EXTENSIONS`(会随 Task 4b 等后续任务扩展),沙箱内跑 |
| 产物 | `save_artifact` / `list_artifacts` | |
| 主动记忆 | `remember` | |

**安全立场**:沙箱(gVisor + per-tenant 隔离 + egress 透明代理 + 全审计)**就是**安全边界。tool gate 对一个边界已含住的能力是冗余防御。任意代码执行默认开 = 沙箱兑现其设计目的。治理对手盘见 Tier 2 的审批闸(能力不削弱,但可加「执行前人工批」)。

### Tier 2 — 表单可见开关(opt-in / 可见 opt-out)
| 能力 | spec 块 | 表单控件 | picker 端点 |
|---|---|---|---|
| 联网搜索 | `tools[builtin web_search]` | 开关(现有) | — |
| HTTP 调用 | `tools[http]` | 开关(现有) | — |
| MCP 外部工具 | `tools[mcp]` | 开关 + allow/servers(现有) | — |
| 长期记忆 | `memory.long_term` | 开关 + top_k(现有) | — |
| 反思评估模型 | `routing[when=reflection]` | 模型选择(现有) | — |
| 图像理解 | `vision`(VL fallback) | 模型选择(现有 #818) | model catalog |
| **知识库 RAG** | `knowledge.knowledge_base_refs` | **多选** | `GET /v1/knowledge/bases` ✓ |
| **挂技能** | `skills[]` | **多选** | `GET /v1/skills` ✓ |
| **子 Agent 委派** | `subagents[]` | **Form.List**:工具名 + 选 agent + 说明 | `GET /v1/agents` ✓ |
| **执行前审批闸** | `policies.approval_required_tools` | **多选**工具名 | — (从 Tier1/2 工具集生成) |
| **自主造临时 worker** | `dynamic_workers.enabled` | **可见退出开关**(默认开) | — |

### Tier 3 — 高级 YAML(罕见/专家,表单不露)
技能自创工具(`author_skill`/`refine_skill`/`fork_skill`/`propose_skill_to_tenant`)、`note_behavior_patch`/`clarify_tool_usage`、`workflow`(plan_execute)、`code`(代码包)、`cache`、`observability`、`policies` 细调(approval_timeout_s 等)、`defenses`/`sandbox`/`dynamic_context` 细调。

---

## 两个易混能力的澄清(用户问到,记录免后人再绕)

- **`subagents`(静态)**:预先列 `agent_ref`(`name@version`)委派给**已部署**的具名 agent。空 = 不注册 SubAgentTool = 主 agent **不会**委派。固定花名册。
- **`dynamic_workers`(动态)**:给主 agent `spawn_worker` 工具,LLM 运行中**凭生成角色/任务现造**临时 worker,跑完即弃。**`enabled` 默认 True**;生效 = `平台 enable_dynamic_workers ∧ agent.enabled ∧ depth<MAX ∧ worker builder 接线`。安全上限(并发/单 run 上限/迭代/工具白名单)是平台全局,manifest 抬不动。

→ 即「不配 subagents,主 agent 仍会自主造 worker」(平台开关 on 时)。Tier 2 把它做成**可见退出开关**消除黑盒。

---

## M1 — Tier 1 默认基础能力

### 设计决策
- **能力是 runtime 属性,不是 manifest 配置**:在 `tools/assembly.py` build 时,opt-in 工具循环**之后**恒注册一组 `BASE_CAPABILITY_BUILTINS`,与 manifest 已声明的去重(老 manifest 显式列了 exec_python 仍兼容)。manifest `tools` 列表退化为「附加 opt-in 工具」。
- **exec_python/bash 依赖 Sandbox Supervisor client**(`assembly.py:491` 无 client 即 raise)。本平台每 agent 都有沙箱 → client 恒在;若未接线 = 平台配置错误(fail-loud,不静默降级)。
- **read_document 不引入独立解析栈**:复用 office 沙箱镜像里已烤进的 canonical Anthropic 解析库(pdf/docx/xlsx/pptx,见 `skill-runtime-capability.md`)。工具实现 = 给定工作区文件路径 → 在沙箱里跑解析 → 返回文本。低新代码。

### PR1 — 恒装基础能力
- `services/orchestrator/src/orchestrator/tools/assembly.py`:加 `BASE_CAPABILITY_BUILTINS` 常量集;build 末尾恒注册(去重 manifest 已声明项);exec_python/bash 缺 supervisor client 时 fail-loud。
- 单测:空 tools 的 manifest build 后,registry 含 exec_python/bash/read_file/write_file/edit_file/list_dir/save_artifact/list_artifacts/remember;显式列 exec_python 的老 manifest 不重复注册。

### PR2 — read_document 工具
- `services/orchestrator/src/orchestrator/tools/read_document.py`(新):`ReadDocumentTool`,入参工作区相对路径,沙箱内解析 → 文本(分页/分 sheet 上限,防超大文档撑爆上下文)。
- 注册进 `BASE_CAPABILITY_BUILTINS`。
- 解析输出经 spotlight `untrusted_content` 通道入上下文(复用现有内联注入防御,文档是 untrusted 源)。
- 单测:各 mime 解析 round-trip;超限截断;损坏文件 → 干净 ToolResult error(不抛)。

---

## M2 — Tier 2 表单四新段 + dynamic_workers 开关

> 涉及前端 `form_model.ts` reader/writer + `FormView.tsx` 新段 + i18n 三同步;backend 全有端点,零后端改(approval_required_tools/dynamic_workers/knowledge/skills/subagents 都是已存在的 spec 字段)。

### form_model.ts 新 reader/writer(immutable,保留 siblings,沿用现有范式)
- `readKnowledgeRefs` / `setKnowledgeRefs`(`knowledge.knowledge_base_refs`;空数组 = 删 knowledge 块)
- `readSkills` / `setSkills`(`spec.skills`)
- `readSubagents` / `setSubagents`(`spec.subagents` 行数组)
- `readApprovalTools` / `setApprovalTools`(`policies.approval_required_tools`)
- `readDynamicWorkersOn` / `setDynamicWorkersOn`(`dynamic_workers.enabled`;**默认视 true** —— 块缺省即开,关 = 显式写 `{enabled:false}`)

### FormView.tsx 新段(testid)
- `af-knowledge` —— 多选 tenant 知识库(load `GET /v1/knowledge/bases`)
- `af-skills` —— 多选 tenant 技能(load `GET /v1/skills`)
- `af-subagents` —— Form.List:每行 `工具名(snake_case 校验) + 选已部署 agent(load /v1/agents,拼 name@version) + 说明`
- `af-approval` —— 多选「执行前需人工审批」,选项 = 当前 agent 实际具备的可门控工具(exec_python/bash/http/write_file/edit_file/mcp/web_search…)
- `af-dynamic-workers` —— 单 Switch「允许运行中自主创建临时 worker(默认开)」

> 全部放「高级」`af-advanced` Collapse 内(承接现有折叠范式),避免新建表单又变长。基础段(name/model/prompt)仍在外层。

### i18n / stories / e2e
- en interface(`TranslationKeys`)+ en values + zh-CN values 三同步;`agent_form.*` 加段标题 + 字段 + FieldHelp 文案。
- 每段加 FieldHelp(? 图标,含义 + 示例)。
- 表单元素 aria-label(防 axe critical)。
- stories 补 mock;`tsc -b --noEmit` 权威;e2e 展开 af-advanced 再触达新段(承接现有 Collapse e2e 修法)。

---

## M3 — 文档上传(扩展现有端点)

### 设计决策(核完优化)
- **上传不解析**:`POST /v1/sessions/{thread_id}/uploads` 当前图片专用(校验 mime → strip EXIF → 落存 → 返 `Expert Work://image/...`)。扩展为**也接文档 mime**:校验 + 落原件进工作区 + 返 `Expert Work://file/...` ref。**不在 control-plane 解析** —— 把恶意 doc 攻击面挡在控制面外。
- **解析在 agent 侧按需**:agent 用 Tier 1 的 `read_document`(沙箱内)解析。单一解析路径 = 沙箱。
- 图片路径(strip EXIF + VL)不变。

### PR1
- `services/control-plane/src/control_plane/api/uploads.py`:mime allowlist 加文档类(`application/pdf`、`...wordprocessingml.document`、`...spreadsheetml.sheet`、`...presentationml.presentation`、`text/plain`、`text/markdown`、`text/csv`);分流 —— 图片走现有 sanitize,文档走「校验 + 落工作区」直存。
- **大小上限**(文档独立阈值,大于图片);**zip-bomb 防护**:docx/xlsx/pptx 是 zip → 落存前查解压比/解压后总大小上限(拒超限,不解压内容只查 central directory 大小)。
- 返回结构区分 `kind: image|file` + ref。
- `runs.py`(~90 引用 uploads)对账 ref 形态。
- 单测:文档 mime 通过、落工作区、ref 正确;超大/zip-bomb 拒;未知 mime 拒;图片路径不回归。

---

## 安全分析

| 面 | 处理 |
|---|---|
| 任意代码执行(exec_python/bash 默认开) | 沙箱 gVisor + per-tenant 隔离 + egress 代理 + 审计 = 边界。治理对手盘 = approval_required_tools 可门控。 |
| 恶意文档(PDF/docx 漏洞、zip-bomb) | 上传不解析(控制面零解析面);解析在沙箱 read_document;上传查大小 + 解压比上限。 |
| 文档内容注入(提取文本里藏指令) | read_document 输出走 spotlight `untrusted_content` 通道(同源 fence),复用现有内联注入防御。 |
| 审批闸绕过 | `approval_required_tools` 是平台强制 LangGraph `interrupt()`,agent 绕不过(spec 已实现)。 |
| 自主 worker 资源耗尽 | 并发/单run/迭代/工具白名单平台全局,manifest 抬不动(spec 已实现)。 |
| 跨租户子 agent 委派 | `agent_ref` 限同租户(`SubAgentSpec` 校验已实现);picker 只列本租户 agent。 |

---

## 验证

- **M1**:pytest —— 空 manifest build 后基础工具全在 + 老 manifest 不重复 + 无 supervisor client fail-loud;read_document 各 mime round-trip + 超限截断 + 损坏文件干净 error。手动真栈:新建零配置 agent → 让它跑 `exec_python` 算数 + `read_document` 读个 PDF。
- **M2**:vitest form_model reader/writer + FormView 新段渲染/读写;`tsc -b --noEmit`;e2e 展开高级段操作四新控件;axe 无 critical。
- **M3**:pytest 文档上传通过/拒超大/拒 zip-bomb/图片不回归;手动真栈:上传 docx → 工作区有原件 → agent read_document 读出文本。

---

## 实现注意(引自 memory)

- 分支先行 `git checkout -b agent/<子项>`;设计/代码分 commit;footer `Co-authored-by: leyi`,不加 Claude 署名。
- 每 PR 同步 `docs/ITERATION-PLAN.md`(checkbox + PR 号)。
- push 前 `pre-commit run --all-files`(ruff UP038 / RUF002 中文标点 / EN DASH→hyphen)。
- CodeQL:别 log 工具名/文件名(clear-text-logging 启发式);assert 不放副作用。
- admin-ui:antd 表单元素 aria-label;i18n 三同步 `tsc -b --noEmit` 权威;envelope-vs-raw 写 SDK 前 grep 核外层。
- 无新 migration。
