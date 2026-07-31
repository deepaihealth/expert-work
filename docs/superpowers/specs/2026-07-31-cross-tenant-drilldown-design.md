# 跨租户钻取修复 — 设计(2026-07-31)

依据:`docs/research/2026-07-31-cross-tenant-drilldown-audit.md`(全量摸底)。
拍板(2026-07-31):**一期只开读;四波全量连做**。

## 目标

系统管理员在租户切换器选中目标租户后,所有**读**路径(列表→详情→trace→SSE→
记忆/知识库/技能/触发器等)与租户管理员视角一致可用;**写**路径在切入态一律
禁用并在 UI 明示。顺带修掉审计发现的 3 个 C 类问题、3 处平台接口泄漏、9 个
死导出。

## 决策

1. **只开读**:后端只给读端点接 `ensure_tenant_scope`;写端点保持
   `state.tenant_id` 不动(天然 404/错租户不可达,后端本身就是闸)。前端在
   切入态把写操作置灰 + tooltip「切回归属租户操作」。
2. **切入态定义**(前端):`useTenantScope()` 的 scope 为具体 UUID 且 ≠
   identity.homeTenantId。新 hook `useIsTenantSwitched()` 单源判定,所有
   写控件复用。`"*"` 聚合视图不算切入态(聚合页本就只读列表)。
3. **后端统一套路**(照 conversations.py:329 先例):端点加
   `tenant_id: UUID | Literal["*"] | None` query 参数 → `ensure_tenant_scope`
   → `applied_scope`。**详情端点拒绝 `"*"`**(400,单租户语义);SSE 事件流
   同套路(EventSource URL 带 query 参数)。
4. **前端统一套路**:SDK 函数加可选 `tenantScope` 末参 + `withTenantScope`;
   页面从 `useTenantScope()` 透传。SSE/下载类拼 URL 的手动加
   `tenant_id` 参数(照 listThreadRuns 先例)。
5. **写端点不接 scope ≠ 不修 UI**:切入态下写按钮必须可见地禁用,否则用户
   点了拿 404 当 bug 报(本次事故的镜像)。

## 波次与范围

### W1 — 安全洞 + B 类纯前端(1 PR)

**quota.py:127 安全洞**:`POST /v1/quota/release/{reservation_id}` 的
`_tenant_id_from_query_or_principal` 接受任意 `?tenant_id=` 无校验。修法:
非 service 主体时,`tenant_id` 参数必须经 `ensure_tenant_scope` 校验
(allowed_tenants / system_admin),否则 403;service 主体(orchestrator
内部调用)保持现语义。补回归测试:普通租户用户带他租户 tenant_id → 403。

**B 类页面补传**(后端已 scope-aware,SDK 已支持或仅加末参):
- ConversationDetail.tsx:99 `getConversation(threadId)` → 接 `useTenantScope`
  透传;下游 getSessionMessages / listThreadRuns 已带 tenantId(源自
  conversation.tenant_id,修好 getConversation 后自动正确)。
- Users.tsx:66 `listUsers()`、UserProfile.tsx:58 / UserDetail.tsx:301
  `getTenantUser(userId)` → 透传 scope。
- skill-evolution 读端点(eval-results / lineage / kill-switch)后端已
  scope-aware:SDK 加 `tenantScope` 末参 + 页面透传(写端点 approve/reject/
  engage 留给切入态禁用 UI,不透传)。

### W2 — run/agent 详情读面 + 切入态禁用 UI 地基(1-2 PR)

后端接 scope(读):
- runs.py:1093 getRun、1193 trace、1228 trace/raw、**1411 events SSE**
- agents.py:949 getAgent、1057 listRevisions、1091 getRevision
- sessions.py:362 getSession、392/432/468 workspace 读、553 artifacts 下载
- artifacts.py:149 download、328 versions;audit.py:198 getAuditEntry(顺带,
  虽死导出,后端补齐口径)

前端:
- 对应 SDK 函数加 `tenantScope`;TurnCard(getRun/getRunTrace/streamRunEvents/
  downloadArtifact)、RunDetail、ConversationDetail、PlaygroundTab 历史轮
  重建链透传。
- **useIsTenantSwitched hook + 禁用 UI 地基**:AgentDetail 页写操作(编辑
  manifest/禁用启用/回滚/删除)、调试台发送框(streamRun/createSession 是写)、
  重试/审批/反馈按钮切入态置灰。对话页只读天然安全。

验收:系统管理员切入租户 → 智能体详情打开、调试台历史轮可看、对话详情可看、
轮次卡「在 Langfuse 打开」出现且深链正确、发送框置灰。

### W3 — 其余读面(2-3 PR)

后端接 scope(读)+ SDK/页面透传 + 各页写控件切入态禁用:
- knowledge.py:222/245/399/468/502(列表/详情/文档/chunks/检索测试)
- skills.py:1138/1150/1165/1313 + 420(详情/版本/导出/支撑文件读)
- triggers.py:442 getTrigger;webhook_endpoints.py:263 详情
- eval_runs.py:75/117/129;curation.py:225/424
- quality.py:70/85;usage.py:110/168(用户详情页用量)
- workspace.py:79/130/156(用户维度工作区读)
- mcp_servers.py:702/745/805 + 543(列表/available/tools/catalog 读)
- members.py:160 **静默忽略修复**:接 ensure_tenant_scope,具体 tenant_id
  生效(这是 C-2,提前到 W3 因为同文件同套路)

### W4 — C 类杂项 + 清理(1 PR)

- tenant_config.py / tenant_quotas.py:`_ensure_tenant_match` 补
  SYSTEM_TENANT_SWITCH 审计 + `applied_scope` GUC 重绑(口径与 Stream N 统一)。
- 平台泄漏 3 处:McpToolPicker(listPlatformCatalog/listCatalogTools)、
  PlaygroundTab(listRateCards)、CreateAgentModal(getPlatformEmbeddingStatus)
  —— 按调用方角色 gate(isSystemAdmin 才调平台端点,否则走租户可见替代或
  隐藏功能),逐处定。
- 死导出清理:getAuditEntry(W2 后端接了 scope 的话前端接线或删)、
  getEvalDataset、createSkill、addSkillVersion、getSkillVersion、getTrigger
  (W3 接线)、getWebhookEndpoint(W3 接线)、deletePlatformProvider、
  deletePlatformTool——能接线的接线,确认无用的删。

## 测试策略

- **后端每个新接 scope 的端点**:pytest 三件套——①系统管理员带目标租户
  tenant_id 命中目标租户数据;②普通租户用户带他租户 tenant_id → 403
  TENANT_NOT_ALLOWED;③详情端点带 `"*"` → 400。照 conversations 既有
  scope 测试先例。SQL 谓词改动跑本地 integration(DOCKER_HOST 配方)。
- **quota 洞**:回归测试进 W1,红→绿。
- **前端**:vitest——SDK 函数断言 query 含 tenant_id;切入态禁用 UI 断言
  (useIsTenantSwitched mock 两态)。
- **真栈冒烟**(每波发测试环境后):系统管理员切入乐毅大公司走一遍该波
  验收清单。

## 非目标

- 跨租户**写**(二期按需逐个开,含计费/审计/user_id 归属语义设计)。
- 运行期 RLS 启用(独立 parked 项目)。
- listMembers 的 `"*"` 聚合增强、平台技能/模板等 platform 端点改造。

## 风险

- 面广(后端 ~35 读端点 + 前端 ~40 函数/20 页面):套路机械但量大,靠
  SDD 分波 + 每波真栈冒烟兜。
- SSE 接 scope 是新形态(EventSource 无自定义 header,只能 query 参数——
  与现 token 传递方式一致,风险低)。
- 详情端点拒 `"*"` 可能影响聚合视图既有跳转(SkillsList.tsx:415 注释已知
  此坑):聚合列表行点击需带具体租户跳转,W3 一并处理。
