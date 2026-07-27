# Agent 延迟优化二期 PR1 — 提速 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 入口链四项提速(bump_access 后台化 / tenant_config 走已有缓存 / workspace_ingest 并行 / guards 全关跳 64 字符缓冲)+ bench 场景扩展到 8 段全亮 + verify_reads on/off 数据。

**Architecture:** 全部改动在 orchestrator 热路径与 control-plane 装配层;不新增任何缓存实现(P1.2 复用 `TenantConfigService` 现有 60s 缓存);不改任何安全默认值。bench 扩展让每项收益可量。

**Tech Stack:** Python 3.12 / LangGraph / pytest(orchestrator 测试须 `DOCKER_HOST= uv run`)。

## Global Constraints

- spec:`docs/superpowers/specs/2026-07-27-agent-latency-phase2-design.md`(PR1 节)
- CI 门:`uv run ruff check .`(全库)+ `uv run ruff format --check .`(独立步骤,加 with/if 块缩进变化必触发)+ CI-scope mypy:`uv run mypy packages services/audit-backup-worker/src services/billing-rollup-job/src services/event-log-archive-job/src services/orchestrator/src services/retention-cleanup-job/src`
- orchestrator 测试:`cd` 仓库根后 `DOCKER_HOST= uv run pytest services/orchestrator/tests/ -x -q`(禁用 docker 相关误连);control-plane 测试:`uv run pytest services/control-plane/tests/ -x -q`
- **不动**:`TRACED_SPANS` / `_SPAN_LABELS` / trace facade 序列化(一期 parity 测锁定);`HOLD_CHARS=64` 的值;`output_screen` 默认 `"block"`;`verify_reads` 默认 `True`(Task 6 拿数字后由用户拍板,不在本计划内改)
- bench 脚本/manifest 不进 CI 测试范围之外的门(`tools/bench` 非包,tests 由 `tools/bench/conftest.py` 驱动,CI pytest 范围已含)
- 测试同步跑,禁止后台化 + 轮询等待

---

### Task 1: bench 场景扩展(seed 记忆 + manifest 入仓 + verify 指标)

**Files:**
- Create: `tools/bench/manifests/bench-entry.yaml`
- Create: `tools/bench/prompts/seed.txt`
- Modify: `tools/bench/entry_latency.py`
- Modify: `tools/bench/README.md`
- Test: `tools/bench/test_entry_latency.py`

**Interfaces:**
- Produces: `VERIFY_MS_KEY = "__verify_ms__"`(内部聚合键,YAML 顶层输出节 `verify_ms`)、`--seed-prompt-file` CLI 选项、`seed_memories()` 协程。
- 后续 Task 6 用本 task 的 manifest + seed 流程跑真栈数据。

**背景(实施者需知):**
- 现基线只亮 8 段中 5 段:固定 prompt 零召回结果(rerank/bump_access/verify 不触发)、无持久工作区(workspace_ingest 不存在)。
- 记忆预埋没有创建 API(`/v1/memory` 只有 GET/PATCH/DELETE/correct)——唯一入口是跑一轮种子对话让 run 末的 `memory_writeback` 节点落库(run 终态 success 即写完)。记忆是 (tenant, user, agent) 维度,跨 session 可召回,所以 seed 用独立 session 即可。
- verify 的 span 在 trace facade 输出中是 `kind == "llm"`、`label == "记忆校验"`、`group == None`(facade `_LLM_LABELS` 定死),**不在** `group == "entry"` 里——按 label 抓,照 `FIRST_LLM_START_KEY` 的「内部键 + 写出时 pop 成顶层节」范式。
- `tools/bench/prompts/fixed.txt` **不改**(问「新加入团队的后端工程师第一周该了解哪些系统组件和开发流程」)——seed 记忆的内容要与它语义重叠,召回才非空。

- [ ] **Step 1: 写失败测试(verify 指标抓取)**

在 `tools/bench/test_entry_latency.py` 追加:

```python
def test_extract_run_metrics_captures_verify_llm_span_by_label() -> None:
    """verify_reads 开着时 facade 输出 label=「记忆校验」的 llm span
    (group=None,不在 entry 组)——按 label 抓成独立指标,不混进 segments。"""
    trace = {
        "status": "ok",
        "spans": [
            {"label": "记忆召回", "group": "entry", "latencyMs": 120, "kind": "span"},
            {"label": "记忆校验", "group": None, "latencyMs": 350, "kind": "llm", "startMs": 400},
            {"label": "LLM 调用", "group": None, "latencyMs": 900, "kind": "llm", "startMs": 800},
        ],
    }
    metrics = extract_run_metrics(trace)
    assert metrics[VERIFY_MS_KEY] == 350.0
    assert "记忆校验" not in metrics  # 不以 label 名混进 segments


def test_write_result_emits_verify_section_when_present(tmp_path: Path) -> None:
    out_path = tmp_path / "baseline.yaml"
    per_run = [{"记忆召回": 100.0, VERIFY_MS_KEY: 300.0}, {"记忆召回": 120.0, VERIFY_MS_KEY: 340.0}]
    _write_result(out_path, per_run, {"agent": "x@1", "runs": 2}, failed_runs=0)
    written = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    assert written["verify_ms"]["median"] == 320.0
    assert "__verify_ms__" not in written["segments"]
```

import 行加 `VERIFY_MS_KEY`。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tools/bench/test_entry_latency.py -x -q`
Expected: FAIL — `ImportError: cannot import name 'VERIFY_MS_KEY'`

- [ ] **Step 3: 实现 verify 指标**

`tools/bench/entry_latency.py`:

在 `FIRST_LLM_START_KEY` 之后加:

```python
#: 同范式的第二个内部键 —— verify_reads 开着时 facade 输出的「记忆校验」
#: llm span(group=None,不在 entry 组)。写出时 pop 成顶层 ``verify_ms`` 节。
#: 二期 P1.4:verify on/off 两组基线的对照数据源。
VERIFY_MS_KEY = "__verify_ms__"

_VERIFY_SPAN_LABEL = "记忆校验"  # trace_facade.py _LLM_LABELS 的固定中文标签
```

`extract_run_metrics` 的 for 循环里,`if span.get("kind") == "llm":` 分支扩为:

```python
        if span.get("kind") == "llm":
            start_ms = span.get("startMs")
            if isinstance(start_ms, int | float):
                llm_starts.append(float(start_ms))
            latency_ms = span.get("latencyMs")
            if span.get("label") == _VERIFY_SPAN_LABEL and isinstance(latency_ms, int | float):
                metrics[VERIFY_MS_KEY] = float(latency_ms)
```

`_write_result` 里 `first_llm_start = aggregated.pop(...)` 之后加:

```python
    verify_ms = aggregated.pop(VERIFY_MS_KEY, None)
```

`result` 组装后(`first_llm_start` 写出块之后):

```python
    if verify_ms is not None:
        result["verify_ms"] = {"median": verify_ms.median, "p95": verify_ms.p95, "n": verify_ms.n}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tools/bench/test_entry_latency.py -x -q`
Expected: PASS(全部,含既有)

- [ ] **Step 5: 写失败测试(seed 轮)**

追加:

```python
async def test_seed_round_runs_one_priming_conversation() -> None:
    """--seed-prompt-file 只跑一轮独立 session 的种子对话(让 writeback 落
    库),校验 run 终态 success,不拉 trace、不计入 bench 数据。"""
    seen = {"sessions": 0, "runs": 0, "trace_gets": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sessions":
            seen["sessions"] += 1
            return httpx.Response(200, json={"data": {"thread_id": f"t-{seen['sessions']}"}})
        if request.method == "POST" and request.url.path.endswith("/runs"):
            seen["runs"] += 1
            return _mock_run_response("33333333-3333-3333-3333-333333333333")
        if request.method == "GET" and "/runs/" in request.url.path and not request.url.path.endswith("/trace"):
            return httpx.Response(200, json={"data": {"status": "success"}})
        if request.url.path.endswith("/trace"):
            seen["trace_gets"] += 1
            return httpx.Response(200, json={"status": "ok", "spans": []})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        await seed_memories(client, agent_name="bench-entry", agent_version="2.0.0", prompt="记住这些")

    assert seen["sessions"] == 1
    assert seen["runs"] == 1
    assert seen["trace_gets"] == 0  # 种子轮永不拉 trace
```

import 行加 `seed_memories`。

- [ ] **Step 6: 跑测试确认失败**

Run: `uv run pytest tools/bench/test_entry_latency.py::test_seed_round_runs_one_priming_conversation -x -q`
Expected: FAIL — ImportError

- [ ] **Step 7: 实现 seed**

`entry_latency.py` 在 `run_rounds` 前加:

```python
async def seed_memories(
    client: httpx.AsyncClient, *, agent_name: str, agent_version: str, prompt: str
) -> None:
    """跑一轮独立 session 的种子对话,让 run 末的 memory_writeback 落库。

    记忆是 (tenant, user, agent) 维度、跨 session 可召回,所以种子轮用
    独立 session,不污染 bench session 的对话历史。run 终态 success 即
    writeback 已完成(它是 graph 的 end 前节点)。失败直接抛 —— 种子没
    种上,后面所有轮的召回都是空的,数据全白跑,fail-fast 是正确行为。
    """
    thread_id = await _create_session(client, agent_name, agent_version)
    print(f"seed session: {thread_id}", file=sys.stderr)
    await _run_once(client, thread_id, prompt)
```

`_amain` 里,建完 client、`_create_session` 之前:

```python
        if args.seed_prompt_file:
            seed_prompt = Path(args.seed_prompt_file).read_text(encoding="utf-8").strip()
            if not seed_prompt:
                raise SystemExit(f"{args.seed_prompt_file} is empty")
            await seed_memories(
                client, agent_name=agent_name, agent_version=agent_version, prompt=seed_prompt
            )
```

（注意:`agent_name/agent_version` 的解析在 client 块之前已存在。）

`main()` 加参数:

```python
    parser.add_argument(
        "--seed-prompt-file",
        default=None,
        help="optional: run one priming conversation first so recall is non-empty",
    )
```

- [ ] **Step 8: 跑测试确认通过**

Run: `uv run pytest tools/bench/test_entry_latency.py -x -q`
Expected: PASS

- [ ] **Step 9: manifest + seed prompt 入仓**

`tools/bench/prompts/seed.txt`(内容与 fixed.txt 的问题语义重叠,召回才命中):

```
请记住我们团队的这些事实,后面会用到:后端主要组件是 control-plane(FastAPI,负责 API 与租户管理)、orchestrator(LangGraph,负责 Agent 运行)、PostgreSQL 17(主库)和 Keycloak(登录);开发流程是主干开发,每个改动走 PR 评审,CI 全绿才允许合并,发布用蓝绿部署。新人第一周需要先读 getting-started 文档并在本地把 docker compose 栈跑起来。
```

`tools/bench/manifests/bench-entry.yaml` —— 结构照 `manifests/canonical-agent/v1.0.0.yaml` 抄(实施时打开对照,尤其 `system_prompt: {template: ...}` 是 dict 不是裸字符串),要点字段:

```yaml
name: bench-entry
version: "2.0.0"
# 2.0.0 = 二期扩展场景(1.0.0 是一期未入仓的手工版):开持久工作区 + 长期
# 记忆全链(write_back + rerank + verify),8 个入口链段全部可触发。
spec:
  model: { ... 照 canonical-agent 的 model 块 ... }
  system_prompt:
    template: "你是团队知识助手,回答成员关于系统与流程的问题。"
  memory:
    long_term:
      enabled: true
      write_back: true
      retrieve_top_k: 5
      verify_reads: true
  sandbox:
    filesystem:
      readonly_root: true
      writable: [/tmp]
      persistent_workspace: true
```

（字段名以 canonical-agent + `packages/expert-work-protocol/.../agent_spec.py` 为准——`verify_reads` 在 `MemorySpec.long_term` 下、`persistent_workspace` 在 `FilesystemSpec` 下已核实;其余照 protocol 校验通过为准,manifest 注册时 422 即字段错。）

- [ ] **Step 10: README 更新**

`tools/bench/README.md` 加一节「二期:8 段全亮的跑法」:注册 manifest(`POST /v1/agents`,body `{"manifest": <yaml text>}`,若 422 试 `{"manifest_yaml": ...}` ——仓内两处写法不一致,以实测为准并把结论写进 README)→ `--seed-prompt-file tools/bench/prompts/seed.txt` → 正常 bench。说明:`verify_ms` 顶层节的含义、seed 轮不计入数据、verify 开着时 `first_llm_start` 是 verify 的开始时间(对照口径用端到端总时长)。

- [ ] **Step 11: 全量检查 + commit**

```bash
uv run ruff check tools/bench && uv run ruff format --check tools/bench
uv run pytest tools/bench/test_entry_latency.py -q
git add tools/bench docs/superpowers/plans/2026-07-27-agent-latency-phase2-pr1.md
git commit -m "feat(bench): seed 记忆轮 + verify_ms 指标 + bench-entry manifest 入仓"
```

---

### Task 2: P1.1 bump_access 改 fire-and-forget

**Files:**
- Modify: `services/orchestrator/src/orchestrator/graph_builder/memory.py`
- Test: `services/orchestrator/tests/test_memory_nodes.py`

**Interfaces:**
- Produces: 模块级 `_BACKGROUND_BUMP_TASKS: set[asyncio.Task[None]]`(测试 drain 用)。
- recall 返回值/异常语义不变;`bump_access` span 保留(后台任务内打)。

**背景:**
- 现代码 `memory.py:645-656`:recall 最后一步同步 `await memory_store.bump_access(...)`,后续仅纯 CPU 的 redact + return,零依赖。
- fire-and-forget 范式照 `sse.py:797-810`:模块级强引用集合 + `add_done_callback(discard)`(防 RUF006 / GC 提前回收)。
- `create_task` 拷贝当时的 context——在 recall span 的 `with` 块内创建,子 span 父子关系不变;时间上任务在 recall 返回后才跑完,该 span 不再阻塞入口链(这就是收益)。
- 后台任务不再特判 `RunCancelledError`:`bump_access` 不消费 cancellation token,run 取消后这个 UPDATE 照样值得完成(记忆确实被召回过)。

- [ ] **Step 1: 改现有测试为先红(drain 后断言)**

`test_memory_nodes.py` 的 `test_memory_recall_node_bumps_access_count_on_hit`(约 337-356 行),在 `out = await node(...)` 断言 recalled 之后、`access_count` 断言之前插入:

```python
    # 二期 P1.1 —— bump_access 是 fire-and-forget 后台任务,断言前先 drain。
    from orchestrator.graph_builder.memory import _BACKGROUND_BUMP_TASKS

    await asyncio.gather(*list(_BACKGROUND_BUMP_TASKS))
```

文件顶部确认有 `import asyncio`(没有则加)。

再追加一个新测试:

```python
@pytest.mark.asyncio
async def test_memory_recall_returns_before_bump_access_completes() -> None:
    """P1.1 —— bump_access 不再阻塞 recall 返回:store 的 bump_access 挂在
    一个未 set 的事件上,recall 必须照常返回;set 事件、drain 后计数才落。"""
    gate = asyncio.Event()
    store = InMemoryMemoryStore()
    tenant, user = uuid4(), uuid4()
    await _seed(store, tenant=tenant, user=user, content="user prefers metric units")

    original_bump = store.bump_access

    async def slow_bump(**kwargs: Any) -> None:
        await gate.wait()
        await original_bump(**kwargs)

    store.bump_access = slow_bump  # type: ignore[method-assign]

    node = make_memory_recall_node(memory_store=store, embedder=FakeEmbedder(dim=_DIM), top_k=5)
    out = await asyncio.wait_for(
        node(  # type: ignore[arg-type]
            _state("what's the distance"),
            {"configurable": {"tenant_id": str(tenant), "user_id": str(user)}},
        ),
        timeout=2.0,
    )
    assert [m.content for m in out["recalled_memories"]] == ["user prefers metric units"]

    from orchestrator.graph_builder.memory import _BACKGROUND_BUMP_TASKS

    gate.set()
    await asyncio.gather(*list(_BACKGROUND_BUMP_TASKS))
    [after] = await store.list_for_user(tenant_id=tenant, user_id=user)
    assert after.access_count == 1
```

(`Any` 已在文件 import 里则复用,否则加。)

- [ ] **Step 2: 跑测试确认失败**

Run: `DOCKER_HOST= uv run pytest services/orchestrator/tests/test_memory_nodes.py -x -q -k "bump or before_bump"`
Expected: 新测试 FAIL(`wait_for` 超时——现同步 await 卡在 gate 上)+ 旧测试 ImportError `_BACKGROUND_BUMP_TASKS`。

- [ ] **Step 3: 实现**

`memory.py`:模块顶部(logger 附近)加:

```python
#: 二期 P1.1 —— fire-and-forget bump_access 任务的强引用集合(RUF006:
#: 裸 create_task 的返回值不被引用会被 GC 提前回收)。照 sse.py 的
#: _BACKGROUND_CLEANUP_TASKS 范式。测试用它 drain。
_BACKGROUND_BUMP_TASKS: set[asyncio.Task[None]] = set()


async def _bump_access_background(
    *, memory_store: MemoryStore, tenant_id: UUID, user_id: UUID, ids: list[UUID]
) -> None:
    """后台执行 bump_access —— best-effort 语义与内联时代一致(失败只
    warning),但不再阻塞 recall 返回。不特判 RunCancelledError:这条
    UPDATE 不消费 cancellation token,run 取消后记忆确实被召回过,计数
    照记。"""
    with expert_work_span(ExpertWorkComponent.MEMORY, "bump_access"):
        try:
            await memory_store.bump_access(tenant_id=tenant_id, user_id=user_id, ids=ids)
        except Exception:
            logger.warning("memory.bump_access_failed", exc_info=True)
```

（`import asyncio` / `from uuid import UUID` 确认已在文件 import 中,缺则补。）

recall node 里 645-656 行整块替换为:

```python
            if memories:
                bump_task = asyncio.create_task(
                    _bump_access_background(
                        memory_store=memory_store,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        ids=[m.id for m in memories],
                    )
                )
                _BACKGROUND_BUMP_TASKS.add(bump_task)
                bump_task.add_done_callback(_BACKGROUND_BUMP_TASKS.discard)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `DOCKER_HOST= uv run pytest services/orchestrator/tests/test_memory_nodes.py -x -q`
Expected: PASS(全部)

- [ ] **Step 5: 全量回归 + commit**

```bash
DOCKER_HOST= uv run pytest services/orchestrator/tests/ -x -q
uv run ruff check services/orchestrator && uv run ruff format --check services/orchestrator
git add services/orchestrator
git commit -m "perf(orchestrator): bump_access 改 fire-and-forget,不再阻塞召回返回(P1.1)"
```

---

### Task 3: P1.2 recall 的 tenant_config 读走已有缓存

**Files:**
- Modify: `services/control-plane/src/control_plane/tenancy/tenant_config.py`(文件底部加适配器类)
- Modify: `services/control-plane/src/control_plane/app.py`(MemoryEnv 注入处,约 1460-1472 行)
- Test: `services/control-plane/tests/test_tenant_config_read_adapter.py`(新建)

**Interfaces:**
- Consumes: `TenantConfigService`(同文件,已有 60s 缓存 + upsert 失效/prime)。
- Produces: `ServiceBackedTenantConfigStore` —— 实现 persistence 的 `TenantConfigStore` 接口,`get` 委托 service(`TenantConfigNotConfiguredError` → `None`),`upsert` 委托 service(保 prime 语义)。

**背景:**
- recall 的 `resolve_mode` 段 15ms/次 = MemoryEnv 注入的是**无缓存裸 repo**(`app.py:1466` `tenant_config_store=resolved_tenant_config_repo`);`TenantConfigService` 的缓存(`tenant_config_cache_ttl_s`,默认 60s)只有 resolver 等路径在用。
- **禁止**在 repo 层包缓存:`TenantStatusService`(kill switch,秒级 TTL)消费同一 repo,repo 层长缓存会架空急停传播——安全回归。
- persistence 的 `TenantConfigStore` 抽象在 `packages/expert-work-persistence/src/expert_work/persistence/tenant_config/base.py`(方法集实施时打开核实——至少 `get`/`upsert`;若还有别的抽象方法,同样逐个委托 service 或底层 store,不留 NotImplementedError)。
- `service.get(actor_id=None)` 不发 audit(service 只在带 actor 时 emit)——热路径无审计噪音,已核实。

- [ ] **Step 1: 写失败测试**

`services/control-plane/tests/test_tenant_config_read_adapter.py`:

```python
"""二期 P1.2 —— ServiceBackedTenantConfigStore:recall 路径的 tenant_config
读走 TenantConfigService 的现有缓存,不再每次召回打一条 DB 读。"""

from uuid import uuid4

import pytest
from control_plane.tenancy.tenant_config import (
    ServiceBackedTenantConfigStore,
    TenantConfigService,
)
from expert_work.persistence.tenant_config import InMemoryTenantConfigStore
from expert_work.persistence.tenant_config.base import TenantConfigPatch

from .auth_fixtures import make_noop_audit_logger  # 实施时核实工厂名/路径,
# control-plane tests 里已有现成 no-op audit fixture(TenantConfigService 构造需要)


class _CountingStore(InMemoryTenantConfigStore):
    def __init__(self) -> None:
        super().__init__()
        self.get_calls = 0

    async def get(self, *, tenant_id):  # type: ignore[override]
        self.get_calls += 1
        return await super().get(tenant_id=tenant_id)


@pytest.mark.asyncio
async def test_get_hits_service_cache_not_store() -> None:
    store = _CountingStore()
    tenant_id = uuid4()
    await store.upsert(
        tenant_id=tenant_id,
        patch=TenantConfigPatch(memory_recall_mode="vector"),
        actor_id="t",
    )
    service = TenantConfigService(store=store, audit_logger=make_noop_audit_logger(), ttl_s=60.0)
    adapter = ServiceBackedTenantConfigStore(service=service)

    first = await adapter.get(tenant_id=tenant_id)
    second = await adapter.get(tenant_id=tenant_id)
    assert first is not None and first.memory_recall_mode == "vector"
    assert second is not None
    assert store.get_calls == 1  # 第二次命中 service 缓存,没打 store


@pytest.mark.asyncio
async def test_get_missing_row_returns_none_not_raise() -> None:
    """store 接口约定 miss 返回 None;service 的 NotConfiguredError 必须被
    适配器吞掉转 None —— recall 节点靠 None 走默认 hybrid。"""
    service = TenantConfigService(
        store=InMemoryTenantConfigStore(), audit_logger=make_noop_audit_logger(), ttl_s=60.0
    )
    adapter = ServiceBackedTenantConfigStore(service=service)
    assert await adapter.get(tenant_id=uuid4()) is None
```

（`TenantConfigPatch` 的字段名/构造方式与 no-op audit fixture 实施时以真实代码为准;测试意图不变:计数店证明缓存命中 + miss 转 None。）

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest services/control-plane/tests/test_tenant_config_read_adapter.py -x -q`
Expected: FAIL — ImportError `ServiceBackedTenantConfigStore`

- [ ] **Step 3: 实现适配器**

`tenancy/tenant_config.py` 文件底部:

```python
class ServiceBackedTenantConfigStore(TenantConfigStore):
    """二期 P1.2 —— 给 orchestrator 的 MemoryEnv 用的 store 适配器。

    recall 节点每次召回读一次 tenant_config(只用 memory_recall_mode 一个
    字段);裸 repo 是每次一条真 DB 读(基线 15ms/召回)。这个适配器把读
    委托给 :class:`TenantConfigService`,复用它现有的 per-tenant TTL 缓存
    与 upsert 失效/prime —— 不新建任何缓存。

    为什么不直接把共享 repo 包一层缓存:TenantStatusService(kill switch)
    用秒级 TTL 消费同一 repo,repo 层长缓存会架空急停传播。

    ``get`` 把 service 的 :class:`TenantConfigNotConfiguredError` 转回
    store 接口约定的 ``None``(recall 节点靠 None 走默认 hybrid);
    ``actor_id=None`` 使 service 不发读审计 —— 热路径零审计噪音。
    """

    def __init__(self, *, service: TenantConfigService) -> None:
        self._service = service

    async def get(self, *, tenant_id: UUID) -> TenantConfigRecord | None:
        try:
            return await self._service.get(tenant_id=tenant_id, actor_id=None)
        except TenantConfigNotConfiguredError:
            return None

    async def upsert(
        self, *, tenant_id: UUID, patch: TenantConfigPatch, actor_id: str
    ) -> TenantConfigRecord:
        return await self._service.upsert(tenant_id=tenant_id, patch=patch, actor_id=actor_id)
```

import 块补 `TenantConfigStore` / `TenantConfigPatch`(persistence base)。**实施时打开 `tenant_config/base.py` 核对抽象方法全集**——若基类还有其它抽象方法(如 `delete`),逐个委托 `self._service._store`(或提升 service 对应方法),不留坑。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest services/control-plane/tests/test_tenant_config_read_adapter.py -x -q`
Expected: PASS

- [ ] **Step 5: app.py 接线**

`app.py` MemoryEnv 构造(约 1460 行)改注入:

```python
                memory_env = MemoryEnv(
                    store=resolved_memory_store,
                    embedder=embedder,
                    dlq=resolved_memory_dlq,  # K.K7 — failed writebacks land here
                    # Capability Uplift Sprint #6 (Mini-ADR U-5) — recall
                    # node reads tenant_config.memory_recall_mode.
                    # 二期 P1.2 —— 经 ServiceBackedTenantConfigStore 走
                    # TenantConfigService 的 60s 缓存,不再每次召回打 DB。
                    tenant_config_store=ServiceBackedTenantConfigStore(
                        service=resolved_tenant_config_service
                    ),
                    reranker=reranker,
                )
```

(import 块加 `ServiceBackedTenantConfigStore`;`resolved_tenant_config_service` 在 app.py:739 已构造,先于此处,无时序问题。)

- [ ] **Step 6: 全量回归 + commit**

```bash
uv run pytest services/control-plane/tests/ -x -q
uv run ruff check services/control-plane && uv run ruff format --check services/control-plane
git add services/control-plane
git commit -m "perf(control-plane): recall 的 tenant_config 读走 TenantConfigService 缓存(P1.2)"
```

---

### Task 4: P1.3 memory_recall 与 planner→workspace_ingest 并行

**Files:**
- Modify: `services/orchestrator/src/orchestrator/graph_builder/builder.py:1424-1443`(入口链边构造)
- Test: `services/orchestrator/tests/test_entry_chain_parallel.py`(新建)

**Interfaces:**
- Consumes: 既有节点工厂(不动);`AgentState` 写集已核实不相交(recall 写 `recalled_memories`、planner/ingest 写 `plan`,两 key 均无 reducer 但分属不同分支)。
- Produces: 新边拓扑 —— `START → memory_recall → agent` ‖ `START → planner → workspace_ingest → agent`,LangGraph 多入边 = AND-join 屏障。

**背景:**
- 现拓扑线性:`START → memory_recall → planner → workspace_ingest → agent`(`itertools.pairwise`)。ingest 在 planner 后是 CM-0 语义(人改的 PLAN.md 覆盖 planner 生成的)——**分支内保序,分支间并行**。
- planner 只读 `state["messages"]`(`planner.py:148`),不消费召回结果——已核实,并行不改变其输入。
- approval RESUME 重入路径(`builder.py:1160-1167`,agent_node 内直接 await ingest)与本改动无关,不动。
- 汇合边构造小心分支为空的情形(见 Step 3 代码,`tails` 列表法,勿用 set——两支全空时须恰好一条 `START → agent`)。

- [ ] **Step 1: 写失败测试(并发证明)**

`services/orchestrator/tests/test_entry_chain_parallel.py`:

```python
"""二期 P1.3 —— memory_recall 与 plan 分支(planner→workspace_ingest)并行。

并发证明用事件握手:recall 节点 await 一个只有 ingest 节点才 set 的事件。
旧线性拓扑(recall 先于 ingest)下 recall 永远等不到 → wait_for 超时;
并行拓扑下两节点同一 superstep,握手成功。先红后绿。
"""

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from orchestrator.graph_builder.builder import build_react_graph
from orchestrator.llm import LLMCaller  # 实施时核对:既有 builder 测试怎么造
# fake caller / 最小 build_react_graph 参数集,照最近的现成 fixture 抄
# (例如 test_entry_chain_spans.py 的搭法),不要自造新范式。


@pytest.mark.asyncio
async def test_memory_recall_runs_concurrently_with_ingest_branch() -> None:
    handshake = asyncio.Event()
    order: list[str] = []

    async def fake_recall(state: dict[str, Any], config: Any) -> dict[str, Any]:
        order.append("recall:start")
        await asyncio.wait_for(handshake.wait(), timeout=2.0)
        order.append("recall:end")
        return {"recalled_memories": []}

    async def fake_ingest(state: dict[str, Any], config: Any) -> dict[str, Any]:
        order.append("ingest:start")
        handshake.set()
        return {}

    graph = build_react_graph(
        # 最小参数集照 test_entry_chain_spans.py 的现成 fixture,
        # 注入 memory_recall_node=fake_recall, workspace_ingest_node=fake_ingest,
        # planner_node=None。
        ...,
    )
    result = await asyncio.wait_for(
        graph.ainvoke(
            {"messages": [HumanMessage(content="hi")]},
            {"configurable": {"thread_id": "t1"}},
        ),
        timeout=10.0,
    )
    assert "recall:end" in order  # 握手成功 = 两节点确在同一 superstep 并发
    assert result is not None
```

(`...` 处按现成 fixture 补全——这是测试搭建代码,不是产品逻辑;若 `build_react_graph` 直连内部别名类型导致 fake 节点类型不合,加 `# type: ignore[arg-type]`,与 builder.py 既有注释同款。)

- [ ] **Step 2: 跑测试确认失败**

Run: `DOCKER_HOST= uv run pytest services/orchestrator/tests/test_entry_chain_parallel.py -x -q`
Expected: FAIL — `asyncio.TimeoutError`(线性拓扑下 recall 先跑,等不到 ingest 的 set)

- [ ] **Step 3: 改边拓扑**

`builder.py:1424-1443` 整块替换:

```python
    # Entry chain(二期 P1.3)—— 两条分支从 START 并发,AND-join 汇到 agent:
    #   分支 1: memory_recall(写 recalled_memories)
    #   分支 2: planner → workspace_ingest(写 plan;分支内保序 —— CM-0:
    #           人改的 PLAN.md 仍覆盖 planner 生成的 plan)
    # 写集不相交,LangGraph 多入边节点等全部父分支完成后执行一次。
    # ``# type: ignore[arg-type]``: the bare Callable node aliases don't
    # match LangGraph's internal ``_NodeWithConfig`` overloads (same gap
    # runs.py documents).
    tails: list[str] = []
    if memory_recall_node is not None:
        graph.add_node("memory_recall", memory_recall_node)  # type: ignore[arg-type]
        graph.add_edge(START, "memory_recall")
        tails.append("memory_recall")
    plan_tail: str | None = None
    if planner_node is not None:
        graph.add_node("planner", planner_node)  # type: ignore[arg-type]
        graph.add_edge(START, "planner")
        plan_tail = "planner"
    if workspace_ingest_node is not None:
        graph.add_node("workspace_ingest", workspace_ingest_node)  # type: ignore[arg-type]
        graph.add_edge(plan_tail if plan_tail is not None else START, "workspace_ingest")
        plan_tail = "workspace_ingest"
    if plan_tail is not None:
        tails.append(plan_tail)
    if not tails:
        tails.append(START)
    for tail in tails:
        graph.add_edge(tail, "agent")
```

（`itertools.pairwise` 若此后无其他使用点则同步删 import——实施时 grep 确认。）

- [ ] **Step 4: 跑测试确认通过**

Run: `DOCKER_HOST= uv run pytest services/orchestrator/tests/test_entry_chain_parallel.py -x -q`
Expected: PASS

- [ ] **Step 5: 全量回归(重点盯既有入口链测试)+ commit**

```bash
DOCKER_HOST= uv run pytest services/orchestrator/tests/ -x -q
```

重点关注 `test_entry_chain_spans.py` 与一切断言节点执行顺序的既有测试——若有测试断言「recall 先于 ingest」的线性顺序,那是拓扑假设过期,按新拓扑修测试(两分支并发、agent 等 join),不是修实现。

```bash
uv run ruff check services/orchestrator && uv run ruff format --check services/orchestrator
git add services/orchestrator
git commit -m "perf(orchestrator): memory_recall 与 planner→workspace_ingest 两分支并行(P1.3)"
```

---

### Task 5: P3 guards 全关时 token 流零缓冲

**Files:**
- Modify: `services/orchestrator/src/orchestrator/graph_builder/streaming_redact.py`
- Test: `services/orchestrator/tests/test_streaming_redact.py`

**Interfaces:**
- `StreamingRedactor` 构造签名不变(`dlp: bool, screen: bool`);新增行为:双关时 `feed` 直返全文、`flush` 返空。`TokenSink`/`make_token_sink` 不动。

**背景:**
- `feed()` 的 `boundary = max(self._emitted_out, full_red_len - HOLD_CHARS)` 无视开关,guards 全关仍扣尾部 64 字符。hold 的存在意义 = screen 的整段撤回 + dlp 的跨 chunk 模式——两者都关时 hold 纯损耗。
- judge 开 → `make_token_sink` 返 None(整条不流式),与本改动无关。
- `screen` 或 `dlp` 任一开着 → 行为必须逐字节不变(既有测试盖)。

- [ ] **Step 1: 写失败测试**

`test_streaming_redact.py` 追加:

```python
def test_feed_passthrough_when_both_guards_off() -> None:
    """P3 —— dlp/screen 双关时无 64 字符 hold:feed 立即全量返回。"""
    r = StreamingRedactor(dlp=False, screen=False)
    assert r.feed("short") == "short"          # < HOLD_CHARS 也立即出
    assert r.feed("A" * 100) == "A" * 100      # 无尾部扣留
    assert r.flush() == ""                     # 无 buffered 尾巴


def test_feed_still_holds_when_screen_on() -> None:
    """screen 单开 —— hold 行为必须不变(撤回语义依赖它)。"""
    r = StreamingRedactor(dlp=False, screen=True)
    out = r.feed("A" * 100)
    assert len(out) == 100 - HOLD_CHARS


def test_feed_still_holds_when_dlp_on() -> None:
    r = StreamingRedactor(dlp=True, screen=False)
    out = r.feed("A" * 100)
    assert len(out) == 100 - HOLD_CHARS
```

- [ ] **Step 2: 跑测试确认失败**

Run: `DOCKER_HOST= uv run pytest services/orchestrator/tests/test_streaming_redact.py -x -q`
Expected: 第一个新测试 FAIL(现 `feed("short")` 返回 `""`),后两个 PASS。

- [ ] **Step 3: 实现 passthrough 快路径**

`streaming_redact.py` `StreamingRedactor.__init__` 加一行(现有字段初始化之后):

```python
        # 二期 P3 —— 双关时整条快路径:无 hold、无重扫、无冻结指针。
        # hold 的存在意义是 screen 的整段撤回 + dlp 的跨 chunk 模式匹配,
        # 两者都关时扣住尾部 64 字符纯粹是感知延迟损耗。
        self._passthrough = not dlp and not screen
```

`feed()` 方法体最前(`if self._blocked:` 之前)加:

```python
        if self._passthrough:
            return text
```

`flush()` 方法体最前加:

```python
        if self._passthrough:
            return ""
```

- [ ] **Step 4: 跑测试确认通过**

Run: `DOCKER_HOST= uv run pytest services/orchestrator/tests/test_streaming_redact.py -x -q`
Expected: PASS(全部——既有 fuzz/oracle 测试都是 dlp 或 screen 开的路径,不受影响)

- [ ] **Step 5: 全量回归 + commit**

```bash
DOCKER_HOST= uv run pytest services/orchestrator/tests/ -x -q
uv run ruff check services/orchestrator && uv run ruff format --check services/orchestrator
git add services/orchestrator
git commit -m "perf(orchestrator): guards 全关时 token 流走直通快路径,免 64 字符 hold(P3)"
```

---

### Task 6: 真栈 before/after + verify on/off 数据(主会话执行,不派实施 subagent)

**不是代码 task。** 由 coordinator 在真栈上执行(容器内 bench 配方见程序记忆/一期 ledger):

1. 在改动前的 main(`f892b6dc`)容器上:注册 `bench-entry@2.0.0` + seed + 跑 10 轮 → `tools/bench/baselines/2026-07-27-phase2-before.yaml`(8 段全亮的新基线)。
2. 切本分支镜像重建后同容器同 prompt 同 seed 再跑 10 轮 → `2026-07-27-phase2-after.yaml`。预期:`记忆召回` 段瘦掉 bump_access 与 resolve_mode 的贡献、`工作区摄取` 不再串行贡献总时长、`verify_ms` 数字首次可见。
3. verify off 对照:PUT `bench-entry@2.0.1`(唯一差异 `verify_reads: false`)再跑 10 轮 → `2026-07-27-verify-off.yaml`。
4. 两个 YAML 的 meta.note 写清对照口径(照一期教训:对照解释必须进 tracked 产物)。
5. **拿 verify on/off 数字问用户 P1.4 拍板**(改默认则在本 PR 顺带改 `agent_spec.py:398` + tooltip;不改则只补 tooltip——admin-ui locale 两处)。

---

## 执行顺序与依赖

Task 1(尺子)→ Task 2/3/4/5(四项独立,可任意顺序但**串行执行**——同仓避免冲突)→ Task 6(数据轮,依赖全部)。

## Self-Review 记录

- spec 覆盖:PR1 六 task 对应 spec Task 1-6 ✓;P1.4 的「改默认」留给 Task 6 拍板后,spec 一致 ✓。
- 占位符:Task 1 Step 9 manifest 字段留了「以 protocol 校验为准」的活口——非占位符,是外部事实(仓内 `{manifest}` vs `{manifest_yaml}` 两写法不一致)的处置指令 ✓。
- 类型一致:`_BACKGROUND_BUMP_TASKS` / `ServiceBackedTenantConfigStore` / `VERIFY_MS_KEY` 名字全计划一致 ✓。
