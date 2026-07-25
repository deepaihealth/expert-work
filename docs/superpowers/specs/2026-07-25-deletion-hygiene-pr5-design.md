# PR5 成员一键停用并清除 + approval 空转收口 — 设计文档

> 删除接口卫生修复计划第 5 批(最后一批)。PR1(#1048)/PR2(#1049)/PR3(#1050)/PR4(#1051)已合并。
> 本批主题:员工离职的**一站式清除**入口,与 purge_user 的一处**全空转步骤**修复。

## 背景(侦察复核,2026-07-25 按 main@8c264b26;两处历史前提已过时)

1. **前提校正**:早期设计(Phase 3a)的"member 闸 + 员工导向成员页"已被后续
   迭代拆除——`POST /v1/users/{user_id}:purge`(agent_users.py:490)对任意用户
   (含员工/自己)直接放行,docstring 明确"purging never touches
   tenant_member / Keycloak / role bindings";前端 /users 详情页全员可 purge,
   `api/users.ts:140-144` 的"员工 409"docstring 是陈旧残留。
2. **现状缺口**:员工彻底离职需要两条流程手工接力——成员页 revoke
   (members.py:286:invited→revoked+删 KC,active→suspended+仅 disable KC,
   +role_binding 清理)+ /users 详情页 purge(数据)。成员页无数据清除入口;
   **suspended 员工的 KC 账号只是 disabled,永留 Keycloak**。
   `KeycloakAdminClient.delete_user` 已现成(admin_client.py:35-67,404=成功
   幂等;Http + Fake 双实现)。
3. **approval 清理全空转**:`agent_approval` 的唯一创建路径
   (orchestrator sse.py:1013)**硬编码 `user_id=None`**——purge_user 的
   `delete_all_for_user`(user_purge.py:348-353,按 user_id 过滤且 docstring
   明确不碰 NULL 行)**从未删过任何行**。purge_session(PR3)的
   `delete_for_threads` 是现在唯一有效的 approval 清理;purge_user 的
   `_purge_threads`(:171-234)已收全 thread_ids 且已调 feedback 的同型方法,
   唯独没接 approval。
4. **两把钥匙**:`tenant_member.subject_id` = tenant_user.id(surrogate,
   purge 用);`tenant_member.keycloak_user_id` = KC sub(删账号/role_binding
   用)。未首登员工两者皆可能 NULL。前端 `TenantMember` 两字段都有
   (api/members.ts:15-28)。

## 用户拍板(2026-07-25)

| # | 决策 | 结论 |
|---|------|------|
| D1 | 成员页入口形态 | **一键停用并清除**:type-to-confirm 后一次处理(停用+KC+收权+清数据);原 revoke 按钮保留(只停用不清数据);未首登员工自动降级为纯停用 |
| D2 | 员工 KC 账号 | **随清除删掉**(delete_user 幂等);纯 revoke 流照旧只 disable |

## 设计

### A. 后端组合端点 `POST /v1/members/{member_id}:purge`(D1+D2)

1. 放 `members.py`(与 revoke 同域),gate 照成员页既有(admin);编排:
   a. member 查回,404 若无;**全状态可清**(invited/active/suspended/revoked
      ——已停用但数据未清的也要能补清,幂等重跑安全);
   b. member 生命周期:invited → revoked,active → suspended,
      suspended/revoked 不再转移(与既有 revoke 状态机一致);
   c. role_binding 清理(照 revoke 既有块,`keycloak_user_id` 键,best-effort
      + 失败布尔审计——PR2 约束);`keycloak_user_id is None` 跳过;
   d. **KC 账号删除**(D2):`keycloak.delete_user(member.keycloak_user_id)`
      (404=成功;best-effort + `kc_delete_failed` 布尔);
   e. 数据清除:`member.subject_id` 非 NULL → 调既有 `purge_user` 编排
      (agent_users 的 `_build_purge_deps` 装配复用,plan 定提取方式);
      NULL(未首登)→ 跳过,响应注明 `data_purged: false`;
   f. 响应:member 终态 + PurgeSummary(有数据步时)+ 各失败布尔;
   g. 审计:复用/新增 action plan 定(照 revoke 的审计形状,details 带
      `kc_deleted / role_bindings_removed / data_purged` 与失败布尔)。
2. 既有 revoke 端点(`DELETE /{member_id}`)**行为不动**(active 停用仍只
   disable KC——纯停用语义保留)。
3. 与 revoke 共享的步骤(生命周期转移/role_binding/KC 交互)提取共享
   helper 或直调,以最小重复为准(plan 定;两处语义漂移是命门,提取优先)。

### B. purge_user approval 空转收口

1. `_purge_threads` 在 feedback `delete_for_threads` 同位追加:

```python
await deps.approvals.delete_for_threads(thread_ids=thread_ids, tenant_id=tenant_id)
```

   (PR3 方法现成;NULL-user_id 行经 thread 谓词被覆盖。)计数进
   `PurgeSummary.deleted["agent_approval"]`。
2. 既有 per-user 步(`delete_all_for_user`)**保留**——防未来出现带
   user_id 的 approval 行;docstring 更新说明双步互补。
3. 测试:造 NULL-user_id approval 挂用户线程 → purge_user → 行消失
   (修复前红——现状全空转的回归哨兵)。

### C. 前端:成员页一键停用并清除

1. `SettingsMembers.tsx` 危险动作列新增"停用并清除"(与既有 revoke
   Popconfirm 并列);触发 type-to-confirm Modal(泛化复用
   `PurgeUserModal` 交互——提交函数/确认目标参数化,或轻量新建
   `MemberPurgeModal`,plan 定,避免复制漂移)。
2. **确认输入 = 成员 email**(员工没有外部 user_id,KC sub UUID 不友好;
   email 在成员行数据里)。
3. 调新 api `purgeMember(memberId)`(api/members.ts 新增);成功刷新列表
   + toast;partial(响应失败布尔)留 Modal 展示可重试(照 PurgeUserModal
   partial 先例)。
4. 未首登员工(subject_id NULL):按钮照常可用(后端自动降级纯停用+删 KC),
   Modal 文案注明"该成员未使用过系统,无业务数据"。
5. i18n:`settings_members.purge_*` 键,en + zh-CN + interface 三处同步
   (撞键先查,LLM-token-streaming epic 教训)。
6. 顺手清理:`api/users.ts:140-144` 陈旧"员工 409"docstring 更正。

### D. 审计与可观测

- 组合端点全步骤失败布尔审计可见(PR2 约束);日志不放请求派生值;
  副作用不进 assert(CodeQL 两先例)。

## 错误处理

- 组合端点 best-effort 链:单步失败不中断后续(与 purge_user `_step` 同
  哲学),失败布尔进响应+审计;**member 生命周期转移失败例外——阻断**
  (身份状态是主体,转移失败继续清数据会产生"活成员数据被清"的半态)。
- KC 不可用(`KeycloakUnavailableError`):KC 步失败布尔,其余照走
  (重跑可补删,delete_user 幂等)。

## 测试

- 后端组合端点状态机矩阵:invited(删 KC+revoked+无数据步)/
  active(suspended+删 KC+purge)/ suspended 补清(不转移+删 KC+purge)/
  重跑幂等;KC 失败注入 → 布尔;subject_id NULL 降级;非 admin 403。
- §B 回归哨兵(NULL-user_id approval,修复前红)+ PurgeSummary 计数。
- 前端:按钮可见性/type-to-confirm(email)/partial 留驻/未首登文案分支;
  `pnpm typecheck` + 组件测试。
- 变异自验:§B 去掉 delete_for_threads 调用 → 哨兵红;§A 把 KC 删除步
  注掉 → KC 断言红。

## 范围外

- 纯 revoke 流升级(保持只 disable);member 行硬删(90 天硬删已在 PR1
  的 tenant_user sweep,member 行本身无删除诉求);KC 账号恢复流程。
- /users 详情页 PurgeUserModal 的现有行为(继续全员可清,不回收)。
