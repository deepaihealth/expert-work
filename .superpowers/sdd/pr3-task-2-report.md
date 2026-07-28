# PR3 Task 2 报告:platform_delegation_config 配置节全栈

> **注**:本文件为控制者事后重建(原报告在 worktree 清理时随未跟踪文件被删)。内容来自实施者最终摘要 + task 审查记录,数字与结论均为当时实录;细节粒度低于 T1/T3 的原始报告。

## 交付

照 `platform_dynamic_worker_config`(#1029)模板逐文件改名改字段,共 4 commit:

- `578fe7b4` feat(persistence): platform_delegation_config 单行配置表(委托并发闸容量)
- `57c121d5` feat(control-plane): PlatformDelegationConfigService(DB-wins TTL 缓存)
- `ddd640b7` feat(control-plane): /v1/platform/delegation-config 端点 + 审计 + app.py 接线
- `5c521956` feat(admin-ui): 平台委托并发闸容量配置卡(cost tab,单字段 1-64)

层次:persistence models + migration 0137 + store 四件套(base/memory/sql)/ service(env_default 16、DB-wins、TTL 双检锁)/ API `/v1/platform/delegation-config` GET/PUT(system_admin-only,`Field(ge=1, le=64)`,`extra="forbid"`)/ 审计枚举 `PLATFORM_DELEGATION_UPDATED = "platform_delegation_config:updated"` / app.py 全接线(store 解析、service 构造、app.state、include_router、_SqlStores、SQL 装配)/ admin-ui api client + `PlatformDelegationSection`(testid 前缀 `pdg-`)+ SettingsPlatformConfig 挂载 + i18n 三处 + e2e spec。

**范围纪律**:runtime.py / AgentRuntime 接线不在本任务(Task 3 交付)。

## 对 brief 的偏离(2 处,审查核实均为「模板实况优先」,正确)

1. 单行表主键:brief 括注「id 恒 1 CHECK」,实际 0124 模板是 Text 主键 `"singleton"` 无 CHECK——照模板。
2. i18n 命名:brief 写 `platformDelegation.*`,仓内实际约定是扁平 `settings_platform.*`——用 `settings_platform.delegation_*`,避免引入新命名形状。

## 测试(实施时实录)

- persistence store 3 passed;service+API 10 passed
- control-plane 全量 2205 passed(6 个 pre-existing 无关失败 in test_eval_engine_live.py)
- admin-ui vitest 1313 passed + typecheck clean + build clean
- ruff check + format clean;CI-scope mypy clean(791 files)
- persistence 全量 926 passed(1 个 pre-existing 顺序依赖 flake,standalone 过;3 个 docker-pull 基建错误,均与本任务文件无关)

## Task 审查结论(sonnet,实录)

✅ Spec compliant,**Approved**,0 Critical / 0 Important。逐层(~22 文件)与模板 field-by-field 比对无漂移;两处偏离经真模板文件核实属实。2 Minor(均模板家族既有、非本任务引入):

1. service 家族(dynamic_worker 同款)缺 TTL 过期触发重读的专测。
2. service 的 30s TTL 默认在 app.py 被 `tenant_config_cache_ttl_s`(默认 60s)覆盖——与模板先例一致。

终审补充(全分支 opus):SQL store 无独立 store 测试(M-5,模板同缺);RLS docstring 矛盾系模板拷贝继承(M-6)。均记 follow-up。
