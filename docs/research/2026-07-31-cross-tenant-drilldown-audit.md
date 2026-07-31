# 系统管理员跨租户钻取 — 全量摸底审计(2026-07-31)

## 背景

测试环境实测:系统管理员在租户切换器选中目标租户后,

1. 智能体**列表**正常(`GET /v1/agents?tenant_id=<uuid>` 带参),点进**详情** 404
   (`GET /v1/agents/{name}/{version}` 不带参,后端按归属租户查——系统管理员归属
   平台租户,查不到);
2. 对话**详情**同样 404(`GET /v1/conversations/{thread_id}` 前端没传
   `tenant_id`——这条后端其实支持,纯前端漏)。

结论:**跨租户钻取只做了列表层,详情/操作层系统性缺失**。本审计对前后端全量
盘点,给出缺口分类与修复分期建议。修复动工前先由本文档收口范围。

## 机制(Stream N 现状)

- 后端**无全局中间件**。scope-aware 端点各自接 `?tenant_id=` 查询参数并调
  `ensure_tenant_scope()`(control_plane/tenant_scope.py)+ `applied_scope()`
  (RLS GUC 重绑,防御纵深)。`"*"` = 跨租户聚合(system_admin only),具体
  UUID = 切入单租户(system_admin 或 allowed_tenants 命中),都出审计行
  (SYSTEM_CROSS_TENANT_QUERY / SYSTEM_TENANT_SWITCH)。
- 未接入的端点读 `request.state.tenant_id`(= 调用者归属租户),切换器对它们
  完全不可见。
- 前端 SDK 通过 `withTenantScope(params, tenantScope)`(api/client.ts)加
  `tenant_id` 参数;页面从 `useTenantScope()` 取 scope 并逐调用透传。

## 盘点结果(数字)

### 后端(services/control-plane/src/control_plane/api/,250 端点)

| 类别 | 数量 | 说明 |
|---|---|---|
| `ensure_tenant_scope`(scope-aware) | 33(13%) | 几乎全是 list 端点 + skill_evolution 全家 + agent_users 全家 + conversations 两条 + sessions/messages/runs 列表 |
| `state.tenant_id`(归属租户硬绑) | 127(51%) | **缺口主体**:详情/CRUD/SSE/trace 全在这里 |
| other(平台端点/健康/引导等) | 90(36%) | platform_* 全家 system_admin+bypass_rls,多数按设计正确;个别异常见「安全发现」 |

**整文件全瞎**(每个端点都归属租户硬绑):eval_runs(4)、feedback(1)、
knowledge(12)、mcp_oauth_api(4)、plan(2)、quality(2)、uploads(2)、
usage(2)、workspace(4);另 mcp_servers 9/10、members 5/6 实质同类。

### 前端(apps/admin-ui/src/api/,223 个 HTTP SDK 函数)

| 类别 | 数量 |
|---|---|
| 正确带 `tenantScope`(withTenantScope) | 20(全是 list + user 三件套) |
| 手动带 tenantId(getSessionMessages / listThreadRuns) | 2 |
| 部分支持(listMembers 只认 `"*"`,不能切入单租户) | 1 |
| **完全不带** | **200**(其中约 128 个挂在租户业务页面上) |

**第二类病——SDK 支持但页面没传**:`getConversation` SDK 有 scope 参数,
ConversationDetail.tsx:99 没传(实测 404 即此);Users.tsx:66、
UserProfile.tsx:58、UserDetail.tsx:301 同样没接 `useTenantScope`。全库只有
20 个非测试文件消费 `useTenantScope`,几乎全是列表页。

**死导出 9 个**(无非测试调用方):getAuditEntry、getEvalDataset、createSkill、
addSkillVersion、getSkillVersion、getTrigger、getWebhookEndpoint、
deletePlatformProvider、deletePlatformTool(另 createPlatformSkill /
addPlatformSkillVersion 仅经 facade 使用需复核)。

**平台接口泄漏进业务页 3 处**:listPlatformCatalog / listCatalogTools(manifest
编辑器 McpToolPicker)、listRateCards(调试台成本估算)、
getPlatformEmbeddingStatus(CreateAgentModal)——租户管理员命中会 403/空,
属另一类 bug,一并记录。

## 缺口分类

- **Class A(前后端都要动)**:后端 `state.tenant_id` 硬绑 + 前端不带参。
  = 127 个后端端点 × 对应 SDK/页面。钻取详情面全在此:agent 详情全家
  (get/update/revisions/rollback/disable/enable)、run 详情链
  (getRun/trace/trace raw/**events SSE**)、session 工作区/工件、knowledge
  全家、skills 详情全家、triggers 详情 CRUD、webhook 详情、eval_runs、
  curation 详情、memory 三个写、artifacts 详情操作、plan、quality、usage、
  uploads、mcp_servers、members(具体租户)。
- **Class B(纯前端补传)**:后端已 scope-aware,前端漏。对话详情
  (conversations.py:329 ✓)、getSessionMessages / listThreadRuns(后端 ✓、
  SDK ✓、页面漏)、user 三件套页面漏。**成本最低、见效最快**。
- **Class C(安全发现,独立修)**:
  1. **quota.py:127 `POST /v1/quota/release/{id}`**:`_tenant_id_from_query_or_principal`
     接受任意 `?tenant_id=`,**无 allowed_tenants / ensure_tenant_scope 校验**
     ——任何认证主体可指定他租户释放配额预留。安全洞,优先修。
  2. members.py:160 `GET /v1/members` 传具体 `tenant_id` 被**静默忽略**返回
     归属租户成员——切入租户的系统管理员看到的是错误数据(比 404 更糟,静默
     错数据)。
  3. tenant_config.py / tenant_quotas.py 跨租户走 `_ensure_tenant_match`:无
     SYSTEM_TENANT_SWITCH 审计、无 applied_scope GUC 重绑(RLS 防御纵深缺口;
     现运行期 RLS 本就空转——见 rls-inert 记忆——但补齐口径应统一)。

## 产品边界题(修复前必须拍板)

系统管理员切入租户后,**开放到哪一层**?

1. **读**(看列表/详情/trace/记忆/知识库/用量):应全开——排障刚需,本审计
   触发场景即是。
2. **写**(改 manifest、删知识库、发调试台 run、审批、改记忆):跨租户写有
   计费/配额/审计/user_id 归属语义问题(调试台 run 记谁的账?)。建议一期
   **只开读**,写操作在切入态禁用 + UI 明示(按钮置灰/提示"切回归属租户
   操作"),后续按需逐个开。
3. 平台端点泄漏 3 处、死导出 9 个:顺带清理。

## 修复分期建议(拍板后细化)

| 波次 | 内容 | 规模 |
|---|---|---|
| W1 | Class C-1 quota 洞 + Class B 全量(对话详情链/用户页传参)——纯小改,先通「对话页跨租户钻取+Langfuse 深链」 | 1 PR |
| W2 | Class A 读面第一批:run 详情链(getRun/trace/raw/events SSE)+ agent 详情读(get/revisions)+ 前端透传 + 写操作切入态禁用 UI | 1-2 PR |
| W3 | Class A 读面其余:knowledge/skills/triggers/webhook/eval/curation/memory/workspace/usage/quality/plan | 2-3 PR |
| W4 | Class C-2/3(members 静默忽略、tenant_config 审计口径)+ 平台泄漏 3 处 + 死导出清理 | 1 PR |

后端套路统一:端点加 `tenant_id: UUID | Literal["*"] | None` 参数 →
`ensure_tenant_scope` → `applied_scope`(照 conversations.py:329 先例);详情
端点通常禁 `"*"`(单租户语义)。前端套路:SDK 函数加可选 `tenantScope` 末参 +
`withTenantScope`;页面从 `useTenantScope()` 透传(照 ConversationsList 先例)。

## 附录 A — 后端全量表

(250 端点逐行,scope 分类见上;来源:2026-07-31 全量扫描)

| File:Line | Method Path | Scope handling | R/W |
|---|---|---|---|
| api/agent_schema.py:22 | GET /v1/agents/schema | other — 静态 schema,无租户 | R |
| api/agent_templates.py:164 | POST /v1/platform/agent-templates | other — system_admin+bypass_rls | W |
| api/agent_templates.py:189 | GET /v1/platform/agent-templates | other — system_admin+bypass_rls | R |
| api/agent_templates.py:201 | GET /v1/platform/agent-templates/{name}/{version} | other — system_admin+bypass_rls | R |
| api/agent_templates.py:218 | PUT /v1/platform/agent-templates/{name}/{version} | other — system_admin+bypass_rls | W |
| api/agent_templates.py:251 | PATCH /v1/platform/agent-templates/{name}/{version} | other — system_admin+bypass_rls | W |
| api/agent_templates.py:274 | DELETE /v1/platform/agent-templates/{name}/{version} | other — system_admin+bypass_rls | W |
| api/agent_users.py:174 | GET /v1/agents/{agent_name}/{agent_version}/users | ensure_tenant_scope | R |
| api/agent_users.py:332 | GET /v1/users | ensure_tenant_scope | R |
| api/agent_users.py:441 | GET /v1/users/{user_id} | ensure_tenant_scope | R |
| api/agent_users.py:504 | POST /v1/users/{user_id}:purge | ensure_tenant_scope | W |
| api/agents.py:458 | POST /v1/agents | state.tenant_id | W |
| api/agents.py:579 | GET /v1/agents/templates | state.tenant_id(+bypass_rls 读平台行) | R |
| api/agents.py:624 | POST /v1/agents/fork | state.tenant_id | W |
| api/agents.py:734 | POST /v1/agents/{agent_code}/sessions | state.tenant_id | W |
| api/agents.py:799 | POST /v1/agents/{agent_code}/runs | state.tenant_id | W |
| api/agents.py:894 | GET /v1/agents | ensure_tenant_scope(支持 `*`) | R |
| api/agents.py:949 | GET /v1/agents/{name}/{version} | state.tenant_id | R |
| api/agents.py:990 | PUT /v1/agents/{name}/{version} | state.tenant_id | W |
| api/agents.py:1057 | GET /v1/agents/{name}/{version}/revisions | state.tenant_id | R |
| api/agents.py:1091 | GET /v1/agents/{name}/{version}/revisions/{revision} | state.tenant_id | R |
| api/agents.py:1110 | POST /v1/agents/{name}/{version}/revisions/{revision}/rollback | state.tenant_id | W |
| api/agents.py:1172 | DELETE /v1/agents/{name}/{version} | state.tenant_id | W |
| api/agents.py:1267 | POST /v1/agents/{name}/disable | state.tenant_id | W |
| api/agents.py:1351 | POST /v1/agents/{name}/enable | state.tenant_id | W |
| api/api_keys.py:82 | POST /v1/service_accounts/{id}/api_keys | state.tenant_id | W |
| api/api_keys.py:146 | GET /v1/service_accounts/{id}/api_keys | state.tenant_id | R |
| api/api_keys.py:161 | GET /v1/api_keys | ensure_tenant_scope(支持 `*`) | R |
| api/api_keys.py:201 | DELETE /v1/api_keys/{api_key_id} | state.tenant_id | W |
| api/api_keys.py:221 | POST /v1/api_keys/{api_key_id}/rotate | state.tenant_id | W |
| api/approvals.py:102 | GET /v1/approvals | ensure_tenant_scope | R |
| api/approvals.py:143 | POST /v1/approvals:decide | state.tenant_id | W |
| api/artifacts.py:91 | GET /v1/artifacts | ensure_tenant_scope | R |
| api/artifacts.py:149 | GET /v1/artifacts/download | state.tenant_id | R |
| api/artifacts.py:234 | DELETE /v1/artifacts/{name:path} | state.tenant_id | W |
| api/artifacts.py:275 | PATCH /v1/artifacts/{name:path} | state.tenant_id | W |
| api/artifacts.py:328 | GET /v1/artifacts/{name:path}/versions | state.tenant_id | R |
| api/audit.py:110 | GET /v1/audit | ensure_tenant_scope(支持 `*`) | R |
| api/audit.py:198 | GET /v1/audit/{audit_id} | state.tenant_id | R |
| api/billing_admin.py:87 | GET /v1/admin/billing/chargeback | other — system_admin+bypass_rls,`?tenant_id` 仅后置过滤 | R |
| api/conversations.py:149 | GET /v1/conversations | ensure_tenant_scope(支持 `*`) | R |
| api/conversations.py:329 | GET /v1/conversations/{thread_id} | ensure_tenant_scope | R |
| api/curation.py:187 | GET /v1/curation/candidates | ensure_tenant_scope | R |
| api/curation.py:225 | GET /v1/curation/candidates/{id} | state.tenant_id | R |
| api/curation.py:247 | POST /v1/curation/candidates/{id}/promote | state.tenant_id | W |
| api/curation.py:308 | POST /v1/curation/candidates/{id}/dismiss | state.tenant_id | W |
| api/curation.py:345 | POST /v1/eval-datasets | state.tenant_id | W |
| api/curation.py:388 | GET /v1/eval-datasets | ensure_tenant_scope | R |
| api/curation.py:424 | GET /v1/eval-datasets/{dataset_id} | state.tenant_id | R |
| api/curation.py:436 | PATCH /v1/eval-datasets/{dataset_id} | state.tenant_id | W |
| api/curation.py:469 | DELETE /v1/eval-datasets/{dataset_id} | state.tenant_id | W |
| api/eval_runs.py:75 | GET /v1/eval-runs | state.tenant_id | R |
| api/eval_runs.py:94 | POST /v1/eval-runs | state.tenant_id | W |
| api/eval_runs.py:117 | GET /v1/eval-runs/{run_id} | state.tenant_id | R |
| api/eval_runs.py:129 | GET /v1/eval-runs/{run_id}/cases | state.tenant_id | R |
| api/feedback.py:50 | POST /v1/sessions/{thread_id}/feedback | state.tenant_id | W |
| api/health.py:55-57 | GET /healthz/live·ready·startup | other — 探针 | R |
| api/knowledge.py:178 | POST /v1/knowledge/bases | state.tenant_id | W |
| api/knowledge.py:222 | GET /v1/knowledge/bases | state.tenant_id | R |
| api/knowledge.py:245 | GET /v1/knowledge/bases/{name} | state.tenant_id | R |
| api/knowledge.py:260 | PATCH /v1/knowledge/bases/{name} | state.tenant_id | W |
| api/knowledge.py:312 | POST /v1/knowledge/bases/{name}/reindex | state.tenant_id | W |
| api/knowledge.py:345 | DELETE /v1/knowledge/bases/{name} | state.tenant_id | W |
| api/knowledge.py:356 | POST /v1/knowledge/bases/{name}/documents | state.tenant_id | W |
| api/knowledge.py:399 | GET /v1/knowledge/bases/{name}/documents | state.tenant_id | R |
| api/knowledge.py:410 | DELETE /v1/knowledge/bases/{name}/documents/{id} | state.tenant_id | W |
| api/knowledge.py:424 | POST /v1/knowledge/bases/{name}/documents/{id}/reingest | state.tenant_id | W |
| api/knowledge.py:468 | GET /v1/knowledge/bases/{name}/documents/{id}/chunks | state.tenant_id | R |
| api/knowledge.py:502 | POST /v1/knowledge/bases/{name}/test | state.tenant_id | R(查询型 POST) |
| api/mcp_catalog.py(6 端点) | /v1/platform/mcp-catalog… | other — system_admin+bypass_rls | R/W |
| api/mcp_oauth_api.py:181 | POST /v1/mcp-servers/catalog/{id}/oauth/initiate | state.tenant_id | W |
| api/mcp_oauth_api.py:283 | GET /v1/mcp-oauth/callback | state.tenant_id | R(带写副作用) |
| api/mcp_oauth_api.py:394 | GET /v1/mcp-oauth/connections | state.tenant_id | R |
| api/mcp_oauth_api.py:406 | DELETE /v1/mcp-oauth/connections/{id} | state.tenant_id | W |
| api/mcp_servers.py:393 | POST /v1/mcp-servers | state.tenant_id | W |
| api/mcp_servers.py:543 | GET /v1/mcp-servers/catalog | state.tenant_id(+bypass_rls 平台行) | R |
| api/mcp_servers.py:577 | POST /v1/mcp-servers/catalog/{id}/enable | state.tenant_id | W |
| api/mcp_servers.py:653 | DELETE /v1/mcp-servers/catalog/{id}/enable | state.tenant_id | W |
| api/mcp_servers.py:702 | GET /v1/mcp-servers | state.tenant_id | R |
| api/mcp_servers.py:710 | POST /v1/mcp-servers/test | other — 无状态探测 | R |
| api/mcp_servers.py:745 | GET /v1/mcp-servers/available | state.tenant_id | R |
| api/mcp_servers.py:805 | GET /v1/mcp-servers/{name}/tools | state.tenant_id | R |
| api/mcp_servers.py:885 | PATCH /v1/mcp-servers/{name} | state.tenant_id | W |
| api/mcp_servers.py:1015 | DELETE /v1/mcp-servers/{name} | state.tenant_id | W |
| api/me.py:106 | GET /v1/me | other — 回显 principal | R |
| api/members.py:105 | POST /v1/members/invite | state.tenant_id | W |
| api/members.py:160 | GET /v1/members | other — 仅认 `"*"`;**具体 tenant_id 被静默忽略** | R |
| api/members.py:198·252·294·375 | resend / reset-password / DELETE / :purge | state.tenant_id | W |
| api/memory.py:204 | GET /v1/memory | ensure_tenant_scope(支持 `*`) | R |
| api/memory.py:273 | PATCH /v1/memory/{memory_id} | state.tenant_id | W |
| api/memory.py:345 | DELETE /v1/memory/{memory_id} | state.tenant_id | W |
| api/memory.py:376 | POST /v1/memory/{memory_id}/correct | state.tenant_id | W |
| api/metrics.py:18 | GET /metrics | other — Prometheus | R |
| api/model_catalog.py:51 | GET /v1/model-catalog | other — 全局目录 | R |
| api/plan.py:114 | GET /v1/sessions/{thread_id}/plan | state.tenant_id | R |
| api/plan.py:143 | PUT /v1/sessions/{thread_id}/plan | state.tenant_id | W |
| api/platform_*(config/billing/delegation/dynamic_worker/embedding/judge/quality/tool_budget/skills/rate_card,~60 端点) | /v1/platform/… | other — system_admin(+bypass_rls);embedding-config/status 无角色 | R/W |
| api/platform_config.py:636-843 | /v1/platform/credentials/tenants/{tenant_id}/… | other — 目标租户走**路径**,`_require_tenant` | R/W |
| api/quality.py:70 | GET /v1/quality/scores | state.tenant_id | R |
| api/quality.py:85 | GET /v1/quality/drift-alerts | state.tenant_id | R |
| api/quota.py:56·81·109 | POST /v1/quota/check·reserve·commit | other — tenant_id 取自**请求体**(服务主体) | R/W |
| api/quota.py:127 | POST /v1/quota/release/{reservation_id} | other — **接受任意 ?tenant_id=,无 allowed_tenants 校验(安全洞)** | W |
| api/role_bindings.py:66 | POST /v1/role_bindings | state.tenant_id(platform_scope⇒NULL) | W |
| api/role_bindings.py:123 | GET /v1/role_bindings | ensure_tenant_scope(支持 `*`) | R |
| api/role_bindings.py:172 | DELETE /v1/role_bindings/{binding_id} | state.tenant_id | W |
| api/runs.py:931 | POST /v1/sessions/{thread_id}/runs | state.tenant_id | W |
| api/runs.py:1093 | GET /v1/sessions/{thread_id}/runs/{run_id} | state.tenant_id | R |
| api/runs.py:1193 | GET /v1/sessions/{thread_id}/runs/{run_id}/trace | state.tenant_id | R |
| api/runs.py:1228 | GET /v1/sessions/{thread_id}/runs/{run_id}/trace/raw | state.tenant_id | R |
| api/runs.py:1268 | GET /v1/sessions/{thread_id}/messages | ensure_tenant_scope | R |
| api/runs.py:1350 | GET /v1/sessions/{thread_id}/runs | ensure_tenant_scope | R |
| api/runs.py:1411 | GET /v1/sessions/{thread_id}/runs/{run_id}/events | state.tenant_id — **SSE 流,归属租户硬绑** | R |
| api/runs.py:1503 | POST /v1/sessions/{thread_id}/runs/{run_id}/resume | state.tenant_id | W |
| api/runs.py:1673 | GET /v1/runs | ensure_tenant_scope(支持 `*`) | R |
| api/sandbox_egress_audit.py:78 | GET /v1/sandbox-egress-audit | ensure_tenant_scope(支持 `*`) | R |
| api/sandboxes.py:35 | POST /v1/sandboxes/reap | other — system_admin 运维操作 | W |
| api/service_accounts.py:55 | POST /v1/service_accounts | state.tenant_id | W |
| api/service_accounts.py:90 | GET /v1/service_accounts | ensure_tenant_scope(支持 `*`) | R |
| api/service_accounts.py:120 | DELETE /v1/service_accounts/{id} | state.tenant_id | W |
| api/sessions.py:259 | POST /v1/sessions | state.tenant_id | W |
| api/sessions.py:362 | GET /v1/sessions/{thread_id} | state.tenant_id | R |
| api/sessions.py:392·432·468·515 | /v1/sessions/{thread_id}/workspace… | state.tenant_id | R/W |
| api/sessions.py:553·604 | /v1/sessions/{thread_id}/workspace/artifacts/… | state.tenant_id | R/W |
| api/sessions.py:651 | GET /v1/sessions | ensure_tenant_scope(支持 `*`) | R |
| api/sessions.py:759·792·820 | PATCH / DELETE / :purge /v1/sessions/{thread_id} | state.tenant_id | W |
| api/sessions.py:942·963·984 | :pause / :resume / :cancel | state.tenant_id | W |
| api/setup.py:88·102 | /v1/setup… | other — 引导 | R/W |
| api/skill_evolution.py(9 端点) | /v1/skill-evolution/… | ensure_tenant_scope(全家,含写) | R/W |
| api/skills.py:941 | GET /v1/skills | ensure_tenant_scope(支持 `*`) | R |
| api/skills.py 其余 15 端点 | 详情/版本/文件/prompt/订阅/导入导出 | state.tenant_id | R/W |
| api/tenant_config.py:155·174·214 | /v1/tenants/{tenant_id}/config… | other — 路径租户+`_ensure_tenant_match`(无切换审计/无 GUC 重绑) | R/W |
| api/tenant_quotas.py:37·62·93 | /v1/tenants/{tenant_id}/quotas… | other — 同上 | R/W |
| api/tenants.py:199·299·395·431 | /v1/tenants… | other — 平台级 system_admin | R/W |
| api/triggers.py:293 | POST /v1/triggers | state.tenant_id | W |
| api/triggers.py:382 | GET /v1/triggers | ensure_tenant_scope(支持 `*`) | R |
| api/triggers.py:442·470·526·571 | 详情 GET/PATCH/DELETE/:fire | state.tenant_id | R/W |
| api/triggers.py:741 | POST /v1/webhooks/{trigger_id} | other — 入站 ingest,per-trigger secret | W |
| api/uploads.py:233 | POST /v1/sessions/{thread_id}/uploads | state.tenant_id | W |
| api/uploads.py:398 | DELETE /v1/uploads/{image_id} | state.tenant_id | W |
| api/usage.py:110·168 | GET /v1/usage/cost·tokens | state.tenant_id | R |
| api/webhook_endpoints.py:158 | POST /v1/webhook-endpoints | state.tenant_id | W |
| api/webhook_endpoints.py:234 | GET /v1/webhook-endpoints | ensure_tenant_scope(支持 `*`) | R |
| api/webhook_endpoints.py:263·275·324 | 详情 GET/PATCH/DELETE | state.tenant_id | R/W |
| api/workspace.py:79·130·156·195 | GET /v1/workspace… + DELETE file | state.tenant_id | R/W |

## 附录 B — 前端 SDK 全量分类

带 scope 的 20 个:listAgents、getConversation、listConversations、
listApprovals、listArtifacts、listAudit、listApiKeys、listCandidates、
listEvalDatasets、listEgressAudit、listMemories、listRoleBindings、
listServiceAccounts、listSkills、listPromoteRequests、listTriggers、
listWebhookEndpoints、listAgentUsers、getTenantUser、listUsers;手动带
tenantId 2 个:getSessionMessages、listThreadRuns;部分:listMembers(仅 `"*"`)。

不带 scope 且挂在租户业务页面(修复目标,~128 个,按域):

- **agents.ts**:getAgent(实测 404)、updateAgent、createAgent、deleteAgent、
  disableAgent、enableAgent、listRevisions、getRevision、rollbackToRevision
- **runs/sessions/trace**:getRun、resumeRun、streamRunEvents(SSE)、
  createSession、streamRun、submitSessionFeedback、listSessions、renameSession、
  archiveSession、purgeSession、getRunTrace、fetchRunTraceRaw、getThreadPlan、
  updateThreadPlan、uploadImage、uploadDocument
- **knowledge.ts 全家 11 个**;**skills.ts 详情全家 15 个** + skill-evolution
  详情 8 个(后端 scope-aware,纯前端漏,归 Class B);**triggers**:getTrigger、
  createTrigger、patchTrigger、deleteTrigger、fireTriggerNow;**webhooks**:
  getWebhookEndpoint、create/patch/delete
- **eval/curation**:listEvalRuns、getEvalRun、getEvalRunCases、enqueueEvalRun、
  getCandidate、promote/dismiss、eval-datasets CRUD
- **memory**:updateMemory、deleteMemory、correctMemory;**artifacts**:
  downloadArtifact、deleteArtifact、patchArtifactKind、listArtifactVersions;
  **workspace 4 个**;**usage**:getUsageTokens(用户详情页)
- **TS 设置页**:mcp-servers 全家、mcp-oauth 全家、members 写全家、api_keys 写、
  service_accounts 写、role_bindings 写、quality 2 个、usage
- **页面漏传(SDK 已支持)**:ConversationDetail.tsx:99、Users.tsx:66、
  UserProfile.tsx:58、UserDetail.tsx:301

平台泄漏进业务页 3 处:listPlatformCatalog / listCatalogTools(McpToolPicker)、
listRateCards(PlaygroundTab)、getPlatformEmbeddingStatus(CreateAgentModal)。

死导出 9 个:getAuditEntry、getEvalDataset、createSkill、addSkillVersion、
getSkillVersion、getTrigger、getWebhookEndpoint、deletePlatformProvider、
deletePlatformTool。
