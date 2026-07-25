# 删除接口卫生 follow-up 打包 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 收掉删除卫生 5 批 PR(#1048-#1052)留下的 correctness/健壮性 follow-up:精确取消在飞 run、MCP 引用检查漏检、recovery 竞态守卫、成员清除可追责性,加三条护栏测试。

**Architecture:** 无迁移、无新表、无新端点。5 处既有代码的收窄/加固 + 测试补强。每项都来自已归档的任务级/终审裁量记录。

**Tech Stack:** FastAPI + SQLAlchemy async + pytest。

## Global Constraints

- SQL 与 in-memory 双实现谓词**逐字节一致** + 平价测试;集成测须 `export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock`。
- best-effort 失败必须审计/响应可见(布尔或计数);日志**不放请求派生值**(CodeQL py/log-injection 对 `extra=` 同样追踪);**副作用不进 assert**(CodeQL py/side-effect-in-assert)。
- 变异自验 load-bearing(brief 指定的变异必须做并记录红/绿);TDD 先红后绿。
- 终门 CI 同款:ruff 全库 / `ruff format --check` / CI-scope mypy(`packages services/{audit-backup-worker,billing-rollup-job,event-log-archive-job,orchestrator,retention-cleanup-job}/src`)/ 全量 pytest。已知本机噪音(非回归):rls_detect 顺序依赖、pgbouncer、eval_engine_live、pg_restore_drill、orchestrator 顺序串扰(单跑复绿)。
- 分支 `fix-deletion-hygiene-followups`,基 main(含 8d3a71f1)。

## 并行波次

- **波 1(4 并行 worktree,文件互不相交)**:T1(runtime store + agents.py)/ T2(mcp_servers.py)/ T3(knowledge/recovery.py)/ T4(members.py)
- **波 2**:T5(纯测试补强,与 T2 共享 test_mcp_servers_api.py 故排后)
- **T6 终门** + opus 全分支终审。
- worktree 从 main 切出:dispatch 第一步 `git merge --ff-only fix-deletion-hygiene-followups`。

---

### Task 1: 在飞 run 取消收窄到版本级

**背景:** PR4 的 `delete_agent` 级联用 `list_running_for_agent(agent_name=name)` 取消在飞 run —— 删 `foo@v1` 会连带 INTERRUPT 仍活跃的 `foo@v2` 会话(终审 Minor,当时判"宁可多取消")。真相是 SQL 侧**已经 join `thread_meta`**、而 `thread_meta.agent_version` 存在(`models/thread_meta.py:34`,nullable Text)——加一个可选谓词即可精确,**不需要给 `RunInfo` 加列、不需要迁移**。

**Files:**
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/runs/store.py`(抽象 :172 / InMemory :497 / Sql :926)
- Modify: `services/control-plane/src/control_plane/api/agents.py:1213`(delete 级联传版本;**:1309 disable 保持 name 级不动**)
- Test: `packages/expert-work-runtime/tests/test_run_store.py`、`test_sql_run_store.py`、`services/control-plane/tests/test_agents_api.py`

**Interfaces:**
- Produces: `list_running_for_agent(*, tenant_id: UUID, agent_name: str, agent_version: str | None = None) -> list[RunInfo]` —— `agent_version=None` 保持既有 name 级语义(disable 用),给值则加 `ThreadMetaRow.agent_version == agent_version` 谓词。

- [ ] **Step 1: 写失败测试**(store 两实现同型 + 端点):

```python
# test_run_store.py / test_sql_run_store.py(同型)
async def test_list_running_for_agent_filters_by_version_when_given():
    # thread A 绑 foo@v1(RUNNING run)、thread B 绑 foo@v2(RUNNING run)
    both = await store.list_running_for_agent(tenant_id=TEN, agent_name="foo")
    assert {r.run_id for r in both} == {RUN_V1, RUN_V2}          # 不传版本 = 旧语义
    only_v1 = await store.list_running_for_agent(
        tenant_id=TEN, agent_name="foo", agent_version="v1"
    )
    assert {r.run_id for r in only_v1} == {RUN_V1}                # 版本哨兵
```

端点测试(test_agents_api.py):删 `foo@v1` 时 `foo@v2` 的在飞 run **不被取消**(状态仍 RUNNING),`foo@v1` 的被取消且 `runs_cancelled == 1`。
- [ ] **Step 2: 跑测试确认红**(`export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock`;端点测试红在"v2 run 被误取消")
- [ ] **Step 3: 实现**

```python
# store.py SqlRunStore — 既有 stmt 之后按需追加谓词(保持既有 join/order 不变):
if agent_version is not None:
    stmt = stmt.where(ThreadMetaRow.agent_version == agent_version)
```

InMemory 版镜像同一谓词(该实现经 `self._thread_meta_store` 解析绑定;`_thread_meta_store is None` 仍返 `[]`);抽象 docstring 补一句"``agent_version`` 给值时把结果收窄到该版本(删除级联用);``None`` 保持 name 级(kill switch 用)"。
`agents.py:1213` 改为 `list_running_for_agent(tenant_id=tenant_id, agent_name=name, agent_version=version)`;`:1309`(disable_agent)**一字不动**——kill switch 是 name 级设计。
- [ ] **Step 4: 跑测试确认绿**
- [ ] **Step 5: 变异自验**——把 SQL 侧新谓词改成恒真(`if False:`)→ 端点"v2 不被误取消"测试红;恢复绿。记录。
- [ ] **Step 6: Commit** `fix(runtime): 在飞 run 取消收窄到 agent 版本级(删 v1 不再误杀 v2 会话)`

### Task 2: MCP 引用检查分页

**背景:** `mcp_servers.py` 删除端点的引用检查用 `list_by_tenant(tenant_id=..., limit=1000)` 单页(PR4 终审 Observation,**改动前即如此**):租户 spec 超 1000 时,第 1001 份起的引用不被发现 → 误 204 删掉在用的 server。同域的模板反查(`agent_templates.py` `_find_extends_dependents`)已是分页姿态,本项与之对齐。

**Files:**
- Modify: `services/control-plane/src/control_plane/api/mcp_servers.py`(引用检查块,`list_by_tenant(limit=1000)` 处)
- Test: `services/control-plane/tests/test_mcp_servers_api.py`

- [ ] **Step 1: 写失败测试**:播种 1001+ 份 spec,把**唯一**引用该 server 的 spec 放在第二页(晚于前 1000 份)→ DELETE 应 **409**;现状 204(漏检),测试红。为控时长,页大小抽成模块级常量 `_SPEC_PAGE_SIZE`(默认 200),测试 monkeypatch 成小值(如 2)后只需播 3 份 spec 即可跨页——**测试改常量而非改断言**。
- [ ] **Step 2: 确认红**
- [ ] **Step 3: 实现**——照 `_find_extends_dependents` 的循环形状:

```python
_SPEC_PAGE_SIZE = 200

specs: list[AgentSpecRecord] = []
offset = 0
while True:
    page = await agent_spec_store.list_by_tenant(
        tenant_id=tenant_id, limit=_SPEC_PAGE_SIZE, offset=offset
    )
    specs.extend(page)
    if len(page) < _SPEC_PAGE_SIZE:
        break
    offset += _SPEC_PAGE_SIZE
```

下游 `active_specs` / `referencing` / `implicit_all` 三段逻辑与每 spec 单次 `model_dump` 的既有形状**不动**。先核 `list_by_tenant` 是否接受 `offset`(不接受则本任务改签名前先在报告里标出,勿擅自扩接口)。
- [ ] **Step 4: 确认绿**
- [ ] **Step 5: 变异自验**——把循环换回单页 `limit=1000` → 跨页测试红;恢复绿。记录。
- [ ] **Step 6: Commit** `fix(control-plane): MCP server 引用检查分页(>1 页租户的在用引用曾被漏检)`

### Task 3: knowledge recovery worker 补 gone 守卫

**背景:** PR3 §D 给 `ingestion._run` 加了"文档已被并发删除 → 静默终止"守卫;`knowledge/recovery.py` 的同款竞态没加(终审判良性:CAS 不再 claim 已删行、`mark_document_failed_terminal` 对已删行 0 命中),但会留一轮无谓 WARNING + FAILED 语义噪音。本项做一致性收口。

**Files:**
- Modify: `services/control-plane/src/control_plane/knowledge/recovery.py`(失败处理分支,照 `ingestion.py` `_run` except 块形状)
- Test: `services/control-plane/tests/test_knowledge_recovery.py`

- [ ] **Step 1: 写失败测试**:桩 store 使 ingest 抛异常且 `get_document` 返 `None`(文档在途被删)→ **不调** `mark_document_failed_terminal`、静默结束;对照分支:文档仍在 → 照旧 mark failed。前者现状红。
- [ ] **Step 2: 确认红**
- [ ] **Step 3: 实现**——照 `ingestion.py:216-238` 的 gone 判定逐字同构(`get_document(...) is None` → `logger.debug("knowledge.recovery_document_gone document=%s", document_id)` + 提前返回;判定本身失败按"未删"处理)。日志只放 UUID,不放请求派生字符串。
- [ ] **Step 4: 确认绿**
- [ ] **Step 5: 变异自验**——gone 判定改永假 → "已删不 mark failed"测试红;恢复绿。记录。
- [ ] **Step 6: Commit** `fix(knowledge): recovery worker 补文档已删守卫(与 ingest 路径一致)`

### Task 4: 成员清除可追责性 + 数据步 best-effort

**背景:** PR5 终审两条:①`MEMBER_PURGE` 审计 details 只有粗布尔 `data_purged`,不含 `purge.ok`——审计行分不清"数据步跑了且全成功"与"跑了但 partial 失败";②数据步解析(`users.get(...)` / `_build_purge_deps(request)`)未套 best-effort,transient 失败会在 KC 删除**之后** 500 中断链且**不落审计行**。

**Files:**
- Modify: `services/control-plane/src/control_plane/api/members.py`(`purge_member` 数据步 + emit)
- Test: `services/control-plane/tests/test_members_api.py`

- [ ] **Step 1: 写失败测试**:①有数据步的清除 → 审计 details 含 `purge_ok`(true);②注入 partial(purge summary 带 failures)→ 审计 `purge_ok: false` 且响应 `purge.ok === false`;③注入数据步解析异常(monkeypatch `users.get` 抛)→ **200**(非 500)+ `data_purged: false` + `data_purge_failed: true` + 审计行照落(前置步的 `kc_deleted` 等仍如实记)。三条现状红。
- [ ] **Step 2: 确认红**
- [ ] **Step 3: 实现**——数据步整体包 try/except(与端点其余 best-effort 步同形状):失败记 `data_purge_failed = True`、`data_purged` 保持 false、`purge` 保持 null,日志静态串 + `exc_info=True`;emit details 增 `purge_ok`(有 summary 时 `summary.ok`,无数据步时 `None`)与 `data_purge_failed`。响应 data 同步带 `data_purge_failed`(契约只增不改,与既有可选布尔同姿态)。
- [ ] **Step 4: 确认绿**
- [ ] **Step 5: 变异自验**——把 `purge_ok` 恒写 `True` → partial 测试②红;恢复绿。记录。
- [ ] **Step 6: Commit** `feat(control-plane): 成员清除审计带 purge_ok + 数据步 best-effort 化`

### Task 5: 三条护栏测试补强(纯测试,依赖 T2 落地)

**背景:** 三处任务级审查点名"代码正确但缺回归护栏"的缺口。

**Files:**
- Modify: `services/control-plane/tests/test_triggers_api.py`(403 防御纵深)
- Modify: `services/control-plane/tests/test_user_purge.py`(approval 失败分支)
- Modify: `services/control-plane/tests/test_mcp_servers_api.py`(DEPRECATED wildcard 计数)

- [ ] **Step 1: 写测试**(本任务全部是测试,三条都应**先对当前代码跑绿**——它们是护栏而非缺陷复现;逐条用变异证明其咬合):

1. **trigger 403 时子行存活**:非 admin 删他人 unowned trigger → 403,且该 trigger 的 `trigger_run` 行**仍在**(现状代码顺序正确;变异=把级联删移到权限门之前 → 本测试必须红)。
2. **approval 清理失败分支**:桩 `approvals.delete_for_threads` 抛 → `purge_user` 不中断、`summary.failures["agent_approval"]` 有值、后续步照跑(变异=把该 try/except 去掉 → 异常穿透使测试红)。
3. **DEPRECATED wildcard 计入 implicit**:一个 `status=DEPRECATED` 且 `servers=[]` 的 spec → 删 server 成功且 `implicit_all_agents` **含**它(变异=把 active 过滤改成只留 ACTIVE → 本测试红)。

- [ ] **Step 2: 跑测试确认绿**(护栏对当前代码应绿)
- [ ] **Step 3: 逐条变异自验**——按上面括注的变异逐条注入,确认每条测试都红、恢复后绿。三条变异结果全部写进报告(**这是本任务唯一的价值证明,不得省略**)。
- [ ] **Step 4: Commit** `test: 补三条删除卫生护栏(403 子行存活/approval 失败分支/DEPRECATED wildcard 计数)`

### Task 6: 终门

- [ ] `uv run ruff check .` / `uv run ruff format --check .` / CI-scope mypy
- [ ] 全量 pytest(DOCKER_HOST;红项对照已知噪音清单,新红必须查因)
- [ ] 全绿后 opus 全分支终审(`review-package $(git merge-base main HEAD) HEAD`)

## Self-Review 记录

- 每项 follow-up 溯源:T1=PR4 终审 Minor;T2=PR4 终审 Observation(预存);T3=PR3 终审 ⚠️;T4=PR5 终审 MEDIUM+LOW;T5=T4/T1/T4 三处任务级审查的"缺护栏"。
- 类型一致:`list_running_for_agent(..., agent_version: str | None = None)` T1 内三处实现 + 两调用点一致。
- 波 1 四任务文件互不相交(runtime store+agents.py / mcp_servers.py / recovery.py / members.py);T5 与 T2 共享 mcp 测试文件故排波 2。
