# PR4 删除前置检查:模板 extends 炸雷 + agent 软删残留 + MCP 留空语义 — 设计文档

> 删除接口卫生修复计划第 4 批(共 5 批)。PR1(#1048)/PR2(#1049)/PR3(#1050)已合并。
> 本批主题:删除动作缺**前置依赖检查**导致的下游炸雷/僵尸,及一处检查语义错位。

## 背景(审计 + 侦察复核,2026-07-25 按 main@d2ae1fcb 复核)

1. **平台模板 extends 炸雷**:`agent_templates.py:206-234` 删除只查 RBAC+存在性,
   **硬删**(无软删列)且零依赖检查。租户 spec 经 `spec.extends="name@version"`
   继承平台模板(`agent_spec.py:1153`);构建时 fail-closed——base 缺失 →
   `AgentFactoryError("extends target not found")`(runtime.py:571-572)→ 422
   `AGENT_BUILD_FAILED`。删除被继承的模板 = 给全部继承者埋跨租户炸雷。
   `extends` 埋在 `agent_spec.spec_json`(JSONB)无索引、无反查方法;
   `list_all_tenants`(agent_spec/base.py:89)是平台视角先例。
   现无任何"删除被继承模板→继承者 422"的端到端回归护栏。
2. **agent 软删残留**(审计原话两处校正):
   - **僵尸 trigger = 静默空转非报错**:软删不动 `enabled`,调度器每轮照扫
     (scheduler.py:258-276),`claim_cron_fire` 抢槽 stamp `last_fired_at` 后
     `trigger_firing.py:173-183` 对 DELETED agent 返 None——每槽一条 WARNING,
     无 run/无 trigger_run/无 DLQ,**永不自停**。TriggerStore 只有 `list_by_agent`
     查询,无按 agent 停用方法;trigger 唯一开关是 `enabled: bool`(无 PAUSED)。
   - **会话打开/resume 均 200**,404 只出现在起 run/审批续跑
     (runs.py:1002-1010 / :643-647,`get(include_deleted=False)` 挡的),detail
     为 "agent not found"——与"从未存在"不可区分。
   - **delete 与 disable 不对称**:`disable_agent`(agents.py:1254)有 kill-switch
     +批量取消在飞 run;`delete_agent`(agents.py:1168-1203)零级联,只
     `update_status(DELETED)`。
3. **MCP server 留空语义错位**:删除端点引用检查
   (mcp_servers.py:1006-1028 + `manifest_references_server` :50-69)只认
   `spec.tools[].servers` 显式列表包含该名;协议明写 `servers: list[str]`
   默认 `[]` 且 **"empty means every available server"**(agent_spec.py:1013-1015),
   运行时 `set(entry.servers) or None`(assembly.py:689)把空列表解析为
   "不限制=全部"。留空 agent 是隐式全依赖,不命中 409。
   **另发现确定 bug(必修)**:引用检查用 `list_by_tenant` **不滤 agent 状态**
   ——软删 agent 的 spec 也算引用,**假 409 永久锁死 server 删除**。

## 用户拍板(2026-07-25)

| # | 决策 | 结论 |
|---|------|------|
| D1 | 删被继承的平台模板 | **409 拦 + 报继承者清单,无 force**(继承者是活的租户资产,force=故意弄坏,与 catalog force 清从属数据性质不同) |
| D2 | 软删 agent 的 trigger | **禁用(enabled=false)**:新增按 agent 批量禁用,配置保留可审计可人工重开;另默认做——删除对齐 disable 力度,取消该 agent 在飞 run |
| D3 | 留空=全部的引用语义 | **不拦,响应带影响面提示**:留空本就是"动态跟随可用集合",删 server 后"全部"仍自洽;硬拦会让任一留空 agent 永久锁死所有 server 删除。删除响应/审计带 `implicit_all_agents` 计数 |

## 设计

### A. 平台模板删除:继承者反查 + 409(D1)

1. 反查放 **api 层**(不加 store 方法——`extends` 埋 JSONB 无列,SQL/in-memory
   双实现按 JSON 谓词等价成本高;照 `manifest_references_server` 的 app 层判定
   先例):`bypass_rls_session()` 下 `agent_spec.list_all_tenants` 分页遍历,
   对每份 spec 用 `parse_extends_ref`(agent_template_resolve.py:111)解析
   `extends` 并与 `{name}@{version}` 精确匹配。仅统计 **status != DELETED** 的
   spec(软删 agent 不算继承者——与 §C bug 修同一原则)。
2. 命中 → **409** `TEMPLATE_IN_USE`,body 携带:继承者总数 + 前 20 条
   `{tenant_id, agent: "name@version"}` 清单(cap 防 body 爆炸)。无 force。
3. 无命中 → 照删;审计 details 加 `dependents_checked: true`(区分新老行为)。
4. 端到端回归护栏测试:建模板→租户 spec extends 它→删模板 409→继承者构建
   仍 200;无继承者→204;**软删的继承者不拦**(哨兵)。

### B. agent 删除级联:禁用 trigger + 取消在飞 run + 410 语义(D2)

1. `TriggerStore.disable_for_agent(*, agent_name: str, agent_version: str,
   tenant_id: UUID) -> int`(SQL + in-memory 双实现,谓词逐字节一致):
   `enabled=true AND agent_name== AND agent_version== AND tenant_id==` →
   `enabled=false`。只禁指向被删版本的 trigger(agent 按 name+version 删)。
2. `delete_agent`(agents.py:1168):软删成功后
   a. 取消在飞 run(照 `disable_agent` 的 `list_running_for_agent` + 批量
      cancel 套路,best-effort);
   b. `disable_for_agent`(best-effort);
   c. 审计 details 加 `runs_cancelled / triggers_disabled` 计数;失败分支
      `runs_cancel_failed / triggers_disable_failed` 布尔(PR2/PR3 约束:
      best-effort 失败审计可见)。
3. **410 语义区分**:起 run(runs.py:1002-1010)与审批续跑(:643-647)两处
   改为 `get(include_deleted=True)` 后判状态:`status == DELETED` → **410**
   `AGENT_DELETED`(会话历史仍可读,续聊明确告知"智能体已删除");
   记录不存在 → 404 照旧。前端只显示 message,无解析依赖(plan 时复核
   调用面)。
4. 已禁用的 trigger 若人工重开而 agent 仍是 DELETED:调度空转路径
   (trigger_firing 返 None)照旧兜底,不新增守卫——重开是显式人工动作,
   WARNING 日志已有。

### C. MCP server 引用检查修缮(D3 + bug 修)

1. **bug 修(必修)**:`referencing` 计算跳过 `status == DELETED` 的 spec
   (假 409 锁死修复)。
2. 留空语义:`manifest_references_server` 保持只认显式列表(留空≠硬引用);
   新增 helper `manifest_uses_implicit_all(spec) -> bool`(存在 `type=="mcp"`
   且 `servers` 为空的工具条目)。
3. 删除成功路径:统计该租户 **active** spec 中隐式全部的 agent 数,进
   删除响应 body 与审计 details(`implicit_all_agents: N`)——管理员知晓
   影响面但不被拦。
4. 测试:`servers=[]` 显式空列表不算引用(补缺失用例)/ 软删 agent 引用
   不拦(bug 回归哨兵)/ 显式引用仍 409 / implicit 计数正确。

### D. 审计与可观测

- §A `dependents_checked` + 409 路径不发删除审计(未删);§B
  `runs_cancelled / triggers_disabled` + 失败布尔;§C `implicit_all_agents`。
- 日志不放请求派生值(CodeQL:extra 也被追踪);副作用不进 assert
  (PR3 CodeQL 教训)。

## 错误处理

- §A 反查失败(store 异常):**阻断删除**(fail-closed——查不清依赖就不删,
  与构建侧 fail-closed 同姿态)。
- §B 取消在飞 run / 禁用 trigger 失败:best-effort,软删照走,审计布尔可见。
- §C 引用检查在 `agent_spec_store is None` 时跳过的既有行为不动(最小部署)。

## 测试

- §A 端到端护栏(见 A4)+ 分页边界(继承者在第二页)+ cap 生效(>20 继承者)。
- §B `disable_for_agent` 双实现平价 + 变异(去 version 谓词 → 他版本 trigger
  被误禁测试红);delete_agent 级联端到端(trigger 禁用、在飞 run 取消、
  审计计数、失败注入布尔);410 vs 404 两分支。
- §C 见 C4;变异:`manifest_uses_implicit_all` 判定改永假 → implicit 计数
  测试红。
- 全部新 store 方法 SQL↔in-memory 平价 + 真容器集成。

## 范围外

- PR5(成员页员工清除入口 + NULL-user_id approval 盲区收口)。
- trigger 的 PAUSED 状态机(现只有 enabled 布尔,不为本批扩)。
- agent undelete/恢复端点(软删至今无恢复路径,另议)。
- mcp 运行时"显式引用的 server 缺失"构建校验(409 前门已挡,绕过场景仅
  DB 手改,低价值)。
