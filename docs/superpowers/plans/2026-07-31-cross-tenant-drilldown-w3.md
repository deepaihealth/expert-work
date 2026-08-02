# 跨租户钻取修复 W3 实施计划(2026-07-31)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。
> 两波三 track:第一波 D(后端)∥ E(前端读透传)并行 worktree;第二波 F
> (写置灰+横切杂项)在 E 合并后串行。依据:spec
> `docs/superpowers/specs/2026-07-31-cross-tenant-drilldown-design.md` +
> 2026-07-31 现状摸底(本文行号以 main fda9900a 为准)。

**Goal:** 系统管理员切入租户后,其余全部读面(知识库/技能/触发器/webhook/
eval/curation/quality/usage/工作区/MCP/成员)与租户管理员一致可用;全站写
控件切入态置灰;members 静默忽略修复;聚合视图行跳转带具体租户。

**Architecture:** 照 W2 套路——后端详情端点 `ensure_single_tenant_scope`
(agents.py:963 先例)、列表端点 `ensure_tenant_scope`(skills.py:942 先例)
→ `applied_scope`;前端 SDK 可选 `tenantScope` 末参 + `withTenantScope`,
页面 `useTenantScope()` 直取;**详情调用包 `concreteTenantScope`,列表裸传**
(W2 定死的口径);写控件 `useIsTenantSwitched` + Tooltip 置灰。

**Tech Stack:** FastAPI + pytest;React + TS + vitest。

## Global Constraints

- 一期只开读:写端点后端一律不接 scope
- 后端测试三件套(每个新接 scope 端点):①system_admin 带目标租户
  tenant_id 命中 ②他租户用户带 tenant_id → 403 TENANT_NOT_ALLOWED
  ③详情端点带 `tenant_id=*` → 400 SCOPE_ALL_NOT_SUPPORTED。照
  tests/ 里 W2 新增的 scope 测试先例(如 test_runs_api.py)
- 前端:详情包 `concreteTenantScope(apiTenantScope)`,列表裸传
  `apiTenantScope`;组件无 Provider 的测试补 vi.mock(importOriginal 展开,
  `scope: ref ?? "home"` + setScope + apiTenantScope)
- 验证退出码:`cmd > f 2>&1; ec=$?`,不许 `cmd | tail` 后看 `$?`
- 改组件后跑**全套** vitest(教训:AgentDetailTabs.test.tsx 不按组件名放)
- CI 口径:mypy 含 tests、ruff 全库;commit `<type>: <描述>` 无 attribution

---

## Track D(PR-D)— 后端:其余读端点接 scope + members 修复

分支 `feat/ct-w3-backend-read-scope`,worktree `.worktrees/ct-w3-be`。
两 task 串行(同分支)。所有行号 main fda9900a。

### Task D1:knowledge/skills/triggers/webhooks 组

**Files:** `services/control-plane/src/control_plane/api/` 下
`knowledge.py`、`skills.py`、`triggers.py`、`webhook_endpoints.py`;
Test:对应既有 test 文件(没有 scope 测试节的新增)。

端点清单(全部现为 `request.state.tenant_id` 硬绑):

| 文件:行 | 函数 | 套路 |
|---|---|---|
| knowledge.py:223 | list_bases | ensure_tenant_scope(列表) |
| knowledge.py:246 | get_base | ensure_single_tenant_scope(详情) |
| knowledge.py:400 | list_documents | single(从属详情,拒 "*") |
| knowledge.py:469 | list_chunks | single |
| knowledge.py:503 | test_retrieval(POST,读性) | single |
| skills.py:1139 | get_skill | single |
| skills.py:1151 | list_versions | single |
| skills.py:1166 | get_version | single |
| skills.py:1314 | export_version | single |
| skills.py:424(装饰器 :420) | get_supporting_file | single |
| triggers.py:443 | get_trigger | single |
| webhook_endpoints.py:264 | get_endpoint | single |

另:**skills.py `_skill_dict`(:211-244)响应加 `tenant_id` 字段**
(str(record.tenant_id);列表/详情同源)——E 的聚合行跳转依赖它。
knowledge.py 整文件零 scope import,import 块照 agents.py 头部抄
(ensure_tenant_scope/ensure_single_tenant_scope/applied_scope/
cross_tenant_query_enabled/current_trace_id_hex)。

每端点改法(照 agents.py:961-975 逐行):加
`tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None` →
详情 `scope = await ensure_single_tenant_scope(principal, tenant_id, audit,
trace_id=..., endpoint="GET /v1/...", cross_tenant_enabled=...)` →
`async with applied_scope(scope):` 内原 store 调用换 `scope.tenant_id`。
列表用 ensure_tenant_scope + CrossTenant 分支(照 skills.py:942 list_skills)。

测试三件套逐端点;get_supporting_file/export 这类二进制响应断言 200/403/400
状态码即可。commit 粒度:每文件一 commit。

### Task D2:eval/curation/quality/usage/workspace/mcp/members 组

**Files:** `eval_runs.py`、`curation.py`、`quality.py`、`usage.py`、
`workspace.py`、`mcp_servers.py`、`members.py`;Test:对应既有文件。

| 文件:行 | 函数 | 套路 | 特殊注意 |
|---|---|---|---|
| eval_runs.py:76 | list_runs | ensure_tenant_scope | 删「home-tenant only」旧注释 |
| eval_runs.py:118 | get_run | single | |
| eval_runs.py:130 | list_cases | single(从属) | |
| curation.py:226 | get_candidate | single | |
| curation.py:425 | get_eval_dataset | single | |
| quality.py:71 | list_scores | ensure_tenant_scope | |
| quality.py:86 | list_drift_alerts | ensure_tenant_scope | |
| usage.py:111 | usage_cost | ensure_tenant_scope | **现走 principal.tenant_id**,改 scope.tenant_id |
| usage.py:169 | usage_tokens | 同上 | 同上 |
| workspace.py:80 | get_workspace | single | user_id 维度叠加:resolve_caller_user_id 留在 applied_scope 外(caller-identity 惯例) |
| workspace.py:131 | list_workspace_files | single | 同上 |
| workspace.py:157 | download_workspace_file | single | 同上 |
| mcp_servers.py:703 | list_mcp_servers | ensure_tenant_scope | principal.tenant_id → scope |
| mcp_servers.py:746 | list_available_mcp_servers | ensure_tenant_scope | |
| mcp_servers.py:806 | list_mcp_server_tools | single | |
| mcp_servers.py:544 | list_catalog | **不动** | catalog 平台级 NULL-tenant+bypass_rls_session,W4 泄漏项一并定 |
| members.py:161 | list_members | **重写 scope 分支** | 见下 |

**members.py C-2 修复**:现 :167 `tenant_id: str | None` 手写分支只认
`"*"`,具体 UUID 静默丢弃回落 principal.tenant_id。改成标准
`UUID | Literal["*"] | None` + ensure_tenant_scope(具体 UUID 走
SingleTenant 生效、"*" 走 CrossTenant 保持 list_all_tenants 行为)。
回归测试:①system_admin 带具体他租户 UUID → 返回该租户成员(修复断言,
先红后绿)②普通用户带他租户 → 403 ③"*" 聚合行为不回归。

usage/mcp 的 principal.tenant_id 路径改造后跑相关既有测试,确认
applied_scope 包住 store 调用(它们原先绕过 request.state)。

---

## Track E(PR-E)— 前端:读面 SDK+页面透传 + 聚合行跳转

分支 `feat/ct-w3-frontend-read-scope`,worktree `.worktrees/ct-w3-fe`。
与 D 独立可合(先合无害,W2 已证)。三 task 串行。

### Task E1:SDK 层加 tenantScope

**Files:** `apps/admin-ui/src/api/`:`knowledge.ts`(listBases:144/
getBase:149/listDocuments:210/listChunks:235/testRetrieval:249)、
`skills.ts`(getSkill:174/listSkillVersions:248/getSkillVersion:255/
exportSkillVersion:288/getSupportingFile:314;SkillRecord 类型加可选
`tenant_id?: string`)、`skillApi.ts`(facade 接口 :51-88 加 scope 形参,
tenantSkillApi:93/platformSkillApi:109 两实现——platform 侧忽略参数)、
`triggers.ts`(getTrigger:69)、`webhooks.ts`(getWebhookEndpoint:81)、
`eval_runs.ts`(×3)、`curation.ts`(getCandidate:86/getEvalDataset:180)、
`quality.ts`(×2)、`usage.ts`(×2)、`workspace.ts`(×3)、
`mcp-servers.ts`(×3)、`members.ts`(listMembers:68 加
`tenantScope?: TenantScope`,保留 crossTenant 兼容或合并语义——
crossTenant:true ≡ tenantScope:"*",实现里归一)。
Test:`api/__tests__/` 既有 SDK 测试文件补 query 断言(照
tenant_scope_passthrough.test.ts 先例)。

全部可选末参/params 键 + `withTenantScope`,不破既有调用。

### Task E2:页面/组件透传

**Files(调用点全清单,逐个透传;详情包 concreteTenantScope,列表裸传):**
- knowledge:`pages/KnowledgeAdmin.tsx:50`、
  `components/manifest-editor/KnowledgePicker.tsx:34`、
  `pages/KnowledgeDetail.tsx:48`(detail 包 helper)、
  `pages/knowledge_detail/DocumentsTab.tsx:61`、
  `SegmentPreviewDrawer.tsx:48`、`RetrievalTestTab.tsx:69`(均包 helper)
- skills:`pages/SkillDetail.tsx:147/148/277`、
  `pages/skill_detail/FileEditor.tsx:153`、`RenameDeleteModals.tsx:67`
  (全包 helper;经 skillApi facade 传)、
  `components/manifest-editor/SkillPicker.tsx:90`(listSkills 补裸传)
- eval:`pages/EvalRunsList.tsx:89`(裸)、`pages/EvalRunDetail.tsx:79,80`(包)
- curation:`pages/curation/CandidatesPanel.tsx:109` getCandidate(包)
- quality:`pages/SettingsQuality.tsx:158,159`(裸)
- usage:`pages/SettingsUsage.tsx:114,115`(裸)、`pages/UserDetail.tsx:239`、
  `pages/user_profile/UsagePane.tsx:15`(包——user 详情页单租户语义)
- workspace:`pages/user_profile/WorkspacePane.tsx:62,63,112`(包)、
  `pages/agent_detail/PlaygroundTab.tsx:658,659,674`(包)
- mcp:`pages/SettingsMcpServers.tsx:87,88,119`(裸/tools 包)、
  `components/manifest-editor/widgets/McpToolPicker.tsx:100,144`
- members:`pages/SettingsMembers.tsx:112`(裸传 apiTenantScope,替代/归一
  现有 crossTenant 拼法)
- user_profile 补漏(SDK+后端已全支持,纯页面漏传,均包 helper):
  `pages/user_profile/ConversationsPane.tsx:73` listConversations、
  `pages/user_profile/MemoryPane.tsx:61` listMemories

Test:每域至少一条透传断言(mock 模板照 AgentDetailTabs.test.tsx 的
importOriginal 展开写法);既有精确参数断言补 undefined 末参。

### Task E3:SkillsList 聚合行跳转带具体租户

**Files:** `pages/SkillsList.tsx`(:414-421 onRow)、`pages/SkillDetail.tsx`
(读取入口 scope);Test:`SkillsList.test.tsx` 补断言。

现病:`scope==="*"` 下点他租户行 → `/skills/{id}` → getSkill 无 scope →
home 租户 RLS 404。修法:
1. 行跳转:`navigate(\`/skills/\${record.id}?tenant_id=\${record.tenant_id}\`)`
   ——仅当 `record.tenant_id` 存在且 ≠ 归属(D1 加的字段;字段缺失=老后端,
   降级现行为)。
2. `SkillDetail` 用 `useSearchParams` 读 `tenant_id`,优先级:query 参数 >
   `concreteTenantScope(apiTenantScope)`;传给 skillApi 全部读调用。
3. platform 行保持现状(:414 注释挡的是另一件事,不动)。

---

## Track F(PR-F)— 写置灰全站 + 横切杂项(E 合并后启动)

分支 `feat/ct-w3-switched-readonly`,基于合完 E 的 main。三 task 串行。

### Task F1:写控件切入态置灰(19 页面)

**Files:** 摸底 §4 全清单——KnowledgeAdmin(删库/新建)、KnowledgeDetail
(重建索引/编辑设置)、DocumentsTab(上传/重入库/删除)、SkillsList
(导入 ZIP ×2 处)、SkillDetail(改分类/改状态/置顶)、FileEditor(保存 ×2)、
AddFileModal(新增)、RenameDeleteModals(重命名/删除)、TriggersList
(启停/删除/创建)、WebhooksList(启停/删除/创建)、EvalRunsList(入队)、
CandidatesPanel(驳回/提升)、EvalDatasetsPanel(创建/编辑/删除)、
SettingsMembers(邀请/重发/吊销/重置密码/清除)、SettingsMcpServers
(启停/删除/停用/编辑/新建)、CreateMcpServerDrawer、AddMcpServerDrawer、
WorkspacePane(删工件/删文件)、MemoryPane(updateMemory :83/
deleteMemory :98)、PlaygroundTab(上传图片/文档 :312/:313、删文件 :701、
删工件 :716——建会话已在 W2 置灰)。

统一姿势:`const isTenantSwitched = useIsTenantSwitched();` +
`disabled={... || isTenantSwitched}` + antd Tooltip(i18n 键
`common.tenant_switched_readonly` 已存在)。**Tooltip 包函数组件 child 必套
裸 div 承接事件**(ApprovalGate 教训)。Switch/表格行内按钮同样处理;
Modal/Drawer 内表单以入口按钮置灰为准(入口灰了不用逐字段灰)。
Test:每页面挑 1-2 个代表控件两态断言(mock useIsTenantSwitched)。

### Task F2:Provider 降级 effect + FeedbackBar Tooltip + AgentsList

**Files:**
- `src/tenant/TenantScopeContext.tsx`:降级 effect 现只处理部分情形,扩成
  「identity confirmed 非 system_admin && scope 非 home(任意残留具体
  UUID)→ setScope(home)」——防降权后残留 UUID 永久锁死写控件。
  Test:两态(残留 UUID+非管理员→回 home;管理员保持)。
- `components/turn/TurnCard.tsx`(FeedbackBar 的 Tooltip 包裹处):套裸
  div 承接事件(同 ApprovalGate :78 修法);断言 Tooltip 真出现(userEvent
  hover)。
- `pages/AgentsList.tsx` loadDisabledState:带 scope(cosmetic,照页面
  既有 apiTenantScope 用法)。

### Task F3:测试 fixture 下沉

**Files:** Create `apps/admin-ui/src/test-utils/tenantScopeMock.ts`(导出
`mockTenantScopeModule(ref)` 工厂:importOriginal 展开 + useTenantScope
返回 `{scope: ref.current ?? "home", setScope: noop, apiTenantScope:
ref.current}`);把 W2/W3 各测试文件重复的 vi.mock 样板换成共享工厂
(vi.mock 的 hoisting 限制:工厂内不能引模块级变量,用 vi.hoisted 配合——
实现时以真 vitest 跑通为准,跑不通就保留逐文件样板并在 util 里只共享
返回值构造器)。改完跑**全套** vitest。

---

## 冲突分析

- D:纯 services/control-plane,与 E/F 零交叉。
- E:api/*.ts + 读调用页面。F:同一批页面的**写控件区域** + context/
  TurnCard/AgentsList/test-utils。E 先合、F 后启动串行,无并行冲突。
- E 内部:SkillDetail 同时被 E2(透传)E3(query 参数)动——同 track
  串行 task,无冲突。

## 合并序

D、E 并行开发各自 CI 绿后即可合(顺序无关);F 在 E 合并后启动。
全合后发测试环境,真栈验收:切入乐毅大公司 → 知识库列表+详情开、
技能列表+详情开、触发器/webhook/eval/curation/quality/usage/成员各页
数据一致、"*" 聚合下点技能行进详情不 404、全站写按钮置灰。
