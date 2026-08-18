# 调试台重设计 PR1 —— 对外 `plan` SSE 事件 + 文档 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 计划(任务列表)成为对外 SSE 的一等事件:每次计划创建 / 修改推一条顶层 `plan` 事件(整份快照),run 开始时会话已有计划先补发一条;对外文档第 3 章加一节。

**Architecture:** 计划已经在 `updates` 帧的节点值里(`tools` 节点 `accumulated_state` 展开、`planner` 节点通道),只是埋得深、第三方按契约看不到。orchestrator `sse.py` 加一个 `_publish_plan` sink,两处派生:①流循环里发完 `updates` 后检测节点值含 `plan` → 派生一条;②`metadata` 之后、`graph.astream` 之前 `graph.aget_state` 读一次 checkpoint,有 `plan` 先补发。走 `_publish_frame`,所以落库、带 id、可续传、对外流自动可见(`sse_consumer` 与 `_run_event_stream.py` 都是无过滤原样转发)。`updates` 里的 `plan` 键**保留**。文档按 `2026-08-17-external-docs-style-guide.md` 写。

**Tech Stack:** orchestrator(Python asyncio + pytest)/ protocol(pydantic StrEnum)/ docs-site(VitePress)。

**Spec:** `docs/superpowers/specs/2026-08-17-debug-console-redesign-design.md` §二.4「计划一等公民」、§四、§五 PR1、§七。

## Global Constraints

- D6 已拍板:新增顶层事件名 **`plan`**;payload 与 protocol `Plan` 同形:`{ "goal": string, "steps": [ { "id": string, "description": string, "status": "pending" | "in_progress" | "completed" } ] }`;**整份快照,不是增量**。
- 开跑补发:`metadata` 之后、第一条业务帧之前;`aget_state` 失败只记 `logger.warning`,**绝不影响 run**。
- `updates` 帧里的 `plan` 键保留原样(前端 `parseTimeline` 在读)。
- 事件走 `_publish_frame`(落库 + seq),**不能**走 `publish_ephemeral`。
- 对外文档:第三方工程师视角、企业级语气;正文全角标点(，：；（）),代码块内 ASCII;字段表 `字段 / 类型 / 说明`,枚举取值穷举;事件小节四段:什么时候发 → `data` 字段 → 示例 → 客户端怎么处理;不出现内部模块路径 / `.py` 文件名 / 内网地址。
- 每条新断言先在未改代码上跑红。
- 命令:python `uv run …`(仓库根);docs-site 在 `apps/admin-ui/docs-site` 下 `pnpm build`。
- 真栈探针:临时 key 在 control-plane pod 内铸、用、撤,明文**不出集群**;kubeconfig 不贴进对话。

---

## 文件结构

| 文件 | 改动 |
|---|---|
| `packages/expert-work-protocol/src/expert_work/protocol/event.py:13-33` | `EventType.PLAN = "plan"` |
| `services/orchestrator/src/orchestrator/sse.py:455-500, :508-514, :571` | `_publish_plan` / `_plan_in_chunk` / 流循环派生 / 开跑补发 |
| `services/orchestrator/tests/test_sse_plan_events.py`(新) | 派生 / 补发 / 降级 / 落库 四组测试 |
| `apps/admin-ui/docs-site/guide/sse-events.md` | 3.1 / 3.2 id 表 / 3.3 表与计数 / 3.4 新节 `### plan` + 公共 store / 3.5 HANDLERS |
| `apps/admin-ui/docs-site/guide/examples.md:18` | 10.1 导语列表加 `plan` |
| `apps/admin-ui/docs-site/scripts/check_links.py`(新) | 全站死链检查(以前在会话 scratchpad 里,入仓) |

---

### Task 1: `plan` 事件——从 `updates` 派生

**Files:**
- Modify: `packages/expert-work-protocol/src/expert_work/protocol/event.py:13-33`
- Modify: `services/orchestrator/src/orchestrator/sse.py:455-500`(sink 区)、`:571`(流循环)
- Test: `services/orchestrator/tests/test_sse_plan_events.py`(新建)

**Interfaces:**
- Produces: `EventType.PLAN`(值 `"plan"`);`sse.py` 内部闭包 `_publish_plan(plan: Any) -> Awaitable[None]` 与模块级纯函数 `_plan_in_chunk(chunk: Any) -> Any | None`(Task 2 复用 `_publish_plan`)。
- Consumes: `_publish_frame(event_name: str, data: Any)`(`sse.py:399-407`)、`_to_jsonable`(`sse.py:1496`)。

- [ ] **Step 1: 写红测试**

新建 `services/orchestrator/tests/test_sse_plan_events.py`:

```python
"""调试台重设计 PR1 —— 顶层 ``plan`` 事件:从 updates 派生 / 开跑补发 / 降级 / 落库。

spec: docs/superpowers/specs/2026-08-17-debug-console-redesign-design.md §二.4。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from expert_work.protocol import EventType
from expert_work.runtime.runs import DisconnectMode, RunManager, RunRecord, RunStatus
from expert_work.runtime.runs.event_store import InMemoryRunEventStore
from expert_work.runtime.stream_bridge import InMemoryStreamBridge, is_end
from orchestrator.sse import run_agent

_PLAN_V1: dict[str, Any] = {
    "goal": "给客户出续约建议",
    "steps": [
        {"id": "1", "description": "查档案", "status": "completed"},
        {"id": "2", "description": "分析工单", "status": "in_progress"},
    ],
}
_PLAN_V2: dict[str, Any] = {
    "goal": "给客户出续约建议",
    "steps": [
        {"id": "1", "description": "查档案", "status": "completed"},
        {"id": "2", "description": "分析工单", "status": "completed"},
        {"id": "3", "description": "出建议", "status": "pending"},
    ],
}


@dataclass
class _Graph:
    """astream 按脚本吐 updates chunk;aget_state 返回 initial_state(开跑前那次读)
    与 final_state(结束时那次读)—— 两次读用同一个 stub,按调用次序区分。"""

    chunks: list[Any]
    initial_state: dict[str, Any] = field(default_factory=dict)
    final_state: dict[str, Any] = field(default_factory=dict)
    aget_state_raises: bool = False
    aget_state_calls: int = 0

    async def astream(
        self, input: Any, config: Any = None, *, stream_mode: Any = None
    ) -> AsyncIterator[Any]:
        del input, config, stream_mode
        for chunk in self.chunks:
            yield chunk

    async def aget_state(self, config: Any) -> Any:
        del config
        self.aget_state_calls += 1
        if self.aget_state_raises:
            raise RuntimeError("checkpoint backend down")
        values = self.initial_state if self.aget_state_calls == 1 else self.final_state
        return SimpleNamespace(values=dict(values))


async def _new_record(rm: RunManager) -> RunRecord:
    return await rm.create(
        run_id=uuid4(),
        thread_id=uuid4(),
        tenant_id=uuid4(),
        on_disconnect=DisconnectMode.CANCEL,
    )


async def _drain(bridge: InMemoryStreamBridge, run_id: Any) -> list[Any]:
    events: list[Any] = []
    async for entry in bridge.subscribe(run_id, heartbeat_interval=5.0):
        if is_end(entry):
            break
        events.append(entry)
    return events


async def _run(graph: _Graph, *, store: InMemoryRunEventStore | None = None) -> tuple[list[Any], RunRecord, RunManager]:
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={"messages": []},
        config={"configurable": {"thread_id": str(record.thread_id)}},
        event_store=store,
    )
    return await _drain(bridge, record.run_id), record, rm


def test_event_type_has_plan_member() -> None:
    assert EventType.PLAN.value == "plan"


@pytest.mark.asyncio
async def test_updates_with_plan_derives_one_plan_frame_after_it() -> None:
    """tools 节点值里带 plan(update_plan 跑完)→ 紧跟一条顶层 plan 帧,payload 与快照相等。"""
    events, _, _ = await _run(
        _Graph(
            chunks=[
                {"agent": {"step_count": 1}},
                {"tools": {"step_count": 1, "plan": _PLAN_V1}},
                {"agent": {"step_count": 2}},
            ]
        )
    )
    names = [e.event for e in events]
    assert names == ["metadata", "updates", "updates", "plan", "updates"]
    plan_frame = events[3]
    assert plan_frame.data == _PLAN_V1
    # updates 里的 plan 键保留(前端 parseTimeline 在读)
    assert events[2].data["tools"]["plan"] == _PLAN_V1
    # plan 帧走 _publish_frame:有 id(seq),且 seq 严格递增
    seqs = [int(e.id.split("-")[1]) for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


@pytest.mark.asyncio
async def test_planner_node_plan_also_derives() -> None:
    """plan_execute 模式:planner 节点通道带 plan → 同样派生。"""
    events, _, _ = await _run(
        _Graph(chunks=[{"planner": {"plan": _PLAN_V1}}, {"agent": {"step_count": 1}}])
    )
    assert [e.event for e in events] == ["metadata", "updates", "plan", "updates"]
    assert events[2].data == _PLAN_V1


@pytest.mark.asyncio
async def test_each_plan_change_derives_its_own_frame() -> None:
    """两次 update_plan → 两条 plan 帧,各自是当时的整份快照(第三方整段覆盖即可)。"""
    events, _, _ = await _run(
        _Graph(
            chunks=[
                {"tools": {"plan": _PLAN_V1}},
                {"agent": {"step_count": 2}},
                {"tools": {"plan": _PLAN_V2}},
            ]
        )
    )
    plans = [e.data for e in events if e.event == "plan"]
    assert plans == [_PLAN_V1, _PLAN_V2]


@pytest.mark.asyncio
async def test_parallel_nodes_last_plan_wins() -> None:
    """同一 chunk 多个节点都写 plan(并行分支)→ 只派生一条,取最后一个节点的(与
    tools 节点 accumulated_state「后写赢」同语义)。"""
    events, _, _ = await _run(
        _Graph(chunks=[{"tools": {"plan": _PLAN_V1}, "planner": {"plan": _PLAN_V2}}])
    )
    plans = [e.data for e in events if e.event == "plan"]
    assert plans == [_PLAN_V2]


@pytest.mark.asyncio
async def test_updates_without_plan_derive_nothing() -> None:
    """节点值没有 plan、或 plan 为 null → 不派生(不能给没用计划的 run 多发空帧)。"""
    events, _, _ = await _run(
        _Graph(chunks=[{"agent": {"step_count": 1}}, {"tools": {"plan": None}}])
    )
    assert "plan" not in [e.event for e in events]


@pytest.mark.asyncio
async def test_plan_frame_is_persisted_for_replay() -> None:
    """plan 帧落库(RunEventStore),replay / 续传端点才能重放它。"""
    store = InMemoryRunEventStore()
    events, record, _ = await _run(_Graph(chunks=[{"tools": {"plan": _PLAN_V1}}]), store=store)
    rows = await store.list(run_id=record.run_id, limit=500)
    plan_rows = [r for r in rows if r.event_name == "plan"]
    assert len(plan_rows) == 1
    assert plan_rows[0].data == _PLAN_V1
    live_seq = int(next(e for e in events if e.event == "plan").id.split("-")[1])
    assert plan_rows[0].seq == live_seq  # 实时 id 与落库 seq 同源
```

- [ ] **Step 2: 跑,确认红**

Run: `uv run pytest services/orchestrator/tests/test_sse_plan_events.py -q`
Expected: `test_event_type_has_plan_member` FAIL(`AttributeError: PLAN`);其余 FAIL(事件序列里没有 `plan`)。

- [ ] **Step 3: 加枚举成员**

`packages/expert-work-protocol/src/expert_work/protocol/event.py`,在 `COMPACTION = "compaction"` 之后加:

```python
    #: 调试台重设计 PR1(spec 2026-08-17 D6)—— 计划(任务列表)整份快照。
    #: 与 ``COMPACTION`` 同款:自由字符串 ``"plan"`` SSE 帧 + RunEventStore 落库,
    #: 枚举成员只为把规范事件名钉在一处(``orchestrator.sse.run_agent`` 引用它)。
    PLAN = "plan"
```

- [ ] **Step 4: 加模块级纯函数 `_plan_in_chunk`**

`services/orchestrator/src/orchestrator/sse.py`,放在 `_to_jsonable`(`:1496`)**之前**:

```python
def _plan_in_chunk(chunk: Any) -> Any | None:
    """一个 ``updates`` chunk(已 ``_to_jsonable``)里最后一个非空 ``plan``。

    调试台重设计 PR1 —— ``tools`` 节点把 ``update_plan`` 写回的快照展进节点值,
    ``planner`` 节点在自己的通道上带 ``plan``;两者都从这里派生一条顶层 ``plan``
    帧。并行分支里多个节点同时写时取最后一个 —— 与 ``tools`` 节点
    ``accumulated_state`` 的「后写赢」同语义(``builder.py`` K.K8)。
    """
    if not isinstance(chunk, dict):
        return None
    found: Any | None = None
    for node_val in chunk.values():
        if isinstance(node_val, dict) and node_val.get("plan") is not None:
            found = node_val["plan"]
    return found
```

- [ ] **Step 5: 加 sink,并在流循环里派生**

`sse.py` sink 区,在 `_publish_guard`(`:489-490`)之后加:

```python
    # 调试台重设计 PR1(spec 2026-08-17 D6)—— 计划快照帧。走 _publish_frame:
    # 落库、带 seq、可续传,对外流(sse_consumer / _run_event_stream 无过滤转发)
    # 自动可见。payload 与 protocol Plan 同形,整份快照不是增量。
    async def _publish_plan(plan: Any) -> None:
        await _publish_frame(EventType.PLAN.value, plan)
```

流循环里 `await _publish_frame(stream_mode, jsonable_chunk)`(`:571`)之后加:

```python
                        plan_snapshot = _plan_in_chunk(jsonable_chunk)
                        if plan_snapshot is not None:
                            await _publish_plan(plan_snapshot)
```

- [ ] **Step 6: 跑,确认绿(补发那两条留给 Task 2,此时应全绿——本文件目前没有补发用例)**

Run: `uv run pytest services/orchestrator/tests/test_sse_plan_events.py services/orchestrator/tests/test_sse.py -q`
Expected: 全绿(`test_sse.py` 既有序列断言不受影响:它们的 chunk 没有 `plan`)。

- [ ] **Step 7: ruff / mypy**

Run: `uv run ruff check services/orchestrator packages/expert-work-protocol && uv run ruff format --check services/orchestrator packages/expert-work-protocol && uv run mypy packages services/orchestrator/src`
Expected: 无报警。

- [ ] **Step 8: Commit**

```bash
git add packages/expert-work-protocol/src/expert_work/protocol/event.py services/orchestrator/src/orchestrator/sse.py services/orchestrator/tests/test_sse_plan_events.py
git commit -m "feat(sse): 顶层 plan 事件 —— updates 节点值里带 plan 就派生一条整份快照帧

计划以前只藏在 updates 的 tools/planner 节点值里,对外文档让第三方忽略这些键。
现在每次 update_plan / planner 写回都紧跟一条 event: plan(落库、带 seq、可续传);
updates 里的 plan 键保留给前端 parseTimeline。并行节点同写取最后一个(后写赢)。"
```

---

### Task 2: 开跑补发 + 降级

**Files:**
- Modify: `services/orchestrator/src/orchestrator/sse.py:508-514`(`metadata` 帧之后)
- Test: `services/orchestrator/tests/test_sse_plan_events.py`(追加)

**Interfaces:**
- Consumes: Task 1 的 `_publish_plan`;`graph.aget_state(effective_config)`(`StreamableGraph` 协议 `:201-211`);`_to_jsonable`。
- Produces: 无新接口。

- [ ] **Step 1: 追加红测试**

`test_sse_plan_events.py` 末尾追加:

```python
@pytest.mark.asyncio
async def test_existing_plan_is_replayed_right_after_metadata() -> None:
    """会话已有计划(上一 run 留下 / 空闲时 PUT 改的)→ metadata 之后、第一条业务帧之前
    先补发一条 plan,第三方冷启动打开页面就能画任务卡。"""
    graph = _Graph(chunks=[{"agent": {"step_count": 1}}], initial_state={"plan": _PLAN_V1})
    events, _, _ = await _run(graph)
    assert [e.event for e in events] == ["metadata", "plan", "updates"]
    assert events[1].data == _PLAN_V1


@pytest.mark.asyncio
async def test_no_existing_plan_no_extra_frame() -> None:
    """新会话 / 没用过计划 → 不补发。"""
    events, _, _ = await _run(_Graph(chunks=[{"agent": {"step_count": 1}}]))
    assert [e.event for e in events] == ["metadata", "updates"]


@pytest.mark.asyncio
async def test_initial_snapshot_read_failure_does_not_fail_run() -> None:
    """checkpoint 读失败只记日志:run 照常跑完,序列与没有计划时一样。"""
    graph = _Graph(chunks=[{"agent": {"step_count": 1}}], aget_state_raises=True)
    events, record, rm = await _run(graph)
    assert [e.event for e in events] == ["metadata", "updates"]
    assert rm.get(record.run_id).status is RunStatus.SUCCESS


@pytest.mark.asyncio
async def test_initial_snapshot_then_change_gives_two_frames() -> None:
    """开跑补发 v1,run 里 update_plan 改成 v2 → 两条 plan,依次是 v1、v2。"""
    graph = _Graph(
        chunks=[{"agent": {"step_count": 1}}, {"tools": {"plan": _PLAN_V2}}],
        initial_state={"plan": _PLAN_V1},
    )
    events, _, _ = await _run(graph)
    assert [e.data for e in events if e.event == "plan"] == [_PLAN_V1, _PLAN_V2]
```

- [ ] **Step 2: 跑,确认红**

Run: `uv run pytest services/orchestrator/tests/test_sse_plan_events.py -q`
Expected: `test_existing_plan_is_replayed_right_after_metadata` 与 `test_initial_snapshot_then_change_gives_two_frames` FAIL(没有补发帧);`test_no_existing_plan_no_extra_frame` 与 `test_initial_snapshot_read_failure_does_not_fail_run` 在旧代码上也 PASS —— 它们是护栏,不是驱动用例,允许一开始就绿。

- [ ] **Step 3: 在 `metadata` 帧之后补发**

`sse.py:509` `await _publish_frame("metadata", metadata_payload)` 之后、`ttft_started = time.monotonic()` 之前插入:

```python
        # 调试台重设计 PR1(D6)—— 冷启动补发:会话已有计划(上一 run 留下 / 空闲时
        # PUT 改的)就在第一条业务帧之前先发一份快照,第三方打开页面不用等计划变化
        # 才看得到任务卡。一次 checkpoint 读;读失败只记日志(与 J.8 pause 检查、
        # L.L7 trajectory 同款降级),绝不影响 run。
        try:
            initial_snapshot = await graph.aget_state(effective_config)
            initial_values = getattr(initial_snapshot, "values", None) or {}
            initial_plan = _to_jsonable(initial_values.get("plan"))
        except Exception:  # noqa: BLE001 — 降级路径,任何读失败都不能拖垮 run
            logger.warning(
                "run_agent.initial_plan_read_failed run_id=%s", run_id, exc_info=True
            )
            initial_plan = None
        if initial_plan is not None:
            await _publish_plan(initial_plan)
```

- [ ] **Step 4: 跑,确认绿;跑整个 orchestrator sse 测试族**

Run: `uv run pytest services/orchestrator/tests/test_sse_plan_events.py services/orchestrator/tests/test_sse.py services/orchestrator/tests/test_sse_persistence.py services/orchestrator/tests/test_sse_worker_events.py services/orchestrator/tests/test_sse_guard_events.py -q`
Expected: 全绿。特别看 `test_sse.py` 里用 `final_state={"pending_approval": …}` 的用例:`_ScriptedGraph.aget_state` 现在会被多调一次(开跑前),它们的 `final_state` 没有 `plan` 键,序列不变。若 `test_initial_snapshot_read_failure_does_not_fail_run` 红了、原因是 run 结束路径上还有别的 `aget_state` 调用没兜异常 —— 那是新发现,报 DONE_WITH_CONCERNS,不要在本任务里顺手改那些路径。

- [ ] **Step 5: 全量 orchestrator + ruff + mypy**

Run: `uv run pytest services/orchestrator/tests -q -n auto --timeout=120 && uv run ruff check services/orchestrator && uv run ruff format --check services/orchestrator && uv run mypy packages services/orchestrator/src`
Expected: 全绿。

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/src/orchestrator/sse.py services/orchestrator/tests/test_sse_plan_events.py
git commit -m "feat(sse): run 开始时会话已有计划先补发一条 plan(读 checkpoint 失败只记日志)"
```

---

### Task 3: 对外文档 —— 第 3 章 `plan` 节 + 全站同步 + 死链脚本入仓

**Files:**
- Modify: `apps/admin-ui/docs-site/guide/sse-events.md`(3.1 / 3.2 / 3.3 / 3.4 / 3.5)
- Modify: `apps/admin-ui/docs-site/guide/examples.md:18`
- Create: `apps/admin-ui/docs-site/scripts/check_links.py`

**Interfaces:** 无。

- [ ] **Step 1: 3.1 概览(`sse-events.md:44`)**

把

```
2. 中间——`token` 与 `updates` 交替出现，轮数取决于这次 run 走了几步；其间还可能出现 `worker`、`guard`、`compaction`、`approval`、`retry`、`error`。
```

改为

```
2. 中间——`token` 与 `updates` 交替出现，轮数取决于这次 run 走了几步；其间还可能出现 `plan`、`worker`、`guard`、`compaction`、`approval`、`retry`、`error`。
```

- [ ] **Step 2: 3.2 带 id 的事件表(`sse-events.md:95`)**

把

```
| 带 `id:` | `metadata`、`updates`、`worker`、`guard`、`compaction`、`approval`、`retry`、`error` | 会 |
```

改为

```
| 带 `id:` | `metadata`、`updates`、`plan`、`worker`、`guard`、`compaction`、`approval`、`retry`、`error` | 会 |
```

- [ ] **Step 3: 3.3 事件一览表 + 计数(`sse-events.md:141-158`)**

在 `updates` 那一行之后插入一行:

```
| [`plan`](#plan) | Agent 创建或修改计划时；run 开始时会话已有计划也发一次 | 用它整个替换本地保存的计划，渲染任务列表 |
```

`:156` 的「当前会遇到的 12 个事件」改成「当前会遇到的 13 个事件」。

- [ ] **Step 4: 3.4 导语 + 公共 store(`sse-events.md:161-178`)**

`:161`「下面十个小节的顺序与 3.3 的表一致」改成「下面十一个小节的顺序与 3.3 的表一致」。

公共定义代码块里 `store` 加一行(放在 `steps: new Map()` 之前):

```js
  plan: null,              // 当前计划,收到 plan 事件整段覆盖
```

- [ ] **Step 5: 3.4 新节 `### plan`**

插在 `### updates` 一节末尾(即 `### worker` 标题之前)。**全文照抄**:

````markdown
### plan

#### 什么时候发

Agent 在这次 run 里创建或修改了计划时，服务端通过当前这条连接发一次，内容是修改后的整份计划。另外，run 开始时如果这段会话已经有计划（上一轮留下的），服务端会在 `metadata` 之后、第一个步骤之前先发一次，内容是当前这份计划。

计划是会话级的：同一段会话里后续的 run 沿用它并继续修改。是否使用计划由 Agent 自行决定，简单任务通常没有这个事件。

#### data 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `goal` | string | 计划要达成的目标，一句话 |
| `steps` | array | 有序的步骤列表，每一项的字段见下表 |

`steps` 的每一项：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 步骤标识，同一份计划内唯一 |
| `description` | string | 这一步做什么 |
| `status` | string | 执行状态。取值：`pending`（未开始）/ `in_progress`（进行中）/ `completed`（已完成） |

#### 示例

``` [事件流片段]
id: 1755229384902-9
event: plan
data: {"goal":"给客户 C-1024 出一份续约建议","steps":[{"id":"1","description":"查客户档案","status":"completed"},{"id":"2","description":"分析近半年工单","status":"in_progress"},{"id":"3","description":"出建议","status":"pending"}]}
```

#### 客户端怎么处理

每一条 `plan` 都是完整的计划，不是增量。收到就用它整个替换本地保存的那份，以最新一条为准；不要按 `id` 做合并，也不要把多条事件的 `steps` 拼接。断线续传会重放这些事件，按上面的规则处理天然幂等。

```js [示例代码]
function onPlan(data) {
  store.plan = data;                      // 整段覆盖,以最新一条为准
  const done = data.steps.filter((s) => s.status === "completed").length;
  $("#plan").innerHTML =
    `<div class="plan">
       <div class="plan-title">${esc(data.goal)}(${done}/${data.steps.length})</div>
       ${data.steps.map((s) => `<div class="step ${s.status}">${esc(s.description)}</div>`).join("")}
     </div>`;
}
```
````

- [ ] **Step 6: 3.5 接收器骨架(`sse-events.md:1088-1091`)**

把

```js
const HANDLERS = {                       // 每个处理函数见 3.4
  metadata: onMetadata, token: onToken, updates: onUpdates, worker: onWorker,
  guard: onGuard, compaction: onCompaction, approval: onApproval,
  retry: onRetry, error: onError, gap: onGap,
};
```

改为

```js
const HANDLERS = {                       // 每个处理函数见 3.4
  metadata: onMetadata, token: onToken, updates: onUpdates, plan: onPlan,
  worker: onWorker, guard: onGuard, compaction: onCompaction, approval: onApproval,
  retry: onRetry, error: onError, gap: onGap,
};
```

- [ ] **Step 7: 10.1 导语(`examples.md:18`)**

把「`metadata` / `updates` / `approval` / `retry` / `error` 这几类事件，示例中直接原样打印 `data` 字段」改为「`metadata` / `updates` / `plan` / `approval` / `retry` / `error` 这几类事件，示例中直接原样打印 `data` 字段」。四种语言的代码不用改:它们对未列举的事件都是原样打印。

- [ ] **Step 8: 死链脚本入仓**

新建 `apps/admin-ui/docs-site/scripts/check_links.py`(以前每次文档 PR 都在会话 scratchpad 里重造,这次入仓):

```python
"""Full dead-link check for the docs-site build: absolute /docs/ links, same-page #anchors,
AND relative ./x.html#anchor links. Run from the docs-site dir after ``pnpm build``:

    python3 scripts/check_links.py

Exit 1 when any link is dead.
"""
import os
import re
import sys
import urllib.parse
from posixpath import normpath

DIST = ".vitepress/dist"
pages: dict[str, set[str]] = {}
files: dict[str, str] = {}
for root, _, names in os.walk(DIST):
    for n in names:
        if not n.endswith(".html"):
            continue
        fp = os.path.join(root, n)
        html = open(fp, encoding="utf-8").read()
        rel = os.path.relpath(fp, DIST)  # e.g. guide/chat.html
        url = "/docs/" + rel
        pages[url] = {urllib.parse.unquote(i) for i in re.findall(r'\sid="([^"]+)"', html)}
        files[url] = html


def canon(path: str) -> str:
    """Normalize any page path to the '/docs/<rel>.html' key."""
    if path.endswith("/"):
        path += "index.html"
    if not path.endswith(".html"):
        path += ".html"
    return normpath(path)


SKIP = re.compile(r"^(https?:|mailto:|javascript:)|^/docs/assets/|\.(css|js|woff2?|png|svg|ico|json)$")
bad: list[tuple[str, str, str]] = []
total = 0
for url, html in files.items():
    base_dir = os.path.dirname(url)  # /docs/guide
    for href in re.findall(r'href="([^"]*)"', html):
        if SKIP.search(href.split("#")[0]) and not href.startswith("#"):
            continue
        if not (href.startswith("/docs/") or href.startswith("#") or href.startswith("./") or href.startswith("../")):
            continue
        total += 1
        path, _, frag = href.partition("#")
        frag = urllib.parse.unquote(frag)
        if not path:  # same-page
            target = url
        elif path.startswith("/docs/"):
            target = canon(path)
        else:  # relative
            target = canon(normpath(os.path.join(base_dir, path)))
        if target not in pages:
            bad.append((url, href, "PAGE"))
            continue
        if frag and frag not in pages[target]:
            bad.append((url, href, "锚点"))

print(f"{total} 条站内链接(含相对) / {len(pages)} 页")
seen = set()
for s, h, w in bad:
    key = (s, h)
    if key in seen:
        continue
    seen.add(key)
    print(f"  ❌ [{w}] {h} ← {s}")
print("✅ 无死链" if not bad else f"{len(seen)} 条死链(去重)")
sys.exit(1 if bad else 0)
```

- [ ] **Step 9: build + 死链 + 红线扫描 + 标点自检**

Run(在 `apps/admin-ui/docs-site`):

```bash
pnpm build && python3 scripts/check_links.py
```

Expected: build 通过;`✅ 无死链`(新增锚点 `#plan` 被 3.3 表引用,必须能解析)。

Run(仓库根):

```bash
rg -n "expert_work://|expert_work\.|control_plane\.|orchestrator\.|packages/|services/|\.py\b|sse\.py" apps/admin-ui/docs-site/guide/sse-events.md | rg -v "允许上传的扩展名"
```

Expected: 无输出(公开文档红线)。

再肉眼过一遍新节:正文全角标点(，：（）),代码块内 ASCII;标题无标点;字段表三列;`status` 三个取值穷举。

- [ ] **Step 10: Commit**

```bash
git add apps/admin-ui/docs-site/guide/sse-events.md apps/admin-ui/docs-site/guide/examples.md apps/admin-ui/docs-site/scripts/check_links.py
git commit -m "docs(external-api): SSE 第 3 章新增 plan 事件一节,全站事件表 / id 表 / 骨架同步;死链脚本入仓"
```

---

### Task 4: 上线与真栈验证(合并后)

**Files:** 无代码改动;产出 = 测试环境验证记录(进记录 PR 说明)。

**Interfaces:** 无。

- [ ] **Step 1: 发测试环境**

```bash
export KUBECONFIG=~/.kube/expert-work-test.yaml
tools/deploy/release.sh test 2>&1 | tee "$SCRATCH/release-<sha>.log"
```

Expected: 末尾 `SMOKE PASS`。发布后 `infra/k8s/overlays/test/kustomization.yaml` 里那条 `# Sandbox migration W1 Task 2 …` 注释会被挪到 `images:` 上方,手工挪回 control-plane `newTag` 下面。

- [ ] **Step 2: 在 control-plane pod 内跑探针(key 不出集群)**

把下面脚本存为 `$SCRATCH/probe_plan_event.py`,`kubectl -n expert-work cp` 进 control-plane pod 后 `kubectl exec … -- python /tmp/probe_plan_event.py`。它铸一把临时 write key、用完在 `finally` 撤销,只打印 PASS / FAIL 行,不打印 key:

```python
"""PR1 真栈探针:①流里出现 event: plan 且 payload 形状对;②replay 端点重放它;
③同一会话第二个 run 在 metadata 之后立刻补发 plan。跑在 control-plane pod 内。"""
import asyncio, json, urllib.error, urllib.request
from sqlalchemy import text
from control_plane.app import _build_sql_stores
from control_plane.auth.api_key_verifier import mint_api_key
from control_plane.settings import Settings
from expert_work.protocol import ApiKeyScope

BASE, AGENT, USER = "http://127.0.0.1:8000", "test-agent", "pr1-plan-probe"
ASK = ("请先调用 update_plan 工具,把「介绍三种常见排序算法」拆成 3 个步骤的计划,"
       "然后按计划逐步完成,每完成一步就用 update_plan 把该步标成 completed。")

def http(m, p, key, body=None, timeout=300):
    req = urllib.request.Request(BASE + p, method=m,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()

def parse(raw):
    out = []
    for b in raw.split("\n\n"):
        if not b.strip() or b.lstrip().startswith(":"): continue
        fid = ev = dt = None
        for ln in b.strip().split("\n"):
            if ln.startswith("id: "): fid = ln[4:]
            elif ln.startswith("event: "): ev = ln[7:]
            elif ln.startswith("data: "): dt = ln[6:]
        if ev: out.append((fid, ev, dt or ""))
    return out

def plan_ok(d):
    try:
        p = json.loads(d)
        return isinstance(p.get("goal"), str) and isinstance(p.get("steps"), list) and all(
            set(s) >= {"id", "description", "status"} and s["status"] in ("pending", "in_progress", "completed")
            for s in p["steps"])
    except Exception:
        return False

async def main():
    stores = _build_sql_stores(Settings()); key_id = tenant_id = None
    try:
        async with stores.session_factory() as s:
            sa = (await s.execute(text("select id, tenant_id from service_account where is_active order by created_at limit 1"))).first()
        sa_id, tenant_id = sa[0], sa[1]
        gen = mint_api_key(tenant_id=tenant_id)
        created = await stores.api_key.create(tenant_id=tenant_id, service_account_id=sa_id,
            prefix=gen.prefix, secret_hash=gen.secret_hash, scopes=[ApiKeyScope("write")],
            expires_at=None, created_by="pr1-plan-probe")
        key_id, key = created.id, gen.plaintext

        # ① 第一个 run:stream 模式,期望流里出现 plan
        st, hdr, raw = http("POST", f"/v1/agents/{AGENT}/runs", key,
                            {"input": ASK, "user_id": USER, "mode": "stream"})
        if st != 200:
            print("FAIL 建 run:", st, raw[:200]); return
        sid = hdr.get("X-Expert-Work-Session-Id") or hdr.get("x-expert-work-session-id")
        rid = hdr.get("X-Expert-Work-Run-Id") or hdr.get("x-expert-work-run-id")
        frames = parse(raw)
        names = [f[1] for f in frames]
        plans = [f for f in frames if f[1] == "plan"]
        print(f"run1 事件序列: {names}")
        print(f"{'PASS' if plans else 'FAIL'} ① 流里出现 plan({len(plans)} 条)")
        if plans:
            print(f"{'PASS' if all(plan_ok(f[2]) for f in plans) else 'FAIL'} ① payload 形状 goal/steps[id,description,status]")
            print(f"{'PASS' if all(f[0] for f in plans) else 'FAIL'} ① plan 帧带 id(可续传)")
        else:
            print("!! 模型这次没调用 update_plan,换个更硬的提示重跑一次再判"); return

        # ② replay 端点重放它
        st, _h, raw2 = http("GET", f"/v1/agents/{AGENT}/runs/{rid}/events?user_id={USER}", key)
        replayed = [f for f in parse(raw2) if f[1] == "plan"]
        print(f"{'PASS' if len(replayed) == len(plans) else 'FAIL'} ② replay 重放 plan({len(replayed)} vs {len(plans)})")

        # ③ 同一会话第二个 run:metadata 之后立刻补发
        st, _h, raw3 = http("POST", f"/v1/agents/{AGENT}/runs", key,
                            {"input": "谢谢,就这样。", "user_id": USER, "session_id": sid, "mode": "stream"})
        names3 = [f[1] for f in parse(raw3)]
        print(f"run2 事件序列: {names3}")
        ok3 = len(names3) >= 2 and names3[0] == "metadata" and names3[1] == "plan"
        print(f"{'PASS' if ok3 else 'FAIL'} ③ 第二个 run 在 metadata 之后立刻补发 plan")
    finally:
        if key_id: await stores.api_key.revoke(tenant_id=tenant_id, api_key_id=key_id); print("临时 key 已撤销", flush=True)
        await stores.engine.dispose()

asyncio.run(main())
```

Expected: 三个 PASS。①依赖模型真的调用 `update_plan`(默认对所有 agent 注册);模型没调就换更硬的提示重跑,不算失败。

- [ ] **Step 3: 文档站上线核对**

```bash
POD=$(kubectl -n expert-work get pods -o name | rg admin-ui | head -1)
kubectl -n expert-work exec "$POD" -- sh -c 'wget -qO- http://127.0.0.1:8080/docs/guide/sse-events.html | grep -c "id=\"plan\""'
```

Expected: `1`。

- [ ] **Step 4: 记录 PR**

按既有套路:分支 `chore/deploy-test-<sha>`,`kustomization.yaml` newTag 更新,PR 说明附探针三行 PASS 与事件序列,CI 绿后 squash-merge。
