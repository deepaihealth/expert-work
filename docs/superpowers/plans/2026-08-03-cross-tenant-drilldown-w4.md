# 跨租户钻取 W4(收尾波)实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。
> 摸底报告(2026-08-03,file:line 全核实)是本计划的事实底座;与本文冲突时以真代码为准。

**Goal:** 跨租户钻取 program 最后一波:审计/scope 收口 + quota 安全洞 + 技能导出无损化 + 8 处回落改真聚合 + 前端清理,program 完结。

**Architecture:** 三 PR:PR-1 速赢(技能导出修复+前端清理,零依赖先行)、PR-2 审计与 scope 收口(含安全洞)、PR-3 真聚合全链(后端 store + 端点 + 前端列表)。PR 内 task 并行(worktree),PR 间串行合并。

**Tech Stack:** FastAPI + SQLAlchemy(async)/in-memory 双 store、React + antd v5、vitest/pytest。

## Global Constraints

- 前端口径不变:详情调用 `concreteTenantScope(apiTenantScope)`,列表裸传 `apiTenantScope`;URL `?tenant_id=` 优先于 context(SkillDetail readScope 先例)
- 双 store 谓词 byte-identical(sql.py / memory.py 同步改,教训见 memory-evolution)
- 切入态写控件一律 `ReadonlyTooltip`(components/ReadonlyTooltip.tsx),Popconfirm 在其内层
- 改 store 跑真容器集成测(`DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock`)
- CI 范围 mypy/ruff 全库;改组件跑全套 vitest
- 审计 detail schema:SYSTEM_TENANT_SWITCH 补 `mode:"switch"` + `intent:"read"|"write"`(按 HTTP method 推导),与 `_emit_blocked` 的 `{mode,target_tenant}` 对齐

---

## PR-1 速赢(Task B ∥ D1 ∥ D2,三 worktree 并行)

### Task B — tenant 技能导出/读取无损化(后端)
- `api/skills.py:1390` `build_skill_zip` 补 `supporting_files=` + `version=version.version`
- external 条目照 `platform_skills.py:1355-1382` dual-read 回填(`fetch_supporting_files` + 502 映射);skills.py 需新增 import + `_get_object_store` 等价取用
- 同源修 `skills.py:479-484` `get_supporting_file`:external 条目回填真实字节(现在返回空 content)
- 测试:导出往返含附件断言(inline + external 两态)、单文件读取 external 态

### Task D1 — 前端清理
- 4 处旧姿势 Tooltip → ReadonlyTooltip:`AgentDetail.tsx:128-138`、`:168-179`(Popconfirm 内层规则)、`ManifestTab.tsx:137-149`、`HistoryTab.tsx:188-200`;`AgentDetail.tsx:293-303` 是无关 Tooltip 别动
- 删 4 个死 SDK:getTrigger(triggers.ts:69)/getWebhookEndpoint(webhooks.ts:81)/getEvalDataset(curation.ts:182)/getSkillVersion(skills.ts:268)+ tenant_scope_passthrough.test.ts 对应用例
- 列表行内导出(用户明示):`SkillsList.tsx` 新增操作列 + `SettingsPlatformSkills.tsx` 行内「导出最新版」,走既有 `skillApi.exportVersion`

### Task D2 — SkillDetail readScope 下传 + PlaygroundTab 守卫
- `SkillDetail.tsx` 把 `readScope`(:122-125)prop 下传 5 子组件,子组件删自用 useTenantScope:GovernancePanel(:52 无 scope,高危)、EvalEvidencePanel(:42,55)、LineagePanel(:45)、FileEditor(:160-165)、RenameDeleteModals(:74-79)
- `PlaygroundTab.tsx:210` listRateCards 加 `identity?.isSystemAdmin` 前置守卫(非 admin 不发请求,成本区隐藏)

## PR-2 审计与 scope 收口(Task A,内部两 commit)

### A-1 SWITCH 审计口径
- `tenant_scope.py:230-245` details 补 `mode`/`intent`

### A-2 tenant_config + tenant_quotas + quota 收口
- `tenant_config.py`(:161/:182/:226)+ `tenant_quotas.py`(:44/:70/:101)6 handler:删两份 `_ensure_tenant_match`,换 `ensure_single_tenant_scope` + `applied_scope`(path-param 端点姿势:path tenant_id 作为 scope 输入校验)
- `quota.py` 安全洞:`commit_quota`(:109-125)/`release_quota`(:127-144)接 `_tenant_id_from_query_or_principal` 等价校验 + 域审计(QUOTA_COMMIT/QUOTA_RELEASE)+ 跨租户时 SYSTEM_TENANT_SWITCH;mtls 白名单语义保留(:210-211)
- 测试:6 handler 跨租户审计断言、commit/release 越权 403 + 审计行

## PR-3 真聚合全链(C1 ∥ C3 → C2+D3 绑定,E 收尾)

### C1 — 低成本真聚合 ×5
- knowledge(:246)/eval_runs(:102)/quality×2(:95/:122)/mcp_servers list(:720):各 store 加 `list_*_all_tenants`(模板 `skill/sql.py:337`),端点换 `ensure_tenant_scope` + CrossTenant 分支,响应补 tenant_id 列
- base.py + sql.py + memory.py 三处同步,集成测真容器

### C3 — mcp-servers/available(:779)
- 改 `ensure_single_tenant_scope`,"*" 显式 400 SCOPE_ALL_NOT_SUPPORTED(语义:available=本租户可用,聚合无意义)

### C2+D3 — usage×2 + 前端列表(同批,契约耦合)
- usage cost(:141)/tokens(:213):store 聚合读 + 端点内分桶 key 加 tenant 维度,响应形状加 tenant 字段
- 前端:KnowledgeAdmin/EvalRunsList/SettingsQuality/SettingsUsage(或 BillingChargeback)聚合态加租户列 + 行跳转带 `?tenant_id=`(照 SkillsList.tsx:433-445);空态文案区分 empty_cross/empty_home

### Task E — 测试 helper 下沉(最后,避免 rebase 冲突)
- 14 处 `_grant_system_admin`(13 同名 + mcp_servers `_on(app)` 变体)抽 `tests/conftest.py` 双签名 fixture,14 文件 repoint

## 验收
- PR-1:导出往返含附件 diff 一致;列表行内导出下载成功;4 Tooltip hover 出提示
- PR-2:切入态读写 tenant_config/quotas 出 SWITCH 审计行(带 mode/intent);commit/release 越权 403
- PR-3:"*" 下 knowledge/eval/quality/usage/mcp 列表出全租户数据 + 租户列;available 400;行跳转进对应租户详情
- 全波:pytest + integration + 全套 vitest + tsc 绿;发测试环境 smoke 9/9
