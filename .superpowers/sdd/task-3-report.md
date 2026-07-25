# PR5 Task 3 报告 — 前端成员页"停用并清除"入口(§C)

## STATUS: DONE(TDD 先红后绿)

- 注:本文件覆盖的旧 `task-3-report.md` 是 PR4 遗留(delete_agent 级联报告),沿既定覆盖惯例;旧版完整保留在 git 历史。
- 起手 `git merge --ff-only fix-deletion-hygiene-pr5`(fast-forward `8c264b26..e24be19a`,带入 T1/T2)。

Commit: `84cf715c` feat(admin-ui): 成员页一键停用并清除(type-to-confirm email + partial 留驻)

## 交付内容

### 1. `api/members.ts` — 新 SDK 面

- `MemberPurgeResult` 接口,逐字段对齐 T2 落定的 200 信封 `data` 形状
  (`member_id / status / kc_deleted / kc_delete_failed / role_bindings_removed /
  role_bindings_cleanup_failed / data_purged / purge: PurgeSummary | null`)。
- `purgeMember(memberId)` → `POST /v1/members/{id}:purge`(照文件内既有
  `encodeURIComponent` + `postJson` 风格;URL 形状照 `users.ts` 的 `:purge` 先例)。
- `isMemberPurgePartial(result)` — partial 判定单源:
  `kc_delete_failed || role_bindings_cleanup_failed || (purge !== null && !purge.ok)`。
  放在类型旁(members.ts)而非 Modal 内,注释点明无 supervisor 部署里
  `purge.ok === false` 是已知形状,不特判。

### 2. `PurgeUserModal.tsx` 泛化(向后兼容,UserProfile 零改动)

新 props **全可选**,缺省走原路径:

- `confirmTarget?: string` — 覆盖 arming 目标:
  `target = confirmTarget ?? subjectId; armed = target.length > 0 && confirmText.trim() === target`;
  `<Text code>` 展示与 placeholder 同步用 `target`。
- `onSubmit?: () => Promise<PurgeSummary | MemberPurgeResult>` — 覆盖默认
  `purgeUser(userId)`;结果判定用 `"ok" in result` 判别联合
  (PurgeSummary 才有顶层 `ok`;MemberPurgeResult 走 `isMemberPurgePartial`)。
- `copy?: PurgeModalCopy` — 文案覆盖束(title / okText / body / typeToConfirm /
  done / partial),每字段回退原 `user_profile.purge_*` 文案。
- 一处有意的行为门:409"这是员工去成员页"的防御性提示**只在默认
  purgeUser 路径生效**(`onSubmit === undefined` 才走)——成员页自身弹
  "去成员页"是误导;members 流的 409(MEMBER_STATE_CONFLICT)落通用
  `purge_failed_title` 错误弹窗。

### 3. `SettingsMembers.tsx` — 入口 + Modal 装配

- 危险动作列新增"停用并清除"按钮(UserX 图标,照 set-password 按钮结构,
  `data-testid=members-purge-{id}`),**全状态可见**(suspended/revoked 行
  可补清——后端幂等重入);列宽 200→300 容纳第四个按钮。
- 跨租户聚合视图自然隐藏(按钮在既有 actions 列条件块内)。
- Modal 条件挂载(`purgeTarget !== null`),传
  `confirmTarget={member.email}`、`onSubmit={() => purgeMember(member.id)}`;
  copy.body = settings_members 专属警示 Alert({{email}} 插值)+
  `subject_id === null` 时的 no_data_note 段落
  (`data-testid=members-purge-no-data-note`);成功 → 关 Modal + refresh;
  partial → Modal 留驻(泛化后的原留驻逻辑)。

### 4. i18n 三处同步(先查撞键:settings_members 域原无 purge_* 键)

`settings_members.purge_action / purge_confirm_title / purge_confirm_body /
purge_type_to_confirm / purge_no_data_note / purge_done / purge_partial`,
en 值块 + en interface 块 + zh-CN 镜像三处齐。

### 5. 顺手更正

`api/users.ts` `purgeUser` 陈旧"员工 409"docstring 更正为现实
(任意用户可清;账号删除是成员页职责)。

## TDD 过程

- Step 1-2:先写 5 个测试(按钮三状态可见 / email type-to-confirm armed /
  成功调 `purgeMember("m-1")`+刷新+关 Modal / partial 留驻不刷新 /
  未首登 no_data_note),另在跨租户"隐藏写面"测试补 purge 按钮隐藏断言。
  **确认红**:5/5 新测试红(按钮 testid 不存在),存量 1268 全绿。
- Step 3:实现(上述 1-4)。
- Step 4:**确认绿**——`pnpm -C apps/admin-ui typecheck` 0 错;目标两文件
  SettingsMembers(9)+ UserProfile(9)= 18/18 绿(UserProfile 回归
  含默认路径 type-to-confirm / partial 留驻 / 409 防御 / self-warning 全绿,
  泛化未破坏);**全量套件 150 文件 / 1273 测试全绿**。

## 测试要点(套路记录)

- `vi.mock("../../api/members")` 工厂改用 `importOriginal` 展开真模块再覆盖
  各 fn——Modal 经真 `isMemberPurgePartial` 判 partial,桩掉它会让
  partial 测试空转(测不到判定逻辑)。
- 本机 `pnpm` corepack shim 被 SIGKILL(exit 137,原因未查),
  用 `node ~/.nvm/.../corepack/dist/pnpm.js` 直调绕过;worktree 需先
  `pnpm install`(admin-ui 独立 lockfile,非 monorepo workspace)。

## Concerns

1. **（无阻塞）** actions 列四按钮在窄屏可能换行——列宽已放到 300,
   纯 cosmetic,未做响应式折叠(YAGNI)。
2. **（无阻塞）** revoked 成员也显示"停用并清除"(后端幂等补清语义),
   brief 测试 ① 只点名 invited/active/suspended——已按设计文档
   "全状态可清"实现,若产品想对 revoked 隐藏是一行条件的事。
3. CI 的 `pnpm build`(vite build)未在本地跑(typecheck + 全量 vitest
   已绿,build 无新增配置面);CI 会兜。
