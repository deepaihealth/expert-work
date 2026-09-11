# 按 run 的用量对外可见 —— 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让对接方拿到按 run 的、含整棵调用树的 token 用量 —— `end` 帧直接带,
外加一个 GET 端点兜底。

**Architecture:** 聚合能力已存在(`totals_by_trace_ids` + `TokenTotals.by_model`),
本次只做三件:给它加 `usage_kind` 过滤、做一个共享的 loader 闭包、把结果接到
`end_frame_data()` 的**四个**调用点与一个新端点上。**不加列、不迁移。**

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy 2.x async / pytest

**Spec:** `docs/superpowers/specs/2026-09-11-run-usage-external-design.md`

## Global Constraints

- **不加数据库列,不写迁移。** `token_usage.run_id` 和 `agent_run.usage_by_model` 都不加。
- **对外四档字段恒全给**:`input_tokens` / `output_tokens` / `cache_read_tokens` /
  `cache_creation_tokens`。不裁剪、不折算、不给金额。计价口径是对接方的事。
- **`llm_calls` 不对外。** 它是我方内部调用次数,对接方不需要,也不该知道。
- **缺席 ≠ 零。** 无记录时字段**缺席**,不是空数组、不是 0。逐字照抄 `artifacts` 的既有口径。
- **对外只取 `usage_kind='conversation'`。**
- **SQL 与 in-memory 两个 store 实现的谓词必须逐字同义** —— 本仓库反复踩过。
- 提交信息用中文 conventional commit;每条新断言必须 break→red→restore→green 自证,
  变异前 `git diff` 确认落地;**不用 `git checkout` / `git stash` 还原**,手写回原文。

---

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `packages/expert-work-persistence/src/expert_work/persistence/token_usage_store.py` | 聚合查询 | 抽象 + SQL + in-memory 三处加 `usage_kinds` 参数 |
| `services/control-plane/src/control_plane/api/_run_usage.py` | **新建** —— loader 闭包 + 对外序列化 | 单一入口,五个消费点全经过它 |
| `services/orchestrator/src/orchestrator/sse.py` | `end_frame_data` + `sse_consumer` | 加 `usage_by_model` 字段 + `load_usage` 参数 |
| `services/control-plane/src/control_plane/api/_run_event_stream.py` | 三个 end 点 | 加 `load_usage` 参数并在三处分支使用 |
| `services/control-plane/src/control_plane/api/runs.py` | 调用方 ×3 | 传 loader |
| `services/control-plane/src/control_plane/api/external_events.py` | 调用方 ×1 + 透传层 | 传 loader |
| `services/control-plane/src/control_plane/api/external_approvals.py` | 调用方 ×1 | 传 loader |
| `services/control-plane/src/control_plane/api/agents.py` | 调用方 ×1(经 `build_events_response`) | 传 loader |
| `services/control-plane/src/control_plane/api/external_runs.py` | 新端点 | `GET /{agent_code}/runs/{run_id}/usage` |
| `docs/api/*` | 对外文档 | 新增用量一节 |

---

## PR 切分

| PR | 内容 | 行为变更 |
|---|---|---|
| **PR1** | store 加 `usage_kinds` 过滤 + `_run_usage.py` loader/序列化 | **零** —— 新参数默认 `None`,无人调用 |
| **PR2** | `end_frame_data` 加字段 + 五个调用点接线 | 有 —— `end` 帧多一个字段 |
| **PR3** | GET 端点 + 对外文档 | 有 —— 新路由 |

PR1 可先合。PR2 与 PR3 顺序无关,但都依赖 PR1。

---

## Task 1:`totals_by_trace_ids` 加 `usage_kinds` 过滤

**Files:**
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/token_usage_store.py`
  (抽象 `:237`、in-memory `:339`、SQL `:557`)
- Test: `packages/expert-work-persistence/tests/test_token_usage_store.py`

**Interfaces:**
- Produces: `totals_by_trace_ids(trace_ids, *, usage_kinds: Sequence[str] | None = None)`
  —— `None` 不过滤(保持现有行为);给了就只计入这些 kind。

- [ ] **Step 1: 写失败测试**

在 `packages/expert-work-persistence/tests/test_token_usage_store.py` 追加:

```python
async def test_totals_by_trace_ids_filters_usage_kind(
    store: TokenUsageStore,
) -> None:
    """对外路径只算 conversation —— quality_sampling / skill_evolution 是平台
    自身开销,不能算到调用方头上。"""
    trace = "a" * 32
    tenant = uuid4()
    for kind, tokens in (("conversation", 100), ("quality_sampling", 7), ("skill_evolution", 3)):
        await store.insert(
            TokenUsageRecord(
                tenant_id=tenant,
                agent_name="a",
                agent_version="1",
                model="glm-5.3",
                provider="glm",
                trace_id=trace,
                usage_kind=kind,
                input_tokens=tokens,
                output_tokens=0,
            )
        )

    unfiltered = (await store.totals_by_trace_ids([trace]))[trace]
    assert unfiltered.input_tokens == 110, "默认不过滤 —— 控制台的现有行为不能变"

    filtered = (await store.totals_by_trace_ids([trace], usage_kinds=("conversation",)))[trace]
    assert filtered.input_tokens == 100
    assert [b.model for b in filtered.by_model] == ["glm-5.3"]
```

- [ ] **Step 2: 跑,确认红**

```bash
uv run --no-sync pytest packages/expert-work-persistence/tests/test_token_usage_store.py -k usage_kind -q
```

预期:`TypeError: ... unexpected keyword argument 'usage_kinds'`

- [ ] **Step 3: 三处实现**

抽象(`:237`)与两个实现的签名同步改成:

```python
async def totals_by_trace_ids(
    self, trace_ids: Sequence[str], *, usage_kinds: Sequence[str] | None = None
) -> dict[str, TokenTotals]:
```

in-memory(`:339`)在遍历里加一行,紧跟 `tid not in wanted` 那个 continue 之后:

```python
    async def totals_by_trace_ids(
        self, trace_ids: Sequence[str], *, usage_kinds: Sequence[str] | None = None
    ) -> dict[str, TokenTotals]:
        wanted = {t for t in trace_ids if t}
        kinds = set(usage_kinds) if usage_kinds else None      # ← 新增
        buckets: dict[tuple[str, str | None, str], ModelTokenTotals] = {}
        for r in self._rows:
            tid = r.trace_id
            if tid is None or tid not in wanted:
                continue
            if kinds is not None and r.usage_kind not in kinds:   # ← 新增
                continue
            # ↓ 以下到方法结尾原样不动(``key = (tid, r.provider, r.model)`` 起)
```

**只加两行**:`kinds = ...` 和那个 `continue`。方法其余部分一个字符都不要动。

SQL(`:557`)在 `.where(TokenUsageRow.trace_id.in_(ids))` 之后追加:

```python
        if usage_kinds:
            stmt = stmt.where(TokenUsageRow.usage_kind.in_(list(usage_kinds)))
```

**两个实现的谓词必须同义**:都是"`usage_kinds` 为空/None → 不过滤;否则只留集合内的"。

- [ ] **Step 4: 跑,确认绿**

```bash
uv run --no-sync pytest packages/expert-work-persistence/tests/test_token_usage_store.py -q
```

- [ ] **Step 5: 变异自证**

删掉 SQL 那个 `if usage_kinds:` 分支 → 跑 → 必须红在 `filtered.input_tokens == 100`。
`git diff` 确认变异落地后再读结果。手写还原。
同样对 in-memory 那个 continue 做一次。

- [ ] **Step 6: Postgres 集成测**

```bash
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
uv run --no-sync pytest packages/expert-work-persistence/tests/ -k "token_usage" -q
```

- [ ] **Step 7: 提交**

```bash
git add -A && git commit -m "feat(usage): totals_by_trace_ids 支持按 usage_kind 过滤

对外的按-run 用量只能算 conversation —— quality_sampling(质量抽检)与
skill_evolution(技能进化)是平台自身开销,算到调用方头上就是替我们的内部
流程收钱。默认 None 不过滤,保持控制台 Runs 列表/详情的现有行为。"
```

---

## Task 2:`_run_usage.py` —— 单一 loader 与对外序列化

**Files:**
- Create: `services/control-plane/src/control_plane/api/_run_usage.py`
- Test: `services/control-plane/tests/test_run_usage_loader.py`

**Interfaces:**
- Consumes: Task 1 的 `totals_by_trace_ids(..., usage_kinds=)`
- Produces:
  - `EXTERNAL_USAGE_KINDS: tuple[str, ...] = ("conversation",)`
  - `usage_buckets_to_wire(buckets: Sequence[ModelTokenTotals]) -> list[dict[str, Any]]`
  - `make_usage_loader(*, usage, runs, run_id, tenant_id, scope=None) -> Callable[[], Awaitable[list[dict[str, Any]] | None]]`

- [ ] **Step 1: 写失败测试**

`services/control-plane/tests/test_run_usage_loader.py`:

```python
"""B-52 —— 对外用量的单一装配口。

五个消费点(四个 end 帧分支 + GET 端点)全部经过这里,所以口径只有一处可能弄错。
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from control_plane.api._run_usage import (
    EXTERNAL_USAGE_KINDS,
    make_usage_loader,
    usage_buckets_to_wire,
)
from expert_work.persistence.token_usage_store import ModelTokenTotals


def test_wire_shape_drops_llm_calls_and_keeps_four_token_fields() -> None:
    """llm_calls 是我方内部调用次数,不对外;四档 token 一个都不能少。"""
    wire = usage_buckets_to_wire(
        [
            ModelTokenTotals(
                provider="glm",
                model="glm-5.3",
                input_tokens=100,
                output_tokens=20,
                cache_creation_tokens=0,
                cache_read_tokens=80,
                llm_calls=7,
            )
        ]
    )
    assert wire == [
        {
            "provider": "glm",
            "model": "glm-5.3",
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 80,
            "cache_creation_tokens": 0,
        }
    ]
    assert "llm_calls" not in wire[0]


def test_external_usage_kinds_is_conversation_only() -> None:
    assert EXTERNAL_USAGE_KINDS == ("conversation",)


async def test_loader_returns_none_when_run_has_no_trace_yet(
    run_store_with_queued_run,  # fixture: run 行存在但 trace_id 为 None
) -> None:
    """排队中的 run 还没绑 trace —— 返回 None(字段缺席),不是空列表(零用量)。"""
    runs, run_id, tenant_id = run_store_with_queued_run
    loader = make_usage_loader(
        usage=_EmptyUsageStore(), runs=runs, run_id=run_id, tenant_id=tenant_id
    )
    assert await loader() is None


async def test_loader_returns_none_when_run_row_vanished(
    empty_run_store,
) -> None:
    """run 行被 purge —— 同样是 None,不能抛。终局路径上抛异常会带崩整条流。"""
    loader = make_usage_loader(
        usage=_EmptyUsageStore(), runs=empty_run_store, run_id=uuid4(), tenant_id=uuid4()
    )
    assert await loader() is None
```

- [ ] **Step 2: 跑,确认红(模块不存在)**

```bash
uv run --no-sync pytest services/control-plane/tests/test_run_usage_loader.py -q
```

预期:`ModuleNotFoundError: control_plane.api._run_usage`

- [ ] **Step 3: 实现**

```python
"""B-52 —— 对外「按 run 的用量」的单一装配口。

五个消费点全部经过这里:``end`` 帧的四个分支(``sse_consumer`` /
replay / live-join probe / live 实时)与 ``GET .../runs/{id}/usage``。
``end_frame_data`` 的 docstring 记着两条 SSE 流的字段集合**已经分叉过一次**,
所以口径(取哪些 ``usage_kind``、露哪些字段、无记录时返回什么)只放一处。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any
from uuid import UUID

from expert_work.persistence import RunStore
from expert_work.persistence.token_usage_store import ModelTokenTotals, TokenUsageStore

logger = logging.getLogger(__name__)

#: 对外用量只算对话开销。``quality_sampling``(质量抽检)与 ``skill_evolution``
#: (技能进化)是平台自身的流程,算到调用方头上就是替我们的内部开销收钱。
EXTERNAL_USAGE_KINDS: tuple[str, ...] = ("conversation",)


def usage_buckets_to_wire(buckets: Sequence[ModelTokenTotals]) -> list[dict[str, Any]]:
    """把聚合桶转成对外形状。

    四档 token 恒全给 —— 计价口径是调用方的事,我方只负责计量完整准确。
    ``llm_calls`` **不对外**:那是我方内部的 LLM 调用次数,调用方不需要。
    """
    return [
        {
            "provider": b.provider,
            "model": b.model,
            "input_tokens": b.input_tokens,
            "output_tokens": b.output_tokens,
            "cache_read_tokens": b.cache_read_tokens,
            "cache_creation_tokens": b.cache_creation_tokens,
        }
        for b in buckets
    ]


def make_usage_loader(
    *,
    usage: TokenUsageStore,
    runs: RunStore,
    run_id: UUID,
    tenant_id: UUID,
    scope: Callable[[], AbstractAsyncContextManager[None]] | None = None,
) -> Callable[[], Awaitable[list[dict[str, Any]] | None]]:
    """工厂,与 :func:`make_run_probe` 同款:单次可用,每次调用重开 ``scope``。

    返回 ``None`` 表示**无记录**(帧上字段缺席),返回 ``[]`` 表示**确有其事的零用量**
    —— 两者不可混同,这是 ``artifacts`` 立下的既有口径。

    ``None`` 的三种来路:run 行不存在(已 purge)、``trace_id`` 尚未绑定(排队中)、
    该 trace 没有任何 usage 行(历史 run)。

    **绝不抛异常** —— 这个 loader 跑在 SSE 的终局路径上,抛出去会把整条流带崩。
    查询失败只记日志并返回 ``None``:语义恰好是"无记录",而 run 的终局状态照发。
    """

    async def _load() -> list[dict[str, Any]] | None:
        try:
            if scope is not None:
                async with scope():
                    row = await runs.get(run_id=run_id, tenant_id=tenant_id)
            else:
                row = await runs.get(run_id=run_id, tenant_id=tenant_id)
            if row is None or not row.trace_id:
                return None
            if scope is not None:
                async with scope():
                    totals = await usage.totals_by_trace_ids(
                        [row.trace_id], usage_kinds=EXTERNAL_USAGE_KINDS
                    )
            else:
                totals = await usage.totals_by_trace_ids(
                    [row.trace_id], usage_kinds=EXTERNAL_USAGE_KINDS
                )
            found = totals.get(row.trace_id)
            if found is None:
                return None
            return usage_buckets_to_wire(found.by_model)
        except Exception:
            logger.warning(  # codeql[py/log-injection]
                "run_usage.load_failed run_id=%s",
                run_id,  # codeql[py/log-injection]
                exc_info=True,
            )
            return None

    return _load
```

- [ ] **Step 4: 跑,确认绿**

```bash
uv run --no-sync pytest services/control-plane/tests/test_run_usage_loader.py -q
```

- [ ] **Step 5: 变异自证**

1. 把 `usage_kinds=EXTERNAL_USAGE_KINDS` 改成不传 → `test_external_usage_kinds_is_conversation_only`
   不会红(它只断言常量),**所以还要补一条端到端的 kind 过滤断言**在 Task 2 的测试里
   —— 用一个装了三种 kind 的假 store,断言 loader 只算 conversation。补完再跑变异。
2. 把 `return None`(row 为 None 那条)改成 `return []` → 必须红在
   `test_loader_returns_none_when_run_row_vanished`。
3. 把 `except Exception` 整块删掉,让 store 抛 → 必须红(loader 不得抛)。

每次变异前 `git diff` 确认落地,还原手写。

- [ ] **Step 6: 提交**

```bash
git add -A && git commit -m "feat(usage): 对外按-run 用量的单一装配口 _run_usage

五个消费点(四个 end 帧分支 + GET 端点)全经过这里。end_frame_data 的 docstring
记着两条 SSE 流的字段集合已经分叉过一次,所以口径只放一处:取哪些 usage_kind、
露哪些字段(llm_calls 不对外)、无记录返回 None 而不是空列表。

loader 绝不抛 —— 它跑在 SSE 终局路径上,抛出去会带崩整条流;失败只记日志并让
字段缺席,run 的终局状态照发。"
```

---

## Task 3:`end_frame_data` 加 `usage_by_model`

**Files:**
- Modify: `services/orchestrator/src/orchestrator/sse.py:1639`
- Test: `services/control-plane/tests/test_run_event_stream.py`

**Interfaces:**
- Produces: `end_frame_data(*, run_id, status, artifacts=None, usage_by_model=None)`

- [ ] **Step 1: 写失败测试**

```python
def test_end_frame_omits_usage_when_absent_and_includes_when_present() -> None:
    """缺席 ≠ 零 —— 与 artifacts 同一条口径(None → 字段缺席,不是 null 也不是 [])。"""
    from orchestrator.sse import end_frame_data

    run_id = uuid4()
    assert "usage_by_model" not in end_frame_data(run_id=run_id, status="success")

    empty = end_frame_data(run_id=run_id, status="success", usage_by_model=[])
    assert empty["usage_by_model"] == [], "空列表是「确有其事的零用量」,必须出现"

    one = end_frame_data(
        run_id=run_id,
        status="success",
        usage_by_model=[{"provider": "glm", "model": "glm-5.3", "input_tokens": 1}],
    )
    assert one["usage_by_model"][0]["model"] == "glm-5.3"
```

- [ ] **Step 2: 跑,确认红**

```bash
uv run --no-sync pytest services/control-plane/tests/test_run_event_stream.py -k end_frame_omits_usage -q
```

预期:`TypeError: ... unexpected keyword argument 'usage_by_model'`

- [ ] **Step 3: 实现**

在 `end_frame_data` 签名加参数,并在 `if artifacts is not None:` 之后照抄一段:

```python
    usage_by_model: list[dict[str, Any]] | None = None,
```

```python
    if usage_by_model is not None:
        data["usage_by_model"] = usage_by_model
```

docstring 追加一段,与 `artifacts` 那段并列:

```
``usage_by_model``(B-52)—— 本 run 按 ``(provider, model)`` 分桶的 token 用量,
含整棵调用树(worker 与父共用 trace)。``None``(无记录 / 未绑 trace / 查询失败)时
**字段缺席**而不是放 null,与 ``artifacts`` 同一口径:缺席 = 无记录,别当零用量。
```

- [ ] **Step 4: 跑,确认绿**

- [ ] **Step 5: 变异自证**

把 `if usage_by_model is not None:` 改成 `if usage_by_model:` → 必须红在
`empty["usage_by_model"] == []`(空列表被吞成字段缺席,正是"缺席≠零"要防的)。

- [ ] **Step 6: 提交**

---

## Task 4:四个 end 分支接线

**Files:**
- Modify: `services/orchestrator/src/orchestrator/sse.py`(`sse_consumer` 签名 + `:1595`)
- Modify: `services/control-plane/src/control_plane/api/_run_event_stream.py`
  (`build_event_producer` 签名 + `:288` / `:467` / `:499`)
- Test: `services/control-plane/tests/test_run_event_stream.py`

**Interfaces:**
- Consumes: Task 2 的 loader 类型 `Callable[[], Awaitable[list[dict] | None]] | None`
- Produces: `sse_consumer(..., load_usage=None)` 与
  `build_event_producer(..., load_usage=None)` 两个同名同型参数

- [ ] **Step 1: 写失败测试 —— 四个分支各一条**

**这是本计划最重要的一步。** `end_frame_data` 有四个调用点,只测其中一个是这个
仓库反复犯的错("执行入口三个,规矩只写一处就漏两个")。四条测试分别覆盖:

先写一个四条共用的桩与断言助手:

```python
_BUCKET = {
    "provider": "glm",
    "model": "glm-5.3",
    "input_tokens": 100,
    "output_tokens": 20,
    "cache_read_tokens": 80,
    "cache_creation_tokens": 0,
}


def _stub_loader() -> Callable[[], Awaitable[list[dict[str, Any]] | None]]:
    async def _load() -> list[dict[str, Any]] | None:
        return [dict(_BUCKET)]

    return _load


def _end_payload(chunks: list[bytes]) -> dict[str, Any]:
    """从 SSE 字节里取出 end 帧的 data。四条测试共用,免得各写各的解析。"""
    text = b"".join(chunks).decode()
    blocks = [b for b in text.split("\n\n") if "event: end" in b]
    assert blocks, f"没有 end 帧:{text[:400]}"
    line = next(ln for ln in blocks[-1].splitlines() if ln.startswith("data: "))
    return json.loads(line[len("data: ") :])
```

四条各自跑一个分支:

```python
# 1. sse_consumer —— 第三方主路径(POST .../runs 的 mode:"stream")
async def test_sse_consumer_end_frame_carries_usage() -> None:
    bridge = InMemoryStreamBridge()
    record = _make_run_record()
    await bridge.publish_end(record.run_id, status="success")
    chunks = [
        c
        async for c in sse_consumer(
            bridge=bridge,
            record=record,
            run_manager=_stub_run_manager(),
            is_disconnected=_never_disconnected,
            load_usage=_stub_loader(),
        )
    ]
    assert _end_payload(chunks)["usage_by_model"] == [_BUCKET]


# 2. replay —— run 已终态,从 run_event 表重放
async def test_replay_end_frame_carries_usage() -> None:
    plan = await build_event_producer(
        run_id=RUN_ID,
        run_status=RunStatus.SUCCESS,
        event_store=_store_with_events([]),
        stream_bridge=InMemoryStreamBridge(),
        since_seq=None,
        scope=None,
        load_usage=_stub_loader(),
    )
    chunks = [c async for c in plan.producer]
    assert _end_payload(chunks)["usage_by_model"] == [_BUCKET]


# 3. live-join probe —— bridge 里没有该 run 的发布者(被别的副本认领),走轮询
async def test_live_join_probe_end_frame_carries_usage() -> None:
    async def _probe() -> tuple[RunStatus, list[dict[str, Any]] | None]:
        return (RunStatus.SUCCESS, None)

    plan = await build_event_producer(
        run_id=RUN_ID,
        run_status=RunStatus.RUNNING,
        event_store=_store_with_events([]),
        stream_bridge=InMemoryStreamBridge(),   # 空 bridge → 落到 probe 分支
        run_probe=_probe,
        since_seq=None,
        scope=None,
        load_usage=_stub_loader(),
    )
    chunks = [c async for c in plan.producer]
    assert _end_payload(chunks)["usage_by_model"] == [_BUCKET]


# 4. live 实时 —— 订阅 bridge,收到 bridge 自己的 end
async def test_live_end_frame_carries_usage() -> None:
    bridge = InMemoryStreamBridge()
    plan = await build_event_producer(
        run_id=RUN_ID,
        run_status=RunStatus.RUNNING,
        event_store=_store_with_events([]),
        stream_bridge=bridge,
        since_seq=None,
        scope=None,
        load_usage=_stub_loader(),
    )
    await bridge.publish_end(RUN_ID, status="success")
    chunks = [c async for c in plan.producer]
    assert _end_payload(chunks)["usage_by_model"] == [_BUCKET]
```

**四条的分支入口各不相同**(`sse_consumer` 直调 / 终态走 replay / 空 bridge + probe /
活 bridge),这是它们能互相独立变红的前提 —— Step 6 会逐个证明。
桩(`_make_run_record` / `_stub_run_manager` / `_store_with_events` /
`_never_disconnected`)该文件已有,直接复用,**不要另建一套**。

再补一条**穷举钉**,防止将来新增第五个分支时漏掉:

```python
def test_every_end_frame_data_call_site_passes_usage() -> None:
    """AST 扫描:``end_frame_data(...)`` 的每个调用点都必须传 ``usage_by_model``。

    这个函数有四个调用点,分散在两个服务里。漏一个的后果是那条流的 end 帧少一个
    字段 —— 静默、且只有走到那条分支的消费者才会发现。
    """
    import ast, pathlib

    sources = [
        pathlib.Path("services/orchestrator/src/orchestrator/sse.py"),
        pathlib.Path("services/control-plane/src/control_plane/api/_run_event_stream.py"),
    ]
    missing = []
    seen = 0
    for path in sources:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "end_frame_data"
            ):
                seen += 1
                if not any(kw.arg == "usage_by_model" for kw in node.keywords):
                    missing.append(f"{path}:{node.lineno}")
    assert seen == 4, f"调用点数变了(找到 {seen} 个),这条测试的前提要重新核"
    assert missing == [], f"这些 end_frame_data 调用点漏传 usage_by_model: {missing}"
```

- [ ] **Step 2: 跑,确认五条全红**

```bash
uv run --no-sync pytest services/control-plane/tests/test_run_event_stream.py -k usage -q
```

- [ ] **Step 3: `sse_consumer` 接线(点 1)**

签名加:

```python
    load_usage: Callable[[], Awaitable[list[dict[str, Any]] | None]] | None = None,
```

`:1595` 那处改为:

```python
                yield format_sse(
                    "end",
                    end_frame_data(
                        run_id=record.run_id,
                        status=status,
                        artifacts=arts,
                        usage_by_model=(await load_usage()) if load_usage else None,
                    ),
                )
```

- [ ] **Step 4: `build_event_producer` 接线(点 2/3/4)**

签名加同名同型参数,三处调用同样加
`usage_by_model=(await load_usage()) if load_usage else None`。

**注意 `:288` 那处在 replay 分支**,它已有 `run_artifacts` 参数走"调用方预查"的路子;
usage 统一走 loader(每次现查),**不新增 `run_usage` 参数** —— 两条取数路径会分叉,
正是 `end_frame_data` 做成单一函数要防的事。

- [ ] **Step 5: 跑,确认五条全绿**

- [ ] **Step 6: 变异自证 —— 逐个分支**

四个调用点**逐个**把 `usage_by_model=...` 删掉,每次只删一个:
每次必须**恰好红对应的那一条**行为测试 **加上** AST 穷举那条。四次都做。
这证明四条测试各自咬住一个分支,不是互相兜底。

再做一次:把 AST 测试里的 `assert seen == 4` 改成 `assert seen >= 0`,并删掉一处
`usage_by_model=` → 穷举钉必须仍然红(证明它不是靠计数恒真)。

- [ ] **Step 7: 五个调用点传 loader**

- `runs.py:1297` / `runs.py:2129`(`sse_consumer`)
- `external_approvals.py:320`(`sse_consumer`)
- `runs.py:2031` / `external_events.py:119`(`build_event_producer`)
- `external_events.py` 的 `build_events_response` 透传层加参数,
  `agents.py:319` 经它传入

每处用 `make_usage_loader(usage=<token usage store>, runs=<run store>, run_id=..., tenant_id=...)`。
**console 路径(`runs.py`)的跨租户读要带 `scope=`**,与同处 `make_run_probe` 的
`scope` 参数取同一个值。

- [ ] **Step 8: 全量跑 + 提交**

```bash
uv run --no-sync pytest services/control-plane/tests/test_run_event_stream.py -q
uv run --no-sync pytest services/control-plane/tests/test_external_events.py -q
```

---

## Task 5:`GET /{agent_code}/runs/{run_id}/usage`

**Files:**
- Modify: `services/control-plane/src/control_plane/api/external_runs.py`
- Test: `services/control-plane/tests/test_external_runs.py`

**Interfaces:**
- Consumes: Task 2 的 `make_usage_loader`

- [ ] **Step 1: 写失败测试**

```python
async def test_run_usage_requires_user_id(client: AsyncClient) -> None:
    """user_id 必填无默认 —— 漏传是 422,不是「返回整个租户」。"""
    resp = await client.get(f"/v1/agents/{AGENT_CODE}/runs/{uuid4()}/usage")
    assert resp.status_code == 422


async def test_run_usage_foreign_run_is_404_not_empty(client: AsyncClient) -> None:
    """别人的 run 返 404 —— 空结果会泄露「这个 id 存在」。"""
    resp = await client.get(
        f"/v1/agents/{AGENT_CODE}/runs/{uuid4()}/usage", params={"user_id": "someone-else"}
    )
    assert resp.status_code == 404


async def test_run_usage_returns_buckets_and_status(client: AsyncClient) -> None:
    """正常路径:四档 token 全在、llm_calls 不在、run_status 带出来。"""
    run_id = await _seed_run_with_usage(
        client, user_id=USER_ID, model="glm-5.3", provider="glm"
    )
    resp = await client.get(
        f"/v1/agents/{AGENT_CODE}/runs/{run_id}/usage", params={"user_id": USER_ID}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run_status"] in {"success", "error", "cancelled", "timeout"}
    assert body["usage_by_model"][0]["model"] == "glm-5.3"
    assert "llm_calls" not in body["usage_by_model"][0]


async def test_run_usage_matches_the_end_frame_byte_for_byte(client: AsyncClient) -> None:
    """GET 与 end 帧对同一个 run 必须给出逐字段相同的 usage_by_model ——
    两条路分叉过一次,这条是看门狗。"""
    run_id = await _seed_run_with_usage(
        client, user_id=USER_ID, model="glm-5.3", provider="glm"
    )
    from_get = (
        await client.get(
            f"/v1/agents/{AGENT_CODE}/runs/{run_id}/usage", params={"user_id": USER_ID}
        )
    ).json()["usage_by_model"]

    # 同一个 run 重放一次 SSE,取它的 end 帧
    replay = await client.get(
        f"/v1/agents/{AGENT_CODE}/runs/{run_id}/events", params={"user_id": USER_ID}
    )
    from_end = _end_payload([replay.content])["usage_by_model"]

    assert from_get == from_end
```

`_seed_run_with_usage` 需要新建:建一个属于 `(USER_ID, AGENT_CODE)` 的终态 run
(带 `trace_id`),并往 token usage store 插两行 —— 一行 `conversation`、一行
`quality_sampling`,**后者用来顺带钉住过滤**(断言返回的 `input_tokens` 只含前者)。

- [ ] **Step 2: 跑,确认红(404 路由不存在)**

- [ ] **Step 3: 实现**

在 `build_external_runs_router()` 里,`list_runs` 之后加:

```python
    @router.get(
        "/{agent_code}/runs/{run_id}/usage",
        response_model=None,
        dependencies=[Depends(require("session", "read"))],
    )
    async def run_usage(
        agent_code: str,
        run_id: UUID,
        request: Request,
        runs: Annotated[RunStore, Depends(_get_run_store)],
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        usage: Annotated[TokenUsageStore, Depends(_get_token_usage_store)],
        user_id: Annotated[str, Query(min_length=1, max_length=255)],
    ) -> JSONResponse:
        """这个 run 的 token 用量,按 ``(provider, model)`` 分桶。

        含整棵调用树(worker 与主线共用 trace)。四档 token 恒全给 —— 计价口径是
        调用方的事。``usage_by_model`` **缺席**表示无记录,**空数组**表示确有其事的
        零用量,两者不可混同。

        run 未结束也可查,返回到目前为止的量 + ``run_status``,由调用方判断是否落账。
        """
        tenant_id: UUID = request.state.tenant_id
        try:
            run, _meta = await load_owned_run(
                tenant_id=tenant_id,
                agent_code=agent_code,
                user_id=user_id,
                run_id=run_id,
                runs=runs,
                threads=threads,
                users=users,
            )
        except ExternalScopeError as exc:
            return external_error(exc)

        loader = make_usage_loader(
            usage=usage, runs=runs, run_id=run_id, tenant_id=tenant_id
        )
        buckets = await loader()
        body: dict[str, Any] = {
            "run_id": str(run_id),
            "run_status": _EXTERNAL_RUN_STATUS.get(run.status, "error"),
        }
        if buckets is not None:
            body["usage_by_model"] = buckets
        return JSONResponse(content=body)
```

`_get_token_usage_store` 若不存在则照 `_get_run_store` 的样子加一个。
`_EXTERNAL_RUN_STATUS` 用该文件已有的状态映射;没有就复用
`orchestrator.sse.EXTERNAL_END_STATUSES` 的口径,**不要自创第二套状态词表**。

- [ ] **Step 4: 跑,确认绿**

- [ ] **Step 5: 变异自证**

1. 删掉 `user_id` 的 `Query` 必填(给个默认值)→ 必须红在 422 那条
2. 把 `except ExternalScopeError` 改成返回 200 空体 → 必须红在 404 那条
3. 在 wire 里加回 `llm_calls` → 必须红在 `"llm_calls" not in ...`

- [ ] **Step 6: 路由登记表**

`services/control-plane/tests/route_audit.py` 的对外路由手工表**有两张**
(gate + lockdown),新路由两张都要登记;`nul-guard` 那张只列 `agents.py` 自己四条,
**加了会红**(09-09 勘误)。

- [ ] **Step 7: 提交**

---

## Task 6:对外文档

**Files:**
- Modify: `docs/api/`(用量一节)
- Modify: `docs/api/streaming-events.md`(`end` 帧字段表)

- [ ] **Step 1: 先按 style-guide 自检**

规范在 `docs/superpowers/specs/2026-08-17-external-docs-style-guide.md`,
**改对外文档前先按它自检**:按读者任务排、字段表穷举取值、不造黑话。

- [ ] **Step 2: 写 `end` 帧新字段**

字段表加 `usage_by_model`,并写清三条:
- 四档 token 的含义,**明写 `input_tokens` 已包含 `cache_read_tokens`**
- **缺席 ≠ 零**
- 已含整棵调用树,**不要再累加 `worker` 事件的 `usage`**(会双计)

- [ ] **Step 3: 写 GET 端点一节**

- [ ] **Step 4: 顺手修一个已知的文档错**

`sse-events.md` 的 `usage_by_model` 示例写 `"provider":"zhipu"` —— **不是合法
`Provider` 取值**,真实数据是 `glm`。改掉。

- [ ] **Step 5: 提交**

---

## 自检清单(合并前)

- [ ] 四个 `end_frame_data` 调用点**全部**传了 `usage_by_model`,AST 穷举钉在守
- [ ] 四条分支行为测试各自变异过,**证明互不兜底**
- [ ] `llm_calls` 不出现在任何对外响应里
- [ ] 无记录 → 字段**缺席**;零用量 → **空数组**;两条各有测试
- [ ] `usage_kind` 过滤在 SQL 与 in-memory 两处谓词同义,各自变异过
- [ ] loader 在 store 抛异常时返回 `None` 而不是向上抛(终局路径不得带崩)
- [ ] 他人的 run → 404
- [ ] GET 与 `end` 帧对同一 run 逐字段相同
- [ ] 新路由已登记到 `route_audit.py` 的**两张**手工表
- [ ] 零迁移、零新列
