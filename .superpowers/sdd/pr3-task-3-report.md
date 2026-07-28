# Task 3 报告 — DelegationGate 进程级委托并发闸 + 两工具过闸 + 全链注入接线

## 实施摘要

按 brief 逐字采用 `DELEGATION_GATE_KEY = "delegation_gate"`、30s 默认超时、counter
`expert_work_delegations_gated_total`。TDD 顺序按 brief Step 1→5 走(先写
`test_delegation_gate.py` 覆盖闸体+两工具+`_build_tool_context`+`_child_config`,
RED→GREEN;再写 `test_delegation_gate_wiring.py` 覆盖 control-plane 接线)。

### 1. `services/orchestrator/src/orchestrator/tools/_budget.py`
新增 `DELEGATION_GATE_KEY`、`_delegations_gated` counter、`DelegationGate` 类
(与 brief 给的代码逐字一致)。新增 import:`Awaitable`/`Callable`(合并进已有
`collections.abc` import)、`typing.Final`、`expert_work.common.observability.expert_work_counter`
——leaf 模块地位不变(仍无 `orchestrator.tools.registry` 依赖)。

### 2. `services/orchestrator/src/orchestrator/tools/registry.py`
`ToolContext` 加 `delegation_gate: DelegationGate | None = None` 字段(追加在
`guard_sink` 之后,末位)。

### 3. `services/orchestrator/src/orchestrator/tools/subagent.py` / `spawn_worker.py`
两工具 `call()` 都在「deadline 检查之后、child builder 调用之前」插入
`gate.acquire()`;拒绝时返回软失败 `ToolResult`(`meta.delegation_gated=True`),
**不构建子 agent、不调 `run_child_to_result`**。成功路径用 `try/finally` 包住
builder 调用 + `run_child_to_result`(spawn_worker 额外包 `_maybe_concurrency`),
`finally` 里 `gate.release()`。gate 挂在 per-run budget 检查**之后**(spawn_worker),
预算已耗尽时闸完全不被触碰(有测试钉住:`test_spawn_worker_gate_applies_after_per_run_budget_exhausted`)。

放闸点选在 builder 调用**之前**而非之后:避免闸已满时还去 resolve/build 一个跑不了
的子 agent(尤其 spawn_worker 会先合成 worker spec)。brief 原文只说“run_child_to_result
之前”,没钉死相对 builder 的先后——这是我在两个合理位置之间做的选择,已在下方
「偏离」注明。

### 4. `services/orchestrator/src/orchestrator/graph_builder/builder.py`
`_build_tool_context` 新增 `configurable.get(DELEGATION_GATE_KEY)` 读取,喂进
`ToolContext(delegation_gate=...)`。

### 5. `services/orchestrator/src/orchestrator/sse.py`
`run_agent` 新增形参 `delegation_gate: DelegationGate | None = None`,写入
`effective_config["configurable"][DELEGATION_GATE_KEY]`(与 `worker_spawn_budget`
同一 dict 字面量,无条件注入,行为对齐既有 `worker_spawn_budget` 处理方式)。

### 6. `services/control-plane/src/control_plane/runtime.py`
`AgentRuntime` 新增 `delegation_config_service: PlatformDelegationConfigService | None = None`
字段 + `_delegation_gate: Any = field(default=None, repr=False)` 惰性槽位 +
`delegation_gate()` 方法(与 brief 给的代码逐字一致,惰性单例、`capacity_provider`
经 `service.effective().max_concurrent_delegations` 现读)。

### 7. `services/control-plane/src/control_plane/app.py`
lifespan 里紧跟 `dynamic_worker_config_service` 赋值之后接一行
`resolved_agent_runtime.delegation_config_service = resolved_platform_delegation_config_service`
(T2 已构造该 service 并挂 `app.state.platform_delegation_config_service`)。

### 8. 5 个 `run_agent` 调用点
`api/runs.py:747,897`(现文件行号,已核对与 brief 一致)、
`run_queue_worker.py:297`、`trigger_firing.py:296`、`orphan_sweep.py:300` 各加
`delegation_gate=runtime.delegation_gate()`(前两个 `self._runtime`,同步调用,
非 async,不需要 `await`)。

### 9. Tests(两个新文件)
- `services/orchestrator/tests/test_delegation_gate.py`(20 用例):闸体本身
  (容量等待/超时软失败/嵌套不死锁/热生效/release 通知/下溢钳位)+ `FakeGate` 驱动
  的 `SubAgentTool.call`/`SpawnWorkerTool.call`(成功 acquire+release 各1次、
  拒绝软失败不跑子 run、子 run 异常仍 release、`delegation_gate=None` 行为不变、
  gate 在 per-run budget 之后叠加)+ `_build_tool_context` 读取 + `_child_config`
  转发(见下方「偏离」)。
- `services/control-plane/tests/test_delegation_gate_wiring.py`(4 用例):
  `runtime.delegation_gate()` 无 service 返回 None / 惰性单例(两次调用同一对象)/
  容量跟随 service 热更新;真 `create_app` lifespan(`agent_fixtures` 同款模式,
  照抄 `test_credential_cache_wiring.py`)驱动后 `app.state.agent_runtime.delegation_gate()`
  非 None 且与 `app.state.platform_delegation_config_service` 对得上。

## 偏离 brief 之处及理由

1. **新增修改 `services/orchestrator/src/orchestrator/tools/_child_run.py`**
   (brief 的 Files 列表未列出此文件)。`_child_config()` 现在把 `ctx.delegation_gate`
   转发进子 run 的 `configurable[DELEGATION_GATE_KEY]`(与 `token_budget`/
   `guard_sink` 同款,但**不**与 `worker_spawn_budget` 同款——后者故意不下传,
   因为它是 per-run 累计预算)。

   理由:`DelegationGate` 被 brief 明确定性为「进程级」(process-wide)单例,
   Step 1 的测试要求覆盖「嵌套不死锁(容量1,depth1 占闸内再 acquire → 30s 超时
   软失败)」——这个场景只有在子 run(depth-2 委托)拿到与父 run 完全相同的闸
   对象时才可能真实发生(否则子 ctx.delegation_gate 恒为 None,gate 概念上
   只保护顶层调用,"嵌套死锁"这个风险描述本身就无从谈起)。子 run 是通过
   `_child_run.run_child_to_result` → `child.graph.ainvoke(child_input, child_config)`
   起的一个**全新的 graph invocation**,不经过 `sse.run_agent`,所以顶层的
   `DELEGATION_GATE_KEY` 注入到不了子 run——必须由 `_child_config` 显式转发。
   已加两条直接测试:`test_child_config_forwards_same_delegation_gate_object`
   / `test_child_config_omits_delegation_gate_when_absent`(镜像该文件已有的
   `test_child_config_forwards_same_token_budget_and_guard_sink` 惯例,见
   `test_token_budget_graph.py`)。

2. **gate acquire 相对 child builder 调用的先后顺序**:brief 的示例代码片段把
   `gate.acquire()` 放在一段注释掉的 `... # 原 run_child_to_result 调用` 之前,
   没有明确点名 builder() 调用应该在 acquire 之前还是之后。我选择了
   **acquire 在 builder() 之前**(闸满时完全不触碰 builder,尤其 spawn_worker
   要合成 worker spec,是有实际成本的步骤)。若之后复审认为应反过来(builder
   先跑、只在 run_child_to_result 前把关),属局部顺序调整,不影响对外行为契约
   (`ToolResult.meta` 形状、release 是否始终执行)。

3. **4 处测试用 `_FakeRuntime` 补丁**(brief 未提及,但触发是我改的
   `run_queue_worker.py`/`orphan_sweep.py`/`api/runs.py` 调用点):
   `test_approval_timeout_sweep.py`、`test_orphan_sweep.py`、
   `test_resume_idempotency_flow.py`、`test_run_queue_worker.py` 里的
   `_FakeRuntime` 测试替身只实现了 `new_worker_spawn_budget`,没有
   `delegation_gate` 方法——5 个调用点加了 `delegation_gate=runtime.delegation_gate()`
   后,这些 fake 触发 `AttributeError`。各加一个 `delegation_gate(self) -> None: return None`
   (与它们已有的 `new_worker_spawn_budget` 返回 `None` 同款,行为契约上等价于
   「未接线」)。`test_dynamic_worker_hot_reload.py` 用的是真 `AgentRuntime`,
   无需改。这正是 brief 提醒的历史坑("改 run_agent 签名要跑 control-plane 测")
   在这次实际命中的一个变体——命中点不是 `run_agent` 本身的签名,而是下游
   `runtime.delegation_gate()` 新增方法在多个 fake 替身上缺失。

## 测试命令 + 完整结果

```
# Step 1-4:orchestrator 新测试文件独立跑(TDD 过程中反复跑,此处给最终态)
$ DOCKER_HOST= uv run pytest services/orchestrator/tests/test_delegation_gate.py -x -q
20 passed in 0.86s

# Step 5:control-plane 接线测试
$ uv run pytest services/control-plane/tests/test_delegation_gate_wiring.py -x -q
4 passed, 5 warnings in 0.44s

# Step 6:全量门
$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
1499 files already formatted

$ uv run mypy packages services/audit-backup-worker/src services/billing-rollup-job/src \
    services/event-log-archive-job/src services/orchestrator/src services/retention-cleanup-job/src
Success: no issues found in 791 source files

# orchestrator 全测 —— 与 CI 同款拆分(unit / integration)
$ DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock \
    uv run pytest services/orchestrator -v -m "not integration" --timeout=120 --timeout-method=thread -q
1901 passed, 1 skipped, 1 deselected, 3 warnings in 23.95s
  (skip 是既有的 docx 模块缺失,与本任务无关)

$ DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock uv run pytest services/orchestrator -v -m integration -q
1 passed, 1902 deselected in 8.56s

# control-plane 全测 —— 同款拆分(改了 run_agent 签名的历史坑,必跑)
$ PYTHONPATH="$PWD" DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock \
    uv run pytest services/control-plane -m "not integration" --timeout=120 --timeout-method=thread -q
2196 passed, 19 deselected, 9 warnings in 302.53s (0:05:02)

$ PYTHONPATH="$PWD" DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock \
    uv run pytest services/control-plane -m integration -q
19 passed, 2196 deselected, 14 warnings in 88.83s (0:01:28)
```

补充说明:`PYTHONPATH="$PWD"` 是我为了让 `services/control-plane` 单独 scope 的
pytest 调用能解析到仓库根的 `tools` 包(`test_eval_engine_live.py` 里
`from tools.eval.adversarial import ...`)而加的——这纯粹是我把 pytest 调用范围
缩窄到 `services/control-plane` 引出的路径问题,与本任务改动无关(用 `git stash`
在基线上复现过同样的 6 个 `ModuleNotFoundError: No module named 'tools'` 失败,
加 `PYTHONPATH` 后基线和本改动都变绿)。CI 实际是从仓库根跑 `uv run pytest -m ...`
(全 testpaths 一起收集),不会踩这个坑。

## 自审发现

- **闸体正确性**:`DelegationGate.acquire()` 在 `asyncio.timeout` 包裹的
  `asyncio.Condition` 上 wait,`release()` 用 `notify_all()` 唤醒——用真并发测试
  (`asyncio.ensure_future` + `asyncio.wait_for`)钉住了「release 后等待者及时被唤醒
  而非靠轮询」和「热生效对下一次 acquire 生效,不影响已持有的槽位」两条语义,
  不是只测最终状态。
- **`_child_config` 转发是本次任务里唯一超出 brief 文件清单的改动**,已在上面
  「偏离」第 1 条详细说明必要性——如果不做这个转发,「进程级」闸在真实嵌套委托
  场景下会退化成「只挡得住顶层调用」,brief 自己强调的嵌套死锁风险场景根本无法
  在生产中触发(gate 在 depth-2 恒为 None)。
- **`expert_work_delegations_gated_total` 只注册一次**:两个工具文件都从
  `_budget.py` import 同一个模块级 `_delegations_gated` counter 对象,没有各自
  重复调用 `expert_work_counter(...)` 造成 Prometheus 重复注册报错——已通过
  `test_delegation_gate.py` + `test_subagent.py` + `test_spawn_worker.py` 同进程
  混跑验证(96 passed,无 `Duplicated timeseries` 类报错)。
- **未做但值得记录**:没有新增 `sse.py` 层面的专门单测断言
  `effective_config["configurable"][DELEGATION_GATE_KEY]`——`worker_spawn_budget`
  这个先例本身也没有对应的 sse 层单测(只在 `_build_tool_context` + 工具层测试
  覆盖),我照此先例保持一致,机制纯属把形参塞进已有 dict 字面量,风险低。
  `test_sse*.py` 回归全部随 orchestrator 全量跑过,绿。
- **真栈冒烟(PUT delegation-config + 起 run 观察 counter)未做**——brief 把这个
  列在「验证(整 PR)」的 Wave 3(合并前),不属于单个 Task 3 的交付范围,留给
  该阶段执行。

## 变更文件清单

Modify:
- `services/orchestrator/src/orchestrator/tools/_budget.py`
- `services/orchestrator/src/orchestrator/tools/registry.py`
- `services/orchestrator/src/orchestrator/tools/subagent.py`
- `services/orchestrator/src/orchestrator/tools/spawn_worker.py`
- `services/orchestrator/src/orchestrator/tools/_child_run.py`(偏离项,见上)
- `services/orchestrator/src/orchestrator/graph_builder/builder.py`
- `services/orchestrator/src/orchestrator/sse.py`
- `services/control-plane/src/control_plane/runtime.py`
- `services/control-plane/src/control_plane/app.py`
- `services/control-plane/src/control_plane/api/runs.py`
- `services/control-plane/src/control_plane/run_queue_worker.py`
- `services/control-plane/src/control_plane/trigger_firing.py`
- `services/control-plane/src/control_plane/orphan_sweep.py`
- `services/control-plane/tests/test_approval_timeout_sweep.py`(fake runtime 补丁)
- `services/control-plane/tests/test_orphan_sweep.py`(fake runtime 补丁)
- `services/control-plane/tests/test_resume_idempotency_flow.py`(fake runtime 补丁)
- `services/control-plane/tests/test_run_queue_worker.py`(fake runtime 补丁)

New:
- `services/orchestrator/tests/test_delegation_gate.py`
- `services/control-plane/tests/test_delegation_gate_wiring.py`

## Fix round 1(2026-07-28)

### 问题

审查发现 `DelegationGate.acquire` 把 `await capacity_provider()` 包在
`async with self._cond` 锁内(旧 `_budget.py:90-92`)。生产 provider
(`PlatformDelegationConfigService.effective`)TTL(30s)到期时做真 DB 读——
DB 卡顿会让持锁的那次 `acquire` 独占 `self._cond`,队头阻塞同进程内**所有**
`acquire` **和** `release`(`release` 也要拿同一把锁),把一次慢查询放大成
全平台委托的雪崩式软失败。

### 修复(4 处,均按 brief 逐字落地)

1. **容量读挪到锁外 + provider 异常 fail-open**(`_budget.py`):`acquire`
   改写为经典 condition-loop——每轮先在锁外 `await self._read_capacity()`,
   再进 `self._cond` 比较+自增。新增 `_read_capacity() -> int | None`:成功
   缓存到 `self._last_capacity` 并返回;`except Exception`(含内建
   `TimeoutError`,例如 asyncpg 查询超时)记 warning(`exc_info=True`)后
   返回 `self._last_capacity`(从未成功过则 `None`)。`None` 语义 = fail-open
   不设限(闸是延迟保护闸不是安全闸,与 `delegation_gate=None` 现状行为同向)。
   `asyncio.CancelledError` 是 `BaseException` 不会被 `except Exception` 捕获,
   外层 `asyncio.timeout` 到期时的取消信号能正常穿透到 `acquire` 的
   `except TimeoutError: return False`,不会被 `_read_capacity` 误吞。
   `_active += 1` 到 `return True` 之间仍无 `await`;`acquire` 返回 `False`
   绝不占坑。
2. **docstring 补约束**(`_budget.py` 类文档):写明 provider 在锁外执行、
   其耗时计入 `acquire` 超时窗、异常时 fail-open 用最近一次成功值、
   `release` 永不被 provider 阻塞。
3. **counter 改公开名**(Minor):`_delegations_gated` → `DELEGATIONS_GATED`
   ——`_budget.py` 定义处 + `spawn_worker.py` / `subagent.py` 两个 import
   处 + 各自 `.inc()` 调用点。
4. **budget 烧名额不回滚加注释**(Minor,`spawn_worker.py`):`try_reserve`
   与过闸判断之间补一句注释,说明闸拒时已 `reserve` 的 per-run 名额有意不
   回滚(`WorkerSpawnBudget` 无反向 API;闸拒是瞬态、budget 是防失控上限,
   烧掉偏保守方向)。

### 测试(`test_delegation_gate.py` 新增 4 个,RED→GREEN 各自验证)

- `test_release_not_blocked_by_slow_provider`:provider `sleep(0.2)`;先占满
  唯一容量,另起一个 acquirer 卡在（锁外的）慢 provider 读上,同时对占用者
  调用 `release()`——断言 `release` 在 `<0.1s` 内完成。**RED 验证**:用
  `git`-free 的手工回退法——临时把 `acquire()` 换回旧的「锁内读 provider」
  实现(保留 `DELEGATIONS_GATED` 新名不动,以免测试文件 import 失败),单独
  跑这一个测试,复现 `assert 0.150... < 0.1` 失败(旧代码下 `release` 被卡在
  等待者持有的 provider-read 锁后面);随后原样换回新实现,同一测试转绿。
- `test_provider_exception_fails_open`:provider 恒抛 `RuntimeError` →
  `acquire` 立即 `True`,`active` 记账正常,`release` 后归零。
- `test_provider_timeout_error_not_miscounted_as_gate_full`:provider 抛内建
  `TimeoutError` → `acquire` 返回 `True`(fail-open)而非 `False`;直接读
  `DELEGATIONS_GATED._value.get()` 前后对比,断言 counter 不增(与既有
  `test_sse_persistence.py` 同款 `._value.get()` 读法一致)。
- `test_provider_exception_uses_last_known_capacity`:先成功一次(容量 1)
  占满;再切 provider 恒抛异常;第二次 `acquire`(`timeout_s=0.05`)必须
  返回 `False` 而非立即 fail-open 的 `True`——证明用的是缓存容量 1(仍判定
  已满,等到超时才软失败),而不是「provider 一异常就整个不设限」。

其余全部既有 gate/工具测试(`test_third_acquire_waits_until_a_release_frees_a_slot`
等)不改断言,原样跑绿——热生效语义(provider 正常时每轮现读、下一次
`acquire` 立刻看到新容量)未变。

### 验证结果

```
$ DOCKER_HOST= uv run pytest services/orchestrator/tests/test_delegation_gate.py -x -q
24 passed in 1.24s

$ DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock uv run pytest services/orchestrator -q
1906 passed, 1 skipped in 24.56s
  (skip 是既有的 docx 模块缺失,与本次改动无关;
   test_runner_integration.py::test_postgres_checkpoint_persists_across_restart
   在无 DOCKER_HOST 时的 DockerException 在基线上原样复现,与本次改动无关)

$ uv run ruff check services/orchestrator
All checks passed!

$ uv run ruff format --check services/orchestrator
227 files already formatted

$ uv run mypy services/orchestrator/src
Success: no issues found in 83 source files
```

### 自审

- **锁外读+锁内比较不丢唤醒**:`wait()` 返回后仍持锁,`async with self._cond:`
  块结束才释放,循环回到 `while True` 顶部才在锁外重读容量、重新进锁再判——
  经典 re-check 模式,新容量不会因为提前退出锁而被错过。
- **`_read_capacity` 的 `except Exception` 与外层 `asyncio.timeout` 不冲突**:
  仔细核对了 `asyncio.timeout()` 的实现机制——超时到期时它 `task.cancel()`
  当前任务,在 `await self._capacity_provider()` 挂起点抛出的是
  `asyncio.CancelledError`(`BaseException` 子类,3.8+ 起不再继承
  `Exception`),不会被 `_read_capacity` 的 `except Exception` 吞掉;
  `CancelledError` 穿透到 `Timeout.__aexit__` 才被转换成 `TimeoutError`,
  外层 `except TimeoutError: return False` 接住。真正会被 `_read_capacity`
  捕获的是 **provider 自己**抛出的 `TimeoutError`(例如 provider 内部用
  `asyncio.wait_for` 或 asyncpg 驱动对查询做的超时),这正是 brief 要求
  「在这里捕获才不会被外层误判闸满」的场景——两者不会互相干扰。
- **`test_release_not_blocked_by_slow_provider` 的 RED 复现是真实的**:不是
  只凭推理断言应该失败,实测跑出 `0.150... < 0.1` 断言失败,数值(约等于
  provider 的 0.2s sleep 减去测试里 0.05s 的先导 sleep)与「release 被卡在
  等待者持锁读 provider 后面」的假设吻合。
- **`DELEGATIONS_GATED` 改公开名后确认无遗漏引用**:`grep -rn
  "_delegations_gated"` 全仓只剩 Prometheus 指标字符串本身
  (`"expert_work_delegations_gated_total"`,这是 metric name 不是 Python
  标识符,不受影响)。
- **未做但确认过不需要做**:没有改 `runtime.py` 里 `delegation_gate()` 方法
  或 `PlatformDelegationConfigService`——本轮 brief 明确限定改动范围在
  `_budget.py`/`spawn_worker.py`/`subagent.py`/`test_delegation_gate.py`
  四个文件,provider 本身的实现(DB 读、TTL 缓存)不在本轮修复范围内,
  fail-open 是在 `DelegationGate` 侧兜底,不依赖 provider 改造。
