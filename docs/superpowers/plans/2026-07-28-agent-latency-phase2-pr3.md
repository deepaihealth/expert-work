# Agent 延迟二期 PR3:稳健(subagent 全局闸 + sse 后台写)实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给委托(静态 subagent + spawn_worker)加进程级全局并发闸(带平台配置节),并把 run_event 持久化移出 SSE 流路径(有界队列 + 后台批写)。

**Architecture:** 闸体 = leaf 模块 `DelegationGate`(容量每次 acquire 时从 provider 现读,天然热生效),挂 `AgentRuntime` 单例,经 `run_agent → configurable → ToolContext` 注入(照 `worker_spawn_budget` 范式);配置节 `platform_delegation_config` 全栈照 #1029 dynamic-worker 模板。sse 侧 = 9 个帧发射点统一改「seq 同步预分配 + 入队」,per-run 有界队列 512 + 后台 writer 攒批写新 `RunEventStore.append_batch`。

**Tech Stack:** Python 3.12 / asyncio / FastAPI / SQLAlchemy async / LangGraph / React + antd + vitest / Playwright。

**Spec:** `docs/superpowers/specs/2026-07-27-agent-latency-phase2-design.md` PR3 节(113-134 行)。

## Global Constraints

- 分支 `perf-phase2-pr3`(基于 main `3b331df1`,PR2 已合)。
- 配置字段:`max_concurrent_delegations`,默认 **16**,界 **1-64**。
- 闸 acquire 超时 **30s** → 软失败 ToolResult(不抛异常,LLM 自行降级)。
- sse 队列上界 **512**,drop-oldest + counter;writer 攒批 **≤32 条或 100ms flush**;终态前 drain 超时 **5s**;**`asyncio.CancelledError` 路径零 await**。
- **token 帧照旧不入库**(`sse.py` `_publish_token` 设计不变)。
- **seq 必须在任何 await 之前同步分配**(`_publish_worker` sse.py:406-417 注释是铁律)。
- CI 门:`ruff check` 全库 + `ruff format --check`;CI-scope mypy(`uv run mypy packages services/{audit-backup-worker,billing-rollup-job,event-log-archive-job,orchestrator,retention-cleanup-job}/src`);orchestrator 测试 `DOCKER_HOST= uv run pytest`;control-plane pytest;admin-ui `pnpm exec vitest run src && pnpm typecheck && pnpm build`。
- 改 migration/SQL 本地跑 integration(`export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock`)。
- 提交信息 conventional commits,无 attribution。

## 并行波次(文件冲突分析)

| Task | 触碰文件域 | 冲突 |
|---|---|---|
| T1 sse 后台批写 | `services/orchestrator/src/orchestrator/sse.py`、`packages/expert-work-runtime/.../event_store.py`、双侧 tests | 与 T2 零交集 |
| T2 delegation 配置节全栈 | `packages/expert-work-persistence/*`、`packages/expert-work-protocol/audit.py`、control-plane 新文件 + `app.py` + `api/__init__.py`、admin-ui | 与 T1 零交集 |
| T3 闸体+注入链 | orchestrator `_budget.py/registry.py/builder.py/subagent.py/spawn_worker.py/sse.py(仅参数)`、control-plane `runtime.py/app.py/runs.py/run_queue_worker.py/trigger_firing.py/orphan_sweep.py` | 依赖 T1(sse.py 终态)+ T2(service) |

**Wave 1:T1 ∥ T2(worktree 并行);Wave 2:T3(合并 Wave 1 后串行)。**

---

### Task 1: sse updates 帧移出流路径(有界队列 + 后台批写)

**Files:**
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/runs/event_store.py`(ABC + SQL + in-memory 加 `append_batch`)
- Modify: `services/orchestrator/src/orchestrator/sse.py`
- Test: `packages/expert-work-runtime/tests/test_run_event_store.py`(append_batch 三实现)
- Test: `services/orchestrator/tests/test_sse_persistence.py`(现有 8 个测试全部要过 + 新队列测试)

**Interfaces:**
- Produces: `RunEventStore.append_batch(records: Sequence[RunEventRecord]) -> None`;sse.py 内部 `_enqueue_event(event_name: str, data: Any) -> None`(同步,内部预分配 seq)。
- 对外行为不变:replay 端点、bridge live 路径、帧内容与 seq 语义全部照旧。

**核心设计(实施者必读):**

1. **`append_batch`**(ABC 默认实现 = 循环 `append`;SQL 覆盖 = 一 session + `add_all` + 单 commit,照 `event_log/db.py:160-189` 的 `put_batch` 先例但**不需要** advisory lock/补号——seq 由生产端分配;in-memory 覆盖 = 复用现有查重逻辑逐条)。docstring 写明:任何一条撞 `(run_id, seq)` 整批失败,调用方(writer)按 H-7 吞掉 + counter。

2. **sse.py 改造**:`run_agent` 内、`event_seq` 种子(sse.py:359-369)之后建:

```python
# 二期 PR3 — run_event 持久化移出流路径(spec PR3 Task 2)。
# 主循环每帧只做「seq 同步预分配 + put_nowait」;后台 writer 攒批
# (≤_PERSIST_BATCH_MAX 条或 _PERSIST_FLUSH_INTERVAL_S)写 append_batch。
# 队满 drop-oldest(H-7 立场:调试台 replay 可容忍缺帧,live SSE 不能慢)。
persist_queue: asyncio.Queue[RunEventRecord | None] = asyncio.Queue(maxsize=_PERSIST_QUEUE_MAX)

def _enqueue_event(event_name: str, data: Any) -> None:
    nonlocal event_seq
    if event_store is None:
        return
    seq = event_seq          # 同步分配——见 _publish_worker 注释(铁律)
    event_seq += 1
    record_ = make_event_record(run_id=run_id, seq=seq, event_name=event_name, data=data)
    try:
        persist_queue.put_nowait(record_)
    except asyncio.QueueFull:
        try:
            persist_queue.get_nowait()   # drop-oldest
            persist_queue.task_done()
        except asyncio.QueueEmpty:
            pass
        _run_event_queue_dropped.labels(event_name=event_name).inc()
        persist_queue.put_nowait(record_)

writer_task = asyncio.create_task(
    _persist_writer(event_store, persist_queue, run_id=run_id)
)
_BACKGROUND_PERSIST_WRITERS.add(writer_task)
writer_task.add_done_callback(_BACKGROUND_PERSIST_WRITERS.discard)
```

   注意:`event_store is None` 时 `_enqueue_event` 直接返回,但 **`event_seq` 不再递增也无妨**(无 store 就没有 seq 消费者;与现状 `_persist_event` 的 None 早退一致,但现状仍递增——保持递增更稳妥:把 `if event_store is None: return` 放在 seq 分配**之后**,行为与现状完全一致)。**采用后者:先分配 seq 再判 None。**

3. **模块级 writer**(放 sse.py 模块尾部,`_BACKGROUND_CLEANUP_TASKS` 旁):

```python
# 二期 PR3 — run_event 后台批写 writer。sentinel(None)= 收尾:flush 余量后退出。
_PERSIST_QUEUE_MAX: Final = 512
_PERSIST_BATCH_MAX: Final = 32
_PERSIST_FLUSH_INTERVAL_S: Final = 0.1
_PERSIST_DRAIN_TIMEOUT_S: Final = 5.0
_BACKGROUND_PERSIST_WRITERS: set[asyncio.Task[None]] = set()

_run_event_queue_dropped = expert_work_counter(
    "expert_work_run_event_queue_dropped_total",
    "Frames dropped from the run_event persist queue (drop-oldest on overflow).",
    ("event_name",),
)


async def _persist_writer(
    event_store: RunEventStore,
    queue: asyncio.Queue[RunEventRecord | None],
    *,
    run_id: UUID,
) -> None:
    """Drain ``queue`` in batches into ``event_store.append_batch``.

    H-7 立场:batch 写失败 → counter + warning,继续下一批;绝不向上抛。
    收到 sentinel(None)→ flush 剩余 → 退出。
    """
    batch: list[RunEventRecord] = []
    stopping = False
    while not stopping:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=_PERSIST_FLUSH_INTERVAL_S)
        except TimeoutError:
            item = _NO_ITEM
        if item is _NO_ITEM:
            pass
        elif item is None:
            stopping = True
            queue.task_done()
        else:
            # task_done 延迟到 flush 之后——drain 的 queue.join() 语义必须是
            # 「帧已落库(或已尽力)」,不是「writer 已拿走」。在 get 时就
            # task_done 会让 join() 在批未 flush 时假完成,终态先于帧落库。
            batch.append(item)
        if batch and (len(batch) >= _PERSIST_BATCH_MAX or item is _NO_ITEM or stopping):
            await _flush_batch(event_store, batch, run_id=run_id)
            for _ in batch:
                queue.task_done()
            batch = []
    # sentinel 后队列可能仍有余量(CancelledError 路径 put_nowait 竞态)——清空
    tail_count = 0
    while True:
        try:
            tail = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        tail_count += 1
        if tail is not None:
            batch.append(tail)
    if batch:
        await _flush_batch(event_store, batch, run_id=run_id)
    for _ in range(tail_count):
        queue.task_done()


async def _flush_batch(
    event_store: RunEventStore, batch: list[RunEventRecord], *, run_id: UUID
) -> None:
    try:
        await event_store.append_batch(batch)
        for rec in batch:
            _run_event_persist_total.labels(event_name=rec.event_name).inc()
    except Exception as exc:
        for rec in batch:
            _run_event_persist_errors.labels(event_name=rec.event_name).inc()
        logger.warning(
            "run_event.batch_persist_failed run_id=%s count=%s err=%s",
            run_id, len(batch), exc,
        )
```

   `_NO_ITEM: Final = object()` 模块级 sentinel(区分「超时无货」与「关闭」)。类型上用 `item: RunEventRecord | None | object`,mypy 走 `is` 窄化。

4. **9 个发射点替换**(全部 `await _persist_event(...)` + `event_seq += 1` → 一行 `_enqueue_event(name, data)`;`bridge.publish` 照旧 await):compaction(384-391)/worker(414-417,删手工预分配,统一走 helper)/guard(424-427)/metadata(447-454)/updates 主循环(517-524)/retry(549-556)/approval(628-635)/error×2(712-719、745-752)。`_persist_event` 函数与 `_run_event_persist_*` counter 保留(writer 用 counter;`_persist_event` 若无引用则删,连带更新 docstring 引用)。

5. **drain + 收尾**:
   - 新 helper(run_agent 闭包内):
     ```python
     async def _drain_persist_queue() -> None:
         if event_store is None:
             return
         try:
             async with asyncio.timeout(_PERSIST_DRAIN_TIMEOUT_S):
                 await persist_queue.join()
         except TimeoutError:
             logger.warning("run_event.drain_timeout run_id=%s pending=%s",
                            run_id, persist_queue.qsize())
     ```
   - 在**每个 `set_status` 终态调用之前** `await _drain_persist_queue()`:正常/PAUSED(605 前)、RunCancelledError(661-683)、MaxSteps(695-736,error 帧入队后)、generic(737-769,同)。
   - `finally`(770-793)内:`persist_queue.put_nowait(None)`(同步,CancelledError 路径也安全;QueueFull 时 drop-oldest 同款腾位再 put)。writer 在强引用集合里自行收尾。
   - **CancelledError 分支(684-694)不加任何 await**(现状注释明言 teardown await 不可靠)。

6. **HA resume**:`next_seq` 种子逻辑(359-369)不动;writer 启动在种子之后,天然安全。

**Steps:**

- [ ] **Step 1(RED):`append_batch` 测试** —— `packages/expert-work-runtime/tests/test_run_event_store.py` 加:in-memory/SQL(testcontainers)各测「一次 append_batch N 条 → list 全出、顺序对」「批内撞 seq → 抛(SQL IntegrityError/in-memory ValueError)且(SQL)整批回滚」「空批 no-op」。`DOCKER_HOST= uv run pytest packages/expert-work-runtime/tests/test_run_event_store.py -x` 预期 FAIL(方法不存在)。
- [ ] **Step 2(GREEN):实现 `append_batch`** 三处(ABC 默认循环 + SQL add_all 单 commit + in-memory 循环复用查重)。测试转绿。
- [ ] **Step 3(RED):sse 队列测试** —— `services/orchestrator/tests/test_sse_persistence.py` 加(复用 `_ScriptedGraph`/`_drain` 现有 helper,drain 断言前 `await asyncio.gather(*_BACKGROUND_PERSIST_WRITERS)` 或等 store 有货):
  - `test_frames_persist_via_background_writer`:跑一个 run,终态后 store 里 metadata+updates 帧齐、seq 连续无重复。
  - `test_queue_overflow_drops_oldest_and_counts`:塞 >512 帧(缩小常量不可行——monkeypatch `_PERSIST_QUEUE_MAX` 不生效因 Queue 已建;改为测试里直接构造小队列调 `_enqueue_event` 的等价逻辑,或 monkeypatch `orchestrator.sse._PERSIST_QUEUE_MAX` 后再进 run_agent——**常量在 run_agent 内被读取,monkeypatch 模块属性即可生效**);断言 dropped counter 增加、store 里是**最新**的帧。
  - `test_terminal_status_waits_for_drain`:store 的 append_batch 打 50ms 延迟,断言 `set_status` 时刻(spy)晚于最后一帧落库。
  - `test_cancelled_run_does_not_await_drain`:图里抛 `asyncio.CancelledError`,断言 run_agent 及时 re-raise(不被 drain 阻塞),且 writer 事后仍把已入队帧写完(gather 强引用集合后 store 有货)。
  - `test_store_append_failure_does_not_block_sse`(现有 :236)改造:失败注入移到 `append_batch`,断言 SSE 流照常 + errors counter。
  - 现有其余 7 测试跑通(帧内容/顺序契约不变,但需在断言前加 writer drain 等待——统一加个 `_await_writers()` helper)。
  预期 FAIL。
- [ ] **Step 4(GREEN):sse.py 改造**(按核心设计 2-6)。`DOCKER_HOST= uv run pytest services/orchestrator/tests/test_sse_persistence.py services/orchestrator/tests/test_sse_worker_events.py services/orchestrator/tests/test_sse_guard_events.py services/orchestrator/tests/test_sse.py -x` 全绿。
- [ ] **Step 5:全量门**:orchestrator 全测 + runtime 包全测 + `ruff check` + `ruff format --check` + CI-scope mypy。
- [ ] **Step 6:Commit** `perf(orchestrator): run_event 持久化移出 SSE 流路径(有界队列+后台批写)`

---

### Task 2: platform_delegation_config 配置节全栈(照 #1029 模板)

**Files(模板 → 本任务映射;模板文件全部真实存在,照抄改名):**

| 模板(dynamic_worker) | 新建(delegation) |
|---|---|
| `packages/expert-work-persistence/src/expert_work/persistence/models/platform_dynamic_worker_config.py` | `.../models/platform_delegation_config.py` |
| `.../migrations/versions/0124_platform_dynamic_worker.py` | `.../versions/<next>_platform_delegation.py`(先 `ls migrations/versions/ | sort | tail -1` 取下一号) |
| `.../persistence/platform_dynamic_worker_config/{__init__,base,memory,sql}.py` | `.../persistence/platform_delegation_config/{__init__,base,memory,sql}.py` |
| `.../tests/test_platform_dynamic_worker_config_store.py` | `.../tests/test_platform_delegation_config_store.py` |
| `services/control-plane/src/control_plane/platform_dynamic_worker_config.py` | `.../platform_delegation_config.py` |
| `.../tests/test_platform_dynamic_worker_config_service.py` | `.../tests/test_platform_delegation_config_service.py` |
| `.../api/platform_dynamic_worker_config.py` | `.../api/platform_delegation_config.py` |
| `.../tests/test_platform_dynamic_worker_config_api.py` | `.../tests/test_platform_delegation_config_api.py` |
| `apps/admin-ui/src/api/platform_dynamic_worker_config.ts` | `.../api/platform_delegation_config.ts` |
| `.../settings_platform/PlatformDynamicWorkerSection.tsx` | `.../settings_platform/PlatformDelegationSection.tsx` |
| `.../__tests__/PlatformDynamicWorkerSection.test.tsx` | `.../__tests__/PlatformDelegationSection.test.tsx` |
| `apps/admin-ui/e2e/platform-dynamic-worker.spec.ts` | `apps/admin-ui/e2e/platform-delegation.spec.ts` |

**Modify(照模板的落点,行号为侦察时值):**
- `packages/expert-work-persistence/src/expert_work/persistence/models/__init__.py`(import + `__all__`)
- `packages/expert-work-protocol/src/expert_work/protocol/audit.py:367` 后加:
  ```python
  # platform delegation-gate config (perf phase2 PR3) — system_admin-only write
  # to the platform max_concurrent_delegations row.
  PLATFORM_DELEGATION_UPDATED = "platform_delegation_config:updated"
  ```
- `services/control-plane/src/control_plane/api/__init__.py`(import + `__all__`)
- `services/control-plane/src/control_plane/app.py`:store import(:339 旁)、router import(:67 旁)、service import(:164 旁)、store 解析 + service 构造(:888-900 旁,同款 in-memory/SQL 双路)、`app.state.platform_delegation_config_service = ...`(:2082 旁)、`include_router`(:2278 旁)、`_SqlStores` Protocol 字段(:2334 旁)、SQL 装配(:2558 旁)。**注意:runtime 接线(`resolved_agent_runtime.delegation_*`)不在本任务——T3 做。**
- `apps/admin-ui/src/pages/SettingsPlatformConfig.tsx`(import + cost tab `<Space>` 内 `PlatformDynamicWorkerSection` 之后挂 `<PlatformDelegationSection />`)
- `apps/admin-ui/src/i18n/locales/en.ts`(接口块 + 值块)、`zh-CN.ts`(值块)——**先 grep 确认键名不撞既有**(历史坑:同 object 重复键 esbuild 静默覆盖)。

**Interfaces:**
- Produces(T3 消费):
  ```python
  @dataclass(frozen=True)
  class DelegationConfig:
      max_concurrent_delegations: int

  class PlatformDelegationConfigService:
      def __init__(self, *, store: PlatformDelegationConfigStore,
                   env_default: DelegationConfig,
                   ttl_seconds: float = 30.0,
                   clock: Callable[[], float] = time.monotonic) -> None: ...
      async def effective(self) -> DelegationConfig: ...
      async def configured(self) -> DelegationConfig | None: ...
      async def put(self, *, max_concurrent_delegations: int, updated_by: str | None) -> None: ...
      def invalidate(self) -> None: ...
  ```
  (照 `platform_dynamic_worker_config.py` 的 service 逐行改字段;TTL 30s 双检锁照抄。)
- Store row:`PlatformDelegationConfigRow(max_concurrent_delegations: int, updated_by: str | None)`;store ABC `get()/put(*, max_concurrent_delegations, updated_by)`。
- API:`/v1/platform/delegation-config` GET/PUT,`PlatformDelegationConfigWrite` 单字段 `max_concurrent_delegations: int = Field(ge=1, le=64)`,`extra="forbid"`,system_admin-only(403 `PLATFORM_SCOPE_FORBIDDEN`),PUT 审计 `PLATFORM_DELEGATION_UPDATED`(resource_type=`platform_credential`, resource_id=`delegation-config`)。
- 前端:testid 前缀 `pdg-`(`pdg-loading`/`pdg-load-error`/`pdg-root`),单 InputNumber(1-64)+ 保存按钮;i18n 键 `platformDelegation.*`。
- migration:单行表 `platform_delegation_config`(id 恒 1 CHECK,照 0124 模板),默认无行(env_default 兜底)。

**Steps:**

- [ ] **Step 1(RED→GREEN)persistence 层**:先写 store 测试(get 空/put-get 回读/覆写)照模板测试文件改名改字段;实现 models + migration + store 四件套。`DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock uv run pytest packages/expert-work-persistence/tests/test_platform_delegation_config_store.py -x` 绿(SQL 分支走 testcontainers)。
- [ ] **Step 2(RED→GREEN)service**:测试(env_default 兜底/DB-wins/TTL 过期重读/put 后 invalidate 立即生效)照模板;实现 service。control-plane 该测试文件绿。
- [ ] **Step 3(RED→GREEN)API + 审计**:测试(GET 空时吐 effective/PUT 写库+审计落库/非 system_admin 403/越界 422)照模板;实现 router + audit 枚举 + app.py 全部接线。`uv run pytest services/control-plane/tests/test_platform_delegation_config_api.py services/control-plane/tests/test_platform_delegation_config_service.py -x` 绿。
- [ ] **Step 4(RED→GREEN)admin-ui**:组件测试(加载态/保存调 PUT/422 报错)照模板;实现 api client + Section 组件 + 挂载 + i18n 三处 + e2e spec 照 `platform-dynamic-worker.spec.ts`。`pnpm exec vitest run src/pages/settings_platform && pnpm typecheck` 绿。
- [ ] **Step 5:全量门**:control-plane pytest 全量 + `ruff check` 全库 + `ruff format --check` + CI-scope mypy + `pnpm exec vitest run src && pnpm build`。
- [ ] **Step 6:Commit** `feat(control-plane): platform_delegation_config 配置节全栈(委托并发闸容量,默认16界1-64)`

---

### Task 3: DelegationGate 闸体 + 两工具过闸 + 注入链接线

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/_budget.py`(加 `DelegationGate`,leaf 模块)
- Modify: `services/orchestrator/src/orchestrator/tools/registry.py`(ToolContext 加字段)
- Modify: `services/orchestrator/src/orchestrator/tools/subagent.py`(call 过闸)
- Modify: `services/orchestrator/src/orchestrator/tools/spawn_worker.py`(call 过闸,叠加于 per-run budget)
- Modify: `services/orchestrator/src/orchestrator/graph_builder/builder.py:2699` 旁(`_build_tool_context` 读 configurable)
- Modify: `services/orchestrator/src/orchestrator/sse.py:292,334-346`(run_agent 形参 + configurable 注入)
- Modify: `services/control-plane/src/control_plane/runtime.py`(AgentRuntime 字段 + `delegation_gate` 惰性单例)
- Modify: `services/control-plane/src/control_plane/app.py:1373` 旁(lifespan 接线 service)
- Modify: `services/control-plane/src/control_plane/api/runs.py:747,897`、`run_queue_worker.py:297`、`trigger_firing.py:296`、`orphan_sweep.py:300`(5 调用点传 gate)
- Test: `services/orchestrator/tests/test_delegation_gate.py`(新)
- Test: `services/control-plane/tests/test_delegation_gate_wiring.py`(新)

**Interfaces:**
- Consumes: T2 的 `PlatformDelegationConfigService.effective() -> DelegationConfig`。
- Produces: `DelegationGate`(见下,完整代码);`ToolContext.delegation_gate: DelegationGate | None = None`;`run_agent(..., delegation_gate: DelegationGate | None = None)`;configurable key 常量 `DELEGATION_GATE_KEY = "delegation_gate"`(放 `_budget.py`)。

**闸体完整代码**(`_budget.py`,`WorkerSpawnBudget` 之后):

```python
DELEGATION_GATE_KEY: Final = "delegation_gate"

# 二期 PR3(spec P4)— 进程级委托并发闸。容量每次 acquire 时经
# capacity_provider 现读(provider 内部是 30s TTL 的配置服务),配置
# 热生效语义 = 对下一次委托生效,不影响已在闸内的。单进程部署下
# 真闸得住;HA 双色同活时每实例一闸(与本仓多副本 TTL 兜底同一立场)。
class DelegationGate:
    """Process-wide concurrency gate for delegations (subagent + spawn_worker).

    ``acquire`` waits up to ``timeout_s`` for a slot; returns False on
    timeout (caller degrades to a soft-fail ToolResult — never raises, so a
    depth-1 delegation holding all slots cannot deadlock its own depth-2).
    """

    def __init__(
        self,
        capacity_provider: Callable[[], Awaitable[int]],
        *,
        timeout_s: float = 30.0,
    ) -> None:
        self._capacity_provider = capacity_provider
        self._timeout_s = timeout_s
        self._active = 0
        self._cond = asyncio.Condition()

    @property
    def active(self) -> int:
        return self._active

    async def acquire(self) -> bool:
        try:
            async with asyncio.timeout(self._timeout_s):
                async with self._cond:
                    while True:
                        capacity = max(1, int(await self._capacity_provider()))
                        if self._active < capacity:
                            self._active += 1
                            return True
                        await self._cond.wait()
        except TimeoutError:
            return False

    async def release(self) -> None:
        async with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify_all()
```

   注意:`_cond.wait()` 释放锁等 notify;`capacity_provider` 在锁内 await——provider 是内存 TTL 读,纳秒级,可接受(docstring 写明勿挂慢 provider)。`asyncio.timeout` 包住整个等待(含 provider 读)。

**两工具过闸**(deadline 检查之后、`run_child_to_result` 之前;spawn_worker 在 per-run budget 检查**之后**叠加):

```python
gate = ctx.delegation_gate
if gate is not None and not await gate.acquire():
    _delegations_gated.inc()  # expert_work_counter,两文件各自或共用 _budget 放一个
    return ToolResult(
        content=(
            "[delegation refused: platform-wide delegation concurrency is "
            "saturated; retry later or complete the work without delegating]"
        ),
        meta={"delegation_gated": True, "reason": "global_gate_timeout"},
    )
try:
    ... # 原 run_child_to_result 调用(spawn_worker 保留 _maybe_concurrency 包裹)
finally:
    if gate is not None:
        await gate.release()
```

   counter 定义放 `_budget.py`:`_delegations_gated = expert_work_counter("expert_work_delegations_gated_total", "Delegations refused by the global concurrency gate (acquire timeout).")`——确认 `_budget.py` 引入 observability 不成环(它是 leaf,common 包无 orchestrator 依赖,安全)。

**control-plane 接线**(照 `new_worker_spawn_budget` 范式):
- `runtime.py` AgentRuntime 加字段:
  ```python
  delegation_config_service: PlatformDelegationConfigService | None = None
  _delegation_gate: Any = field(default=None, repr=False)  # DelegationGate 惰性单例

  def delegation_gate(self) -> Any | None:
      """进程级闸惰性单例;未接配置服务时返回 None(闸关闭,与现状一致)。"""
      if self.delegation_config_service is None:
          return None
      if self._delegation_gate is None:
          from orchestrator.tools._budget import DelegationGate
          service = self.delegation_config_service
          async def _capacity() -> int:
              return (await service.effective()).max_concurrent_delegations
          self._delegation_gate = DelegationGate(_capacity)
      return self._delegation_gate
  ```
- `app.py:1373` 旁:`resolved_agent_runtime.delegation_config_service = resolved_platform_delegation_config_service`(T2 已构造该 service)。
- 5 调用点各加 `delegation_gate=runtime.delegation_gate(),`(runs.py×2/run_queue_worker/trigger_firing/orphan_sweep,形如 `worker_spawn_budget=` 邻行)。
- `sse.py`:形参 `delegation_gate: Any | None = None`(:292 旁;类型用 orchestrator 侧真类型 `DelegationGate | None`,从 `_budget` import)+ configurable 注入(:345 旁,用 `DELEGATION_GATE_KEY` 常量)。
- `builder.py:2699` 旁:`delegation_gate = configurable.get(DELEGATION_GATE_KEY)` → ToolContext。

**Steps:**

- [ ] **Step 1(RED):闸体单测** `services/orchestrator/tests/test_delegation_gate.py`:容量2三并发第三个等/release 后放行/超时返回 False 不抛/capacity_provider 改值下一次 acquire 生效(热生效)/嵌套不死锁(容量1,depth1 占闸内再 acquire → 30s 超时软失败——测试用 timeout_s=0.05)/release 通知等待者。`DOCKER_HOST= uv run pytest services/orchestrator/tests/test_delegation_gate.py -x` FAIL。
- [ ] **Step 2(GREEN):`DelegationGate` 实现**(上面代码)。绿。
- [ ] **Step 3(RED):工具过闸测试**(同文件加):FakeGate(记录 acquire/release 次数)注入 ToolContext,`SubAgentTool.call`/`SpawnWorkerTool.call` 成功路径 acquire+release 各1次、gate 拒绝时返回软失败 ToolResult(`meta["delegation_gated"]`)且**不调用** run_child_to_result、子 run 抛异常时 release 仍执行(finally)、`delegation_gate=None` 时行为与现状全等。参照 `services/orchestrator/tests/` 里现有 SubAgentTool 测试的桩法(grep `SubAgentTool(` in tests)。FAIL。
- [ ] **Step 4(GREEN):两工具 + ToolContext + builder + sse.py 注入**。绿;`test_sse*.py` 回归全绿。
- [ ] **Step 5(RED→GREEN):control-plane 接线测试** `test_delegation_gate_wiring.py`:`runtime.delegation_gate()` 无 service 返 None/有 service 返单例(两次调用同一对象)/capacity 跟随 service.effective;经 `agent_fixtures` 起 app 后 `app.state.agent_runtime.delegation_gate()` 非 None(照 `test_credential_cache_wiring.py` 的既有 app fixture 范式)。实现接线(runtime.py + app.py + 5 调用点)。`uv run pytest services/control-plane/tests/test_delegation_gate_wiring.py -x` 绿。
- [ ] **Step 6:全量门**:orchestrator 全测(`DOCKER_HOST= uv run`)+ control-plane 全测 + ruff 两道 + CI-scope mypy。**注意历史坑:改 run_agent 签名要跑 control-plane 测试(经真 run_agent 的路径)**。
- [ ] **Step 7:Commit** `feat(orchestrator): 委托全局并发闸(DelegationGate 30s 超时软失败+配置热生效)`

---

## 验证(整 PR)

- 并发闸:闸满排队/超时软失败/嵌套不死锁/配置热生效(单测覆盖,Step 1/3/5)。
- sse 队列:drop 计数/drain 超时/seq 唯一性/取消路径不挂(Task 1 Step 3)。
- e2e:`platform-delegation.spec.ts` 照 dynamic-worker 模板(Task 2 Step 4)。
- 真栈冒烟(合并前,Wave 3):`make -C infra dev-up` 栈上 PUT `/v1/platform/delegation-config` `{"max_concurrent_delegations": 1}` → 起一个带委托的 run 验证第二个委托被闸(观察 `expert_work_delegations_gated_total`)→ 归位 16;bench 一轮确认入口链无回归(对照 `tools/bench/baselines/2026-07-27-phase2-pr2-after.yaml`)。

## 风险与对策(spec 摘录 + 本 plan 补充)

| 风险 | 对策 |
|---|---|
| 全局闸嵌套死锁 | acquire 30s 超时 → 软失败 ToolResult(Task 3 Step 1 有专测) |
| sse 后台写让 replay 窗口不完整 | 终态前 5s drain;前端已降级;丢帧仅影响调试台 |
| CancelledError 路径 await | 该分支零改动;sentinel 用 put_nowait(同步) |
| seq 撞主键 | 所有 9 点统一 helper 内同步预分配(消灭现存「后置递增」混用) |
| 三 PR 互相踩 | 本 PR 基于 PR2 已合的 main `3b331df1` |
