# 删除接口卫生 PR5:成员一键停用并清除 + approval 空转收口 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 成员页一键"停用并清除"(生命周期+KC 删账号+收权+数据清除,D1/D2)+ 修复 purge_user 的 approval 清理全空转(唯一创建路径 user_id 恒 NULL)。

**Architecture:** spec 见 `docs/superpowers/specs/2026-07-25-deletion-hygiene-pr5-design.md`。无迁移;1 个新端点 + 1 处编排补丁 + 前端入口。

**Tech Stack:** FastAPI + pytest;React + antd + vitest。

## Global Constraints

- best-effort 失败审计/响应布尔可见;**member 生命周期转移失败阻断后续**(半态防护)。
- 日志不放请求派生值(CodeQL);**副作用不进 assert**(CodeQL,PR3 被逮)。
- 变异自验 load-bearing;TDD 先红后绿。
- i18n 新键 en + zh-CN + interface 三处同步,先查撞键。
- 终门 CI 同款:ruff 全库 / format / CI-scope mypy(packages + 5 services)/ 全量 pytest(已知本机噪音:rls_detect 顺序、pgbouncer、eval_engine_live、pg_restore_drill、orchestrator 顺序串扰单跑绿)+ 前端 `pnpm typecheck` + vitest。
- 分支 `fix-deletion-hygiene-pr5`,基 main(含 0a8082b7)。

## 并行波次

- **波 1(2 并行 worktree)**:T1(purge/user_purge.py)/ T2(api/members.py + protocol audit)
- **波 2**:T3(前端,依赖 T2 端点契约)
- **T4 终门** + opus 全分支终审。
- worktree 从 main 切出:dispatch 第一步 `git merge --ff-only fix-deletion-hygiene-pr5`。

---

### Task 1: purge_user approval 空转收口(§B)

**Files:**
- Modify: `services/control-plane/src/control_plane/purge/user_purge.py`(`_purge_threads` feedback 块 :204-210 后追加;`delete_all_for_user` 步 :348-353 docstring 更新)
- Test: `services/control-plane/tests/test_user_purge.py`

- [ ] **Step 1: 失败测试(回归哨兵)**:造用户线程 + 挂其上的 **NULL-user_id** approval(照 orchestrator sse.py:1013 现实:`ApprovalRecord(..., user_id=None, thread_id=<用户线程>)`)→ `purge_user` → approval 行消失、`summary.deleted["agent_approval"] >= 1`。现状全空转,测试红。另断言:他用户线程的 NULL approval 不动(thread 谓词哨兵)。
- [ ] **Step 2: 确认红**
- [ ] **Step 3: 实现**——feedback 块正下方同型追加(同 try/except 形状):

```python
    # Approvals are keyed by thread, not user — the only writer (orchestrator
    # sse.py pause flow) stamps user_id=None on every row, so the per-user
    # delete below never matches. Thread-scope delete is the one that works.
    try:
        summary.deleted["agent_approval"] = await deps.approvals.delete_for_threads(
            thread_ids=thread_ids, tenant_id=tenant_id
        )
    except Exception as exc:
        logger.warning("purge_user.approvals_failed", exc_info=True)
        summary.failures["agent_approval"] = f"{type(exc).__name__}: {exc}"
```

注意 `delete_for_threads(*, thread_ids, tenant_id)` 关键字顺序(approval 版与 feedback 版参数顺序相反,PR3 既定)。per-user 步(:348-353)保留,注释更新为"backstop for future user-stamped rows; thread-scope pass above covers today's NULL rows"。若两步计数会撞同一 `deleted` 键,per-user 步改记 `agent_approval_user_scope`(或累加,以既有 `deleted` 记账习惯为准)。
- [ ] **Step 4: 确认绿**
- [ ] **Step 5: 变异自验**——注释掉新增块 → 哨兵红;恢复绿。记录。
- [ ] **Step 6: Commit** `fix(control-plane): purge_user 接 thread 级 approval 清理(per-user 步对 NULL-user_id 行全空转)`

### Task 2: 组合端点 POST /v1/members/{member_id}:purge(§A,D1+D2)

**Files:**
- Modify: `services/control-plane/src/control_plane/api/members.py`(revoke 端点 :286-365 之后新增;共享逻辑就地组织)
- Modify: `packages/expert-work-protocol/src/expert_work/protocol/audit.py`(:81 后加 `MEMBER_PURGE = "member:purge"`)
- Test: `services/control-plane/tests/test_members_api.py`

**Interfaces:**
- Consumes: 既有 `purge_user` 编排 + `_build_purge_deps`(agent_users.py:286——直接 `from control_plane.api.agent_users import _build_purge_deps`,同应用内私有共享,加注释;不搬家);`KeycloakAdminClient.delete_user`(404=成功幂等);`role_binding_repo.delete_for_subject`(照 revoke :341-347);`member_repo.transition`。
- Produces: `POST /v1/members/{member_id}:purge` → 200 信封 `data = {member_id, status, kc_deleted, kc_delete_failed?, role_bindings_removed, role_bindings_cleanup_failed?, data_purged, purge?: PurgeSummary.as_dict()}`(T3 消费)。

- [ ] **Step 1: 失败测试(状态机矩阵)**:①invited → revoked + KC `delete_user` 被调 + `data_purged: false`(subject_id NULL);②active(有 subject_id+数据)→ suspended + KC 删除 + purge summary 返回 + 数据行消失;③suspended 补清 → 状态不变 + KC 删除 + purge 跑;④重跑幂等(200,各步 no-op);⑤KC 失败注入(`KeycloakUnavailableError`)→ 200 + `kc_delete_failed: true` + 其余步照走;⑥非 admin 403;⑦转移失败注入(transition 返 False,invited/active 情形)→ **409 阻断**,无 KC/数据副作用。审计:`MEMBER_PURGE` + details 布尔/计数。
- [ ] **Step 2: 确认红**
- [ ] **Step 3: 实现**——编排(照 revoke 块风格):

```python
@router.post("/{member_id}:purge")
async def purge_member(...同 revoke 的 Depends + request: Request, users: TenantUserStore ...):
    member = get → 404
    now = datetime.now(UTC)
    # 1) lifecycle — 转移失败阻断(半态防护)
    if member.status == "invited":
        moved = await transition(to="revoked"); target_status = "revoked"
    elif member.status == "active":
        moved = await transition(to="suspended"); target_status = "suspended"
    else:  # suspended / revoked — 已终态,补清不转移
        moved, target_status = True, member.status
    if not moved:
        raise HTTPException(409, {"code": "MEMBER_STATE_CONFLICT", ...})
    # 2) role_binding(照 revoke 块逐字,keycloak_user_id 键,best-effort)
    # 3) KC delete_user(D2,best-effort,KeycloakUnavailableError → kc_delete_failed)
    #    keycloak_user_id is None → 跳过(kc_deleted=False)
    # 4) data:member.subject_id 非 None →
    #    summary = await purge_user(tenant_id=principal.tenant_id,
    #        user_id=member.subject_id, subject_id=<user 行的 subject_id,经
    #        users.get(member.subject_id, ...) 取;None 则跳过数据步>,
    #        deps=_build_purge_deps(request), actor_id=..., trace_id=...)
    # 5) emit MEMBER_PURGE(details: email/from_status/kc_deleted/
    #    role_bindings_removed/各失败布尔/data_purged)
    # 6) 200 信封(Produces 契约)
```

注意:purge_user 需要 `subject_id`(tenant_user 的外部 subject 字符串)——经 `users.get(member.subject_id, tenant_id=...)` 拿 user 行;行不在(异常态)→ `data_purged: false` 不炸。日志静态串。
- [ ] **Step 4: 确认绿**(`uv run pytest services/control-plane/tests/test_members_api.py -q`)
- [ ] **Step 5: 变异自验**——注掉 KC delete_user 步 → 测试①②的 KC 断言红;恢复绿。记录。
- [ ] **Step 6: Commit** `feat(control-plane): 成员一键停用并清除端点(生命周期+KC 删账号+收权+数据清除)`

### Task 3: 前端成员页入口(§C,依赖 T2)

**Files:**
- Modify: `apps/admin-ui/src/pages/SettingsMembers.tsx`(危险动作列 :292-349 区)
- Modify: `apps/admin-ui/src/pages/user_profile/PurgeUserModal.tsx`(泛化:新增可选 props `confirmTarget?: string`(覆盖默认 subjectId 匹配)与 `onSubmit?: () => Promise<PurgeSummary | MemberPurgeResult>`(覆盖默认 purgeUser 调用);默认行为不变,UserProfile 零改动)
- Modify: `apps/admin-ui/src/api/members.ts`(新增 `purgeMember(memberId): Promise<MemberPurgeResult>` + 类型)
- Modify: `apps/admin-ui/src/api/users.ts:140-144`(陈旧"员工 409"docstring 更正为现实)
- Modify: i18n 三处:`i18n/locales/en.ts` + `zh-CN.ts` + interface(`settings_members.purge_*` 键:action/confirm_title/confirm_body/type_to_confirm(email)/no_data_note/done/partial)
- Test: `apps/admin-ui/src/pages/__tests__/SettingsMembers.test.tsx`

- [ ] **Step 1: 失败测试**:①"停用并清除"按钮对 invited/active/suspended 成员可见;②点开 Modal,输错 email 确认钮 disabled、输对启用;③确认调 `purgeMember(member.id)`,成功刷新列表;④响应含失败布尔(partial)→ Modal 留驻展示;⑤`subject_id === null`(未首登)→ Modal 显示 no_data_note 文案。
- [ ] **Step 2: 确认红**(vitest)
- [ ] **Step 3: 实现**——PurgeUserModal 泛化保持向后兼容(新 props 全可选,缺省走原 purgeUser 路径;`armed = confirmText.trim() === (confirmTarget ?? subjectId)`);SettingsMembers 危险列新增按钮(照 set-password 按钮 :309-322 的结构),Modal 传 `confirmTarget={member.email}`、`onSubmit={() => purgeMember(member.id)}`;partial 判定读 `kc_delete_failed`/`role_bindings_cleanup_failed`/`purge.ok === false` 任一。antd 静态 Modal 测试渲染:文件若用 `Modal.confirm` 类静态 API 须 `App.useApp()`(agent-config 教训——本处用受控 `<Modal>` 无此坑,留意别改成静态)。
- [ ] **Step 4: 确认绿**——`pnpm -C apps/admin-ui typecheck` + `pnpm -C apps/admin-ui test -- SettingsMembers UserProfile`(UserProfile 回归:泛化未破坏默认路径)。
- [ ] **Step 5: Commit** `feat(admin-ui): 成员页一键停用并清除(type-to-confirm email + partial 留驻)`

### Task 4: 终门

- [ ] `uv run ruff check .` / `uv run ruff format --check .` / CI-scope mypy
- [ ] 全量 pytest(DOCKER_HOST;对照已知噪音清单)
- [ ] `pnpm -C apps/admin-ui typecheck` + 全量 vitest
- [ ] 全绿后 opus 全分支终审(`review-package $(git merge-base main HEAD) HEAD`)

## Self-Review 记录

- Spec 覆盖:§A=T2,§B=T1,§C=T3,§D 分散;范围外未混入。
- 类型一致:`delete_for_threads(*, thread_ids, tenant_id)`(approval 版顺序)T1 内一致;`purgeMember(memberId)` T2 契约 ↔ T3 消费一致。
- T1/T2 文件不相交(user_purge.py vs members.py+audit.py),波 1 并行安全;T2 调 purge_user 不改其文件。
