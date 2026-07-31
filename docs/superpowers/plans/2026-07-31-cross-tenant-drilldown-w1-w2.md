# 跨租户钻取修复 W1+W2 实施计划(2026-07-31)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。
> 三条并行 track,各自独立 worktree/分支/PR,文件零交叉(冲突分析见文末)。

**Goal:** 系统管理员切入租户后:对话/用户页可用(A);run/agent 详情读链可用
+ Langfuse 深链出现(B+C);quota release 安全洞关闭(A)。写操作切入态置灰(C)。

**Architecture:** 后端照 conversations.py:329 先例逐端点接
`tenant_id: UUID | Literal["*"] | None` query 参数 → `ensure_tenant_scope` →
`applied_scope`,详情端点拒 `"*"`(400);前端 SDK 加可选 `tenantScope` 末参 +
`withTenantScope`,组件层直接 `useTenantScope()` 取 scope(不穿 prop);新
hook `useIsTenantSwitched` 单源判定切入态,写控件置灰。

**Tech Stack:** FastAPI + pytest;React + TS + vitest。

## Global Constraints

- spec:`docs/superpowers/specs/2026-07-31-cross-tenant-drilldown-design.md`;
  审计:`docs/research/2026-07-31-cross-tenant-drilldown-audit.md`
- 一期**只开读**:写端点后端一律不接 scope
- 后端测试三件套(每个新接 scope 的读端点):①system_admin 带目标租户
  `tenant_id` 命中目标租户数据 ②普通租户用户带他租户 `tenant_id` → 403
  `TENANT_NOT_ALLOWED` ③详情端点带 `tenant_id=*` → 400。照
  `tests/test_conversations_api.py` 既有 scope 测试先例
- 前端测试:SDK 函数断言请求 query 含 `tenant_id`;切入态禁用断言两态
- CI 口径:mypy 含 tests(lambda 换具名函数)、ruff 全库、CI-scope 照旧
- commit 格式 `<type>: <描述>`,无 attribution

---

## Track A(PR-A)— W1:quota 安全洞 + B 类纯前端

分支 `feat/ct-w1-quota-and-frontend-scope`。docs 三件(spec/审计/本计划)随本 PR 入仓。

### Task A1:quota release 越权修复(后端)

**Files:** `services/control-plane/src/control_plane/api/quota.py`(:186
`_tenant_id_from_query_or_principal`);Test:`services/control-plane/tests/test_quota_api.py`(找既有测试文件,没有则建)

现状:helper 把 `?tenant_id=` 解析成 UUID 直接返回,**无 allowed_tenants 校验**
——任何认证主体可释放他租户配额预留。

修法(保持 mTLS 语义:`auth/mtls.py:188` 服务主体 `allowed_tenants=(system_tenant,)`,
docstring 明说"query param is then required"):

```python
def _tenant_id_from_query_or_principal(
    request: Request,
    principal: Annotated[Principal, Depends(require("quota", "check"))],
) -> UUID:
    raw = request.query_params.get("tenant_id")
    if not raw:
        return principal.tenant_id
    try:
        target = UUID(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={...不变...}) from exc
    # mTLS service principals(orchestrator 等内部服务)保持原语义:
    # 它们挂在 system tenant 上,释放任意租户的预留是其本职。
    if principal.auth_method == "mtls":
        return target
    # 用户/服务账号主体:目标租户必须在其授权范围内(system_admin 的
    # allowed_tenants == "*" 恒过)。
    if principal.allowed_tenants != "*" and target not in principal.allowed_tenants:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "TENANT_NOT_ALLOWED",
                "message": "the caller is not authorized for this tenant",
            },
        )
    return target
```

测试(先红后绿):①租户 A 用户带租户 B 的 `?tenant_id=` → 403;②system_admin
带任意租户 → 通过(404 RESERVATION_NOT_FOUND 也算通过闸);③mTLS 主体带任意
租户 → 通过;④无参 → principal.tenant_id(既有行为回归)。

### Task A2:B 类页面补传(前端)

**Files:**
- `apps/admin-ui/src/pages/ConversationDetail.tsx`(:99 `getConversation(threadId)`)
- `apps/admin-ui/src/pages/Users.tsx`(:66 `listUsers()`)
- `apps/admin-ui/src/pages/UserProfile.tsx`(:58)、`apps/admin-ui/src/pages/UserDetail.tsx`(:301)(`getTenantUser(userId)`)
- Test:各页面既有 test 文件补 scope 断言

四个页面接 `useTenantScope()`,把 `apiTenantScope` 传给已支持 scope 的 SDK
函数(`getConversation(threadId, tenantScope)` / `listUsers({..., tenantScope})`
/ `getTenantUser(userId, tenantScope)`)。ConversationDetail 下游
getSessionMessages/listThreadRuns 用 conversation.tenant_id,修好源头自动正确
——**验证此链路**,若有直接用 scope 处一并透传。

### Task A3:skill-evolution 读端点透传(前端)

**Files:** `apps/admin-ui/src/api/skill-evolution.ts`(:115 listEvalResults、
:130 getLineage、:160 getKillSwitch);callers:`pages/skill_detail/EvalEvidencePanel.tsx`、
`pages/skill_detail/LineagePanel.tsx`、`components/SkillEvolutionKillSwitch.tsx`;Test:对应 vitest

后端全家已 scope-aware(skill_evolution.py `_single_scope`)。三个**读**函数加
`tenantScope?: TenantScope` 末参 + `withTenantScope`;三个 caller 组件内
`useTenantScope()` 透传。写函数(approve/reject/engage/release)**不动**。

---

## Track B(PR-B)— W2 后端:run/agent 详情读端点接 scope

分支 `feat/ct-w2-backend-read-scope`。只动 services/control-plane。

**统一模板**(照 conversations.py:329,含 import、audit、applied_scope;
详情端点拒 `"*"`):

```python
tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
...
if tenant_id == "*":
    raise HTTPException(status_code=400, detail={
        "code": "SCOPE_ALL_NOT_SUPPORTED",
        "message": "detail endpoints require a concrete tenant_id",
    })
scope = await ensure_tenant_scope(
    principal, tenant_id, audit,
    trace_id=current_trace_id_hex(), endpoint=<route>, 
    cross_tenant_enabled=cross_tenant_query_enabled(request),
)
async with applied_scope(scope):
    <原 store 调用,tenant_id=scope.tenant_id>
```

### Task B1:runs.py 四端点

**Files:** `services/control-plane/src/control_plane/api/runs.py`:1093(getRun)、
1193(trace)、1228(trace/raw)、**1411(events SSE)**;Test:
`tests/test_runs_api.py` 等既有文件

SSE 端点(1411)同套路——scope 解析在建流前做,403/400 直接 HTTP 错误(不进
流);流内部逐段读 DB 处沿用 scope.tenant_id。注意 1093 现有
`applied_scope(SingleTenant(home))` 只重绑 GUC,改为完整 ensure_tenant_scope。

### Task B2:agents.py + sessions.py + artifacts.py + audit.py 读端点

**Files:**
- `agents.py`:949(getAgent)、1057(listRevisions)、1091(getRevision)
- `sessions.py`:362(getSession)、392/432/468(workspace 读三件)、553(artifacts 下载)
- `artifacts.py`:149(download)、328(versions)
- `audit.py`:198(getAuditEntry)
- Test:对应既有测试文件

同模板。getAgent 后续的 `ensure_resource_access`(instance RBAC)保持不动
(system_admin 恒过)。测试三件套逐端点。

---

## Track C(PR-C)— W2 前端:SDK 透传 + 切入态禁用 UI

分支 `feat/ct-w2-frontend-scope-ui`。只动 apps/admin-ui。与后端独立可合
(FastAPI 忽略未声明 query 参数 → 前端先合无害;后端先合前端未传参 = 现状)。

### Task C1:useIsTenantSwitched hook

**Files:** Create `apps/admin-ui/src/tenant/useIsTenantSwitched.ts`;Test:
`apps/admin-ui/src/tenant/__tests__/useIsTenantSwitched.test.tsx`

```typescript
/** 切入态 = scope 为具体租户 UUID 且 ≠ 归属租户。"*" 聚合与 home 都不算。 */
export function useIsTenantSwitched(): boolean {
  const { scope } = useTenantScope();
  const { identity } = useAuth();
  return (
    scope !== SCOPE_HOME &&
    scope !== SCOPE_ALL &&
    scope !== identity?.homeTenantId
  );
}
```

### Task C2:SDK 函数加 tenantScope

**Files:** `apps/admin-ui/src/api/`:`agents.ts`(getAgent/listRevisions/
getRevision)、`runs.ts`(getRun/streamRunEvents——SSE 是拼 URL,手动加
`tenant_id` 参数照 listThreadRuns :177 先例)、`trace_facade.ts`(getRunTrace/
fetchRunTraceRaw)、`artifacts.ts`(downloadArtifact/listArtifactVersions)、
`sessions.ts`(getSession 若有导出)。全部可选末参,不破既有调用。Test:vitest
断言 query。

### Task C3:组件/页面透传

**Files:** `components/turn/TurnCard.tsx`(getRun/getRunTrace/downloadArtifact
——组件内 `useTenantScope()` 直取,**不穿 prop**)、`components/turn/useHistoryTurns.ts`
(streamRunEvents)、`pages/RunDetail.tsx` + `pages/run_detail/EventStreamPanel.tsx`
+ `pages/run_detail/TraceToolbar.tsx`、`pages/agent_detail/playground/TraceView.tsx`
(fetchRunTraceRaw)、`pages/AgentDetail.tsx`(getAgent ×2 处)、
`pages/agent_detail/HistoryTab.tsx`(listRevisions/getRevision)。

### Task C4:切入态禁用 UI

**Files:** `pages/AgentDetail.tsx`(启用/禁用/删除按钮)、
`pages/agent_detail/ManifestTab.tsx`(保存)、`pages/agent_detail/HistoryTab.tsx`
(回滚)、`pages/agent_detail/PlaygroundTab.tsx`(发送框/新建会话/重试/审批决策
/反馈——调试台写链全体)、i18n 两 locale 加 tooltip 键
`tenant_switched_readonly`:「已切入租户,只读视角——写操作请切回归属租户」。
Test:两态断言(mock useIsTenantSwitched)。

统一姿势:`disabled={... || isTenantSwitched}` + antd Tooltip。TurnCard 的
readOnly 机制已有(对话页),调试台切入态不复用 readOnly(语义不同:历史轮
仍要能看事件),只禁写控件。

---

## 冲突分析(三 track 文件集)

- **A**:quota.py + ConversationDetail/Users/UserProfile/UserDetail +
  skill-evolution.ts + 3 个 skill 组件
- **B**:runs.py/agents.py/sessions.py/artifacts.py/audit.py + 后端 tests
- **C**:agents.ts/runs.ts/trace_facade.ts/artifacts.ts/sessions.ts +
  TurnCard/useHistoryTurns/RunDetail 系/TraceView/AgentDetail/ManifestTab/
  HistoryTab/PlaygroundTab + hook 新文件 + i18n 两 locale

**零交叉**。A 的 ConversationDetail 只动 getConversation 调用行;C 的 TurnCard
组件内取 scope,不碰 ConversationDetail。若 C 需在 ConversationDetail 加
东西——不需要(TurnCard 自取)。i18n 文件仅 C 动。

## 合并序

三 PR 独立可合,无序要求。全合后真栈冒烟(W2 验收清单:切入乐毅大公司 →
agent 详情开、对话详情开、调试台历史轮可看、「在 Langfuse 打开」出现、发送框
置灰)。W3/W4 计划另出。
