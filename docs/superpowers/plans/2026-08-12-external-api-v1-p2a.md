# 第三方对接 API v1 P2-a 实施计划(块 1–3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给对外 API 补齐消息级 `created_at`/`run_id`、会话级 `message_count`,把请求体做厚(`files[]`/`inputs`/幂等键),并把 `POST .../runs` 的 202 响应统一成信封。

**Architecture:** 消息元数据走**写入侧盖戳**(`additional_kwargs`,仓库现成惯用法);`message_count` 由 orchestrator `run_agent` 的 `finally` 单点重算后落 `thread_meta`;计数与列表共用一个纯函数,杜绝口径漂移。请求体三项均为向后兼容的新增,唯一破坏性改动是对外 202 的信封化。

**Tech Stack:** Python 3.13 / FastAPI / Pydantic v2 / SQLAlchemy 2 + Alembic / LangGraph / pytest

**Spec:** [`docs/superpowers/specs/2026-08-12-external-api-v1-p2-design.md`](../specs/2026-08-12-external-api-v1-p2-design.md)

## Global Constraints

- 时间格式一律 **ISO8601**(`datetime.isoformat()`),不用 unix 秒。
- 消息对象**不可变更新**:用 `model_copy(update=...)`,不原地改 `additional_kwargs`。
- 对外响应一律 `{success, data, error}` 信封;404 一律隐藏存在性。
- 对外读端点用 `include_hidden=False`,**永不**把编排脚手架泄给第三方。
- alembic `revision` 标识符 **≤32 字符**(`version_num` 上限)。迁移 head 当前是 `0143_egress_audit_scan_idx`。
- 新增 `additional_kwargs` 键一律 `expert_work_` 前缀。
- 不回填存量数据:老消息 `created_at`/`run_id` 返回 `null`;存量会话 `message_count` 为 `null`。
- **依赖方向**:`services/orchestrator` **不得** import `services/control_plane`。跨层能力一律走注入(照 `TrajectoryRecorder` 的先例)。
- 本地跑 integration 测试须先 `export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock`。
- 跑 orchestrator 测试须 `DOCKER_HOST= uv run pytest ...`(见既有约定)。

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `services/control-plane/src/control_plane/transcript.py` | checkpoint → 轮次抽取 | 拆出纯函数 `extract_turns` |
| `packages/expert-work-persistence/.../thread_message/base.py` | `MessageTurn` 契约 | 加 2 字段 |
| `services/control-plane/src/control_plane/api/runs.py` | 用户消息构造 / spawn_run / 幂等 / 信封 | 改 |
| `services/control-plane/src/control_plane/trigger_firing.py` | 定时触发的 graph_input | 盖戳 |
| `services/orchestrator/src/orchestrator/graph_builder/builder.py` | `agent_node` 助手消息 | 盖戳 |
| `services/orchestrator/src/orchestrator/graph_builder/_stamp.py` | 盖戳 helper(新) | 建 |
| `services/control-plane/src/control_plane/trigger_delivery.py` | 定时投递消息 + 计数同步 | 改 |
| `packages/expert-work-persistence/migrations/versions/0144_*.py` | `thread_meta.message_count` | 建 |
| `packages/expert-work-persistence/migrations/versions/0145_*.py` | `agent_run` 幂等两列 + 部分唯一索引 | 建 |
| `services/orchestrator/src/orchestrator/sse.py` | run 终局 recorder 注入点 | 改 |
| `services/control-plane/src/control_plane/thread_stats.py` | 计数 recorder 实现(新) | 建 |
| `services/control-plane/src/control_plane/api/agents.py` | `ExternalRunRequest` + 对外 run 端点 | 改 |
| `services/control-plane/src/control_plane/api/external_sessions.py` | 会话列表 / 消息列表 | 改 |

---

## Task 1: `transcript.py` 拆出纯函数

把「取 checkpoint」与「抽轮次」分开,让计数与列表**共用同一个定义**。纯重构,行为零变化。

**Files:**
- Modify: `services/control-plane/src/control_plane/transcript.py:35-106`
- Test: `services/control-plane/tests/test_transcript_extract.py`(新建)

**Interfaces:**
- Produces: `extract_turns(raw_messages: list[Any], *, include_hidden: bool = True) -> list[MessageTurn]` —— 纯函数,供 `read_turns` 与 Task 7 的计数 recorder 共用。

- [ ] **Step 1: 写失败测试**

```python
# services/control-plane/tests/test_transcript_extract.py
"""extract_turns —— transcript 抽取的纯函数形态(P2 Task 1)。"""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from control_plane.transcript import extract_turns


def test_extract_turns_keeps_only_human_and_ai_text() -> None:
    raw = [
        SystemMessage(content="你是助手"),
        HumanMessage(content="你好"),
        AIMessage(content="", tool_calls=[{"id": "1", "name": "t", "args": {}}]),
        ToolMessage(content="结果", tool_call_id="1"),
        AIMessage(content="答案"),
    ]
    turns = extract_turns(raw, include_hidden=False)
    assert [(t.seq, t.role, t.content) for t in turns] == [
        (1, "user", "你好"),
        (4, "assistant", "答案"),
    ]
    assert turns[1].channel == "final"


def test_extract_turns_hidden_filter() -> None:
    raw = [
        HumanMessage(content="你好"),
        HumanMessage(content="<recovery-advisory>", additional_kwargs={"expert_work_hide_from_ui": True}),
        AIMessage(content="答案"),
    ]
    assert len(extract_turns(raw, include_hidden=True)) == 3
    assert len(extract_turns(raw, include_hidden=False)) == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_transcript_extract.py -v`
Expected: FAIL — `ImportError: cannot import name 'extract_turns'`

- [ ] **Step 3: 实现**

把 `read_turns` 里从 `raw = ...` 之后到 `return out` 的全部逻辑原样搬进新函数,`read_turns` 只留取 blob + 调用:

```python
def extract_turns(raw_messages: list[Any], *, include_hidden: bool = True) -> list[MessageTurn]:
    """把检查点 ``messages`` 通道的原始消息抽成用户/助手文本轮次。

    从 :func:`read_turns` 拆出的纯函数(P2)。拆的目的是让「对外消息列表」
    与「会话 message_count」共用同一个定义 —— 镜像表那摊语义债的根因正是
    两套定义各写各的然后漂了。任何一侧改口径,另一侧自动跟随。
    """
    collected: list[tuple[int, str, str, bool, bool]] = []
    for seq, m in enumerate(raw_messages):
        ...  # 原 read_turns 的循环体,逐行搬,不改
    out: list[MessageTurn] = []
    ...  # 原 read_turns 的第二个循环,逐行搬,不改
    return out


async def read_turns(
    checkpointer: BaseCheckpointSaver[Any],
    thread_id: UUID,
    *,
    include_hidden: bool = True,
) -> list[MessageTurn]:
    """（原 docstring 保留不动）"""
    config: RunnableConfig = {"configurable": {"thread_id": str(thread_id), "checkpoint_ns": ""}}
    tup = await checkpointer.aget_tuple(config)
    if tup is None:
        return []
    raw = (tup.checkpoint.get("channel_values") or {}).get("messages", [])
    return extract_turns(raw, include_hidden=include_hidden)
```

同步改 `__all__`:`__all__ = ["extract_turns", "read_turns"]`。

- [ ] **Step 4: 跑新旧两套测试**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_transcript_extract.py tests/test_external_sessions.py -v`
Expected: 全 PASS（重构不改行为,既有测试必须原样绿）

- [ ] **Step 5: 提交**

```bash
git add services/control-plane/src/control_plane/transcript.py services/control-plane/tests/test_transcript_extract.py
git commit -m "refactor(control-plane): transcript 拆出 extract_turns 纯函数"
```

---

## Task 2: `MessageTurn` 扩两字段

**Files:**
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/thread_message/base.py:22-40`
- Test: `packages/expert-work-persistence/tests/test_message_turn_fields.py`(新建)

**Interfaces:**
- Produces: `MessageTurn.created_at: datetime | None`、`MessageTurn.run_id: UUID | None`,两者默认 `None`。

> ⚠️ `MessageTurn` 是 `transcript.py` 与**五个消费者**的共享契约:
> `quality_monitor_worker.py:294`、`transcript_mirror_sweep.py:157`、`trigger_delivery.py:142/172`、
> `api/runs.py:1405`、`api/external_sessions.py:215`。本任务只加可选字段(默认 `None`),
> 五处行为不变 —— Step 4 必须把五处的测试都跑一遍来证明这一点。

- [ ] **Step 1: 写失败测试**

```python
# packages/expert-work-persistence/tests/test_message_turn_fields.py
"""MessageTurn 的 P2 新增字段 —— 默认 None,不破坏既有构造。"""

from datetime import UTC, datetime
from uuid import uuid4

from expert_work.persistence import MessageTurn


def test_new_fields_default_to_none() -> None:
    turn = MessageTurn(seq=0, role="user", content="你好")
    assert turn.created_at is None
    assert turn.run_id is None


def test_new_fields_round_trip() -> None:
    now = datetime.now(UTC)
    rid = uuid4()
    turn = MessageTurn(seq=1, role="assistant", content="答案", channel="final", created_at=now, run_id=rid)
    assert turn.created_at == now
    assert turn.run_id == rid
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd packages/expert-work-persistence && DOCKER_HOST= uv run pytest tests/test_message_turn_fields.py -v`
Expected: FAIL — `TypeError: MessageTurn.__init__() got an unexpected keyword argument 'created_at'`

- [ ] **Step 3: 实现**

```python
    channel: str | None = None
    #: P2 —— 这条消息产生的时刻,来自写入侧盖的 ``expert_work_created_at``
    #: (ISO8601)。``None`` = 盖戳上线之前写入的消息(不回填,见 P2 spec §二)。
    created_at: datetime | None = None
    #: P2 —— 产生这条消息的 run。来自写入侧盖的 ``expert_work_run_id``。
    #: ``None`` 同上。
    run_id: UUID | None = None
```

- [ ] **Step 4: 跑测试 + 五个消费者的既有测试**

```bash
cd packages/expert-work-persistence && DOCKER_HOST= uv run pytest tests/test_message_turn_fields.py -v
cd ../../services/control-plane && DOCKER_HOST= uv run pytest \
  tests/test_transcript_extract.py tests/test_external_sessions.py \
  -k "transcript or session or mirror or trigger_delivery or quality" -v
```
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add packages/expert-work-persistence/src/expert_work/persistence/thread_message/base.py packages/expert-work-persistence/tests/test_message_turn_fields.py
git commit -m "feat(persistence): MessageTurn 加 created_at / run_id 两个可选字段"
```

---

## Task 3: 用户消息盖戳

**Files:**
- Create: `packages/expert-work-common/src/expert_work/common/message_stamp.py`
- Modify: `services/control-plane/src/control_plane/api/runs.py:305-338`（`build_run_graph_input`）
- Modify: `services/control-plane/src/control_plane/api/runs.py:861`、`services/control-plane/src/control_plane/run_queue_worker.py:266`（两个调用方传 `run_id`）
- Modify: `services/control-plane/src/control_plane/trigger_firing.py:252-260`
- Test: `services/control-plane/tests/test_message_stamp.py`(新建)

**Interfaces:**
- Produces:
  - `STAMP_CREATED_AT = "expert_work_created_at"`、`STAMP_RUN_ID = "expert_work_run_id"`
  - `stamp_message(msg: BaseMessage, *, run_id: str, now: datetime) -> BaseMessage` —— 返回**新**消息对象
  - `stamp_messages(msgs: Sequence[BaseMessage], *, run_id: str, now: datetime) -> list[BaseMessage]`
- Consumes: 无

> 放 `expert-work-common` 的理由:control-plane（用户消息）与 orchestrator（助手消息,Task 4）
> 都要用,而 orchestrator 不能 import control-plane。common 是两者共同的下游依赖。

- [ ] **Step 1: 写失败测试**

```python
# services/control-plane/tests/test_message_stamp.py
"""写入侧盖戳 helper（P2 Task 3）。"""

from datetime import UTC, datetime
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage

from expert_work.common.message_stamp import STAMP_CREATED_AT, STAMP_RUN_ID, stamp_message, stamp_messages


def test_stamp_adds_both_keys() -> None:
    now = datetime(2026, 8, 12, 3, 4, 5, tzinfo=UTC)
    rid = str(uuid4())
    out = stamp_message(HumanMessage(content="你好"), run_id=rid, now=now)
    assert out.additional_kwargs[STAMP_CREATED_AT] == now.isoformat()
    assert out.additional_kwargs[STAMP_RUN_ID] == rid


def test_stamp_does_not_mutate_original() -> None:
    original = HumanMessage(content="你好")
    stamp_message(original, run_id="r", now=datetime.now(UTC))
    assert STAMP_CREATED_AT not in original.additional_kwargs


def test_stamp_preserves_existing_kwargs() -> None:
    original = AIMessage(content="答案", additional_kwargs={"expert_work_hide_from_ui": True})
    out = stamp_message(original, run_id="r", now=datetime.now(UTC))
    assert out.additional_kwargs["expert_work_hide_from_ui"] is True
    assert out.additional_kwargs[STAMP_RUN_ID] == "r"


def test_stamp_messages_stamps_all() -> None:
    now = datetime.now(UTC)
    out = stamp_messages([HumanMessage(content="a"), AIMessage(content="b")], run_id="r", now=now)
    assert all(m.additional_kwargs[STAMP_RUN_ID] == "r" for m in out)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_message_stamp.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'expert_work.common.message_stamp'`

- [ ] **Step 3: 实现 helper**

```python
# packages/expert-work-common/src/expert_work/common/message_stamp.py
"""写入侧给消息盖时间戳 / run 归属（P2 块 2）。

对外 ``GET .../sessions/{id}/messages`` 要给每条消息 ``created_at`` 与
``run_id``,而 LangGraph 检查点本身不存这两样。补法是在**写入时**把它们塞进
``additional_kwargs`` —— 这是本仓库现成惯用法(``expert_work_hide_from_ui`` /
``expert_work_scheduled_delivery`` / ``expert_work_source_run_id`` 都是这么塞的),
读取侧 ``transcript.extract_turns`` 只读不算。

放在 common 而非任一 service:用户消息在 control-plane 盖,助手消息在
orchestrator 盖,而 orchestrator 不能 import control-plane。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from langchain_core.messages import BaseMessage

#: 消息产生时刻,ISO8601 字符串。
STAMP_CREATED_AT = "expert_work_created_at"
#: 产生这条消息的 run id,字符串形态的 UUID。
STAMP_RUN_ID = "expert_work_run_id"

__all__ = ["STAMP_CREATED_AT", "STAMP_RUN_ID", "stamp_message", "stamp_messages"]


def stamp_message(msg: BaseMessage, *, run_id: str, now: datetime) -> BaseMessage:
    """返回盖好戳的**新**消息,原对象不动(不可变约定)。

    已有的 ``additional_kwargs`` 原样保留 —— 盖戳绝不能顶掉
    ``expert_work_hide_from_ui`` 这类既有标记,否则脚手架会漏给第三方。
    """
    merged = {**msg.additional_kwargs, STAMP_CREATED_AT: now.isoformat(), STAMP_RUN_ID: run_id}
    return msg.model_copy(update={"additional_kwargs": merged})


def stamp_messages(
    msgs: Sequence[BaseMessage], *, run_id: str, now: datetime
) -> list[BaseMessage]:
    """``stamp_message`` 的批量形态。"""
    return [stamp_message(m, run_id=run_id, now=now) for m in msgs]
```

- [ ] **Step 4: 接到用户消息的三个构造点**

`api/runs.py` —— `build_run_graph_input` 加 `run_id` 参数并盖在 HumanMessage 上（SystemMessage 不盖,它被 `extract_turns` 滤掉）:

```python
def build_run_graph_input(
    built: Any,
    *,
    input_text: str | None,
    image_refs: list[str],
    untrusted_content: list[str] | None,
    inputs: dict[str, Any] | None = None,
    run_id: UUID | None = None,          # P2 —— 盖戳用
) -> dict[str, Any]:
    human = _build_human_message(
        input_text=input_text,
        image_refs=image_refs,
        supports_vision=built.supports_vision,
        untrusted_content=untrusted_content,
        spotlight_nonce=built.spotlight_nonce,
    )
    if run_id is not None:
        human = stamp_message(human, run_id=str(run_id), now=datetime.now(UTC))
    return {
        "messages": [SystemMessage(content=render_system_prompt(built, inputs or {})), human],
        "step_count": 0,
        "max_steps": built.max_steps,
        "max_no_progress": built.max_no_progress,
    }
```

两个调用方各加一行 `run_id=run_id`（`api/runs.py:861` 用局部的 `run_id`;`run_queue_worker.py:266` 用 `run.run_id`）。

`trigger_firing.py:252-260` —— 它自己拼 graph_input,单独盖:

```python
    graph_input = {
        "messages": [
            SystemMessage(content=built.system_prompt),
            stamp_message(HumanMessage(content=seed_text), run_id=str(run_id), now=datetime.now(UTC)),
        ],
        "step_count": 0,
        "max_steps": built.max_steps,
        "max_no_progress": built.max_no_progress,
    }
```

- [ ] **Step 5: 加集成断言并跑**

```python
# 追加到 services/control-plane/tests/test_message_stamp.py
def test_build_run_graph_input_stamps_human_only() -> None:
    from uuid import uuid4
    from control_plane.api.runs import build_run_graph_input

    class _Built:
        supports_vision = False
        spotlight_nonce = None
        max_steps = 10
        max_no_progress = 3
        system_prompt = "sys"

    rid = uuid4()
    gi = build_run_graph_input(
        _Built(), input_text="你好", image_refs=[], untrusted_content=None, run_id=rid
    )
    system, human = gi["messages"]
    assert STAMP_RUN_ID not in system.additional_kwargs
    assert human.additional_kwargs[STAMP_RUN_ID] == str(rid)
```

> ⚠️ `_Built` 桩若与 `render_system_prompt` 的真实要求不符,改用真实的 `BuiltAgent`
> 构造或既有 fixture —— **不要**为了让测试过而放宽断言。

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_message_stamp.py -v`
Expected: 全 PASS

- [ ] **Step 6: 提交**

```bash
git add packages/expert-work-common/src/expert_work/common/message_stamp.py \
        services/control-plane/src/control_plane/api/runs.py \
        services/control-plane/src/control_plane/run_queue_worker.py \
        services/control-plane/src/control_plane/trigger_firing.py \
        services/control-plane/tests/test_message_stamp.py
git commit -m "feat(control-plane): 用户消息写入侧盖 created_at / run_id"
```

---

## Task 4: 助手消息盖戳

**Files:**
- Modify: `services/orchestrator/src/orchestrator/graph_builder/builder.py:1089-1094`、`:1116-1122`
- Modify: `services/control-plane/src/control_plane/trigger_delivery.py:85-96`
- Test: `services/orchestrator/tests/test_agent_node_stamp.py`(新建)

**Interfaces:**
- Consumes: Task 3 的 `stamp_messages` / `STAMP_RUN_ID` / `STAMP_CREATED_AT`
- Produces: 无新接口

> ⚠️ **盖戳必须放在最后、紧挨 `return`**。`response` 在 DLP 重写、
> `_reconcile_parsed_after_rewrite`、judge 那几步会被**重新绑定**;盖早了会被后面的
> 重绑覆盖掉,而且测试很容易在 happy path 上看不出来。

- [ ] **Step 1: 写失败测试**

```python
# services/orchestrator/tests/test_agent_node_stamp.py
"""agent_node 产出的助手消息带 run_id / created_at（P2 Task 4）。"""

import pytest
from expert_work.common.message_stamp import STAMP_CREATED_AT, STAMP_RUN_ID


@pytest.mark.asyncio
async def test_agent_node_stamps_response(build_minimal_agent) -> None:
    """跑一次真 graph,断言落进 messages 通道的 AIMessage 带两个戳。

    用真 graph 而非手工 fixture:reconcile / 盖戳这类不变式在手工对齐的
    fixture 下会假绿（既有教训 —— 见 memory「reconcile 类不变式要驱动真
    graph 集成测」）。
    """
    run_id = "11111111-1111-1111-1111-111111111111"
    built = build_minimal_agent()
    state = await built.graph.ainvoke(
        {"messages": [], "step_count": 0, "max_steps": 2, "max_no_progress": 2},
        {"configurable": {"thread_id": "t", "run_id": run_id}},
    )
    ai = [m for m in state["messages"] if m.type == "ai"]
    assert ai, "graph 没产出助手消息,测试本身无效"
    for m in ai:
        assert m.additional_kwargs[STAMP_RUN_ID] == run_id
        assert m.additional_kwargs[STAMP_CREATED_AT]
```

> `build_minimal_agent` 若无现成 fixture,照 `services/orchestrator/tests/` 里已有的
> graph 构造 fixture（搜 `conftest.py` 中的 agent 构造 helper）复用,**不要**新造一套桩。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_agent_node_stamp.py -v`
Expected: FAIL — `KeyError: 'expert_work_run_id'`

- [ ] **Step 3: 实现（`agent_node` 两个 return）**

中间件路径（`builder.py:1089` 附近,`update_mw` 之前）:

```python
            persisted_messages: list[BaseMessage] = list(new_messages)
            if advisory_message is not None and advisory_message not in persisted_messages:
                persisted_messages = [advisory_message, *persisted_messages]
            # P2 —— 盖戳必须在最后:上面 DLP / 结构化重发 / judge 都可能重绑 response。
            _run_id = current_run_id(config)
            if _run_id is not None:
                persisted_messages = stamp_messages(
                    persisted_messages, run_id=_run_id, now=datetime.now(UTC)
                )
```

无中间件路径（`builder.py:1116` 附近）:

```python
        emit_messages: list[BaseMessage] = (
            [advisory_message, response] if advisory_message is not None else [response]
        )
        # P2 —— 同上,盖在最后。
        _run_id = current_run_id(config)
        if _run_id is not None:
            emit_messages = stamp_messages(emit_messages, run_id=_run_id, now=datetime.now(UTC))
```

`trigger_delivery.py:85` 的投递消息（它已经在塞 `expert_work_source_run_id`,补两个标准键）:

```python
    message = AIMessage(
        content=result_text,
        additional_kwargs={
            "expert_work_scheduled_delivery": True,
            "expert_work_source_run_id": str(source_run_id),
            "expert_work_trigger_id": str(trigger_id),
            STAMP_CREATED_AT: datetime.now(UTC).isoformat(),
            STAMP_RUN_ID: str(source_run_id),
        },
    )
```

- [ ] **Step 4: 变异自验 —— 证明测试真能杀掉退化**

把 Step 3 中间件路径的盖戳整段**临时注释掉**,重跑测试:

Run: `cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_agent_node_stamp.py -v`
Expected: **FAIL**。若仍 PASS,说明测试没走到中间件分支 —— 必须改测试让它覆盖到,再恢复代码。

> 这一步不可省。既有教训:同一任务内出现过四次「全绿的重言式断言」,每条新断言都要
> break→red→restore→green 自证。

- [ ] **Step 5: 恢复代码,跑全测试**

Run: `cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_agent_node_stamp.py tests/test_builder.py -v`
Expected: 全 PASS

- [ ] **Step 6: 提交**

```bash
git add services/orchestrator/src/orchestrator/graph_builder/builder.py \
        services/control-plane/src/control_plane/trigger_delivery.py \
        services/orchestrator/tests/test_agent_node_stamp.py
git commit -m "feat(orchestrator): 助手消息写入侧盖 created_at / run_id"
```

---

## Task 5: 读出侧 —— 抽取盖的戳并对外暴露

**Files:**
- Modify: `services/control-plane/src/control_plane/transcript.py`（`extract_turns`）
- Modify: `services/control-plane/src/control_plane/api/external_sessions.py:219`
- Test: `services/control-plane/tests/test_transcript_extract.py`（追加）、`services/control-plane/tests/test_external_sessions.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `extract_turns`、Task 2 的 `MessageTurn` 两字段、Task 3 的两个键常量

- [ ] **Step 1: 写失败测试**

```python
# 追加到 services/control-plane/tests/test_transcript_extract.py
from datetime import UTC, datetime
from uuid import uuid4

from expert_work.common.message_stamp import STAMP_CREATED_AT, STAMP_RUN_ID


def test_extract_turns_reads_stamps() -> None:
    now = datetime(2026, 8, 12, 1, 2, 3, tzinfo=UTC)
    rid = uuid4()
    raw = [
        HumanMessage(
            content="你好",
            additional_kwargs={STAMP_CREATED_AT: now.isoformat(), STAMP_RUN_ID: str(rid)},
        ),
        AIMessage(content="没戳的老消息"),
    ]
    turns = extract_turns(raw, include_hidden=False)
    assert turns[0].created_at == now
    assert turns[0].run_id == rid
    assert turns[1].created_at is None
    assert turns[1].run_id is None


def test_extract_turns_tolerates_corrupt_stamp() -> None:
    """坏戳退化成 None,绝不让一条脏消息炸掉整个会话的读取。"""
    raw = [HumanMessage(content="你好", additional_kwargs={STAMP_CREATED_AT: "不是时间", STAMP_RUN_ID: "不是uuid"})]
    turn = extract_turns(raw, include_hidden=False)[0]
    assert turn.created_at is None
    assert turn.run_id is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_transcript_extract.py -v`
Expected: FAIL — `AssertionError`（`created_at` 是 `None`）

- [ ] **Step 3: 实现**

在 `extract_turns` 的第一个循环里把两个戳一起收进 `collected`,第二个循环构造 `MessageTurn` 时传入。解析用容错 helper:

```python
def _parse_stamp_created_at(ak: dict[str, Any]) -> datetime | None:
    raw = ak.get(STAMP_CREATED_AT)
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        # 脏戳退化成 None —— 一条坏消息不能让整个会话读不出来。
        return None


def _parse_stamp_run_id(ak: dict[str, Any]) -> UUID | None:
    raw = ak.get(STAMP_RUN_ID)
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None
```

- [ ] **Step 4: 对外端点暴露两字段**

`external_sessions.py:219`:

```python
        out = [
            {
                "role": t.role,
                "content": t.content,
                "channel": t.channel,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "run_id": str(t.run_id) if t.run_id else None,
            }
            for t in page
        ]
```

- [ ] **Step 5: 跑测试**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_transcript_extract.py tests/test_external_sessions.py tests/test_external_api_contract.py -v`
Expected: 全 PASS

- [ ] **Step 6: 提交**

```bash
git add services/control-plane/src/control_plane/transcript.py \
        services/control-plane/src/control_plane/api/external_sessions.py \
        services/control-plane/tests/test_transcript_extract.py services/control-plane/tests/test_external_sessions.py
git commit -m "feat(control-plane): 对外消息端点暴露 created_at / run_id"
```

---

## Task 6: `thread_meta.message_count` 迁移 + store

**Files:**
- Create: `packages/expert-work-persistence/migrations/versions/0144_thread_meta_msg_count.py`
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/models/thread_meta.py`
- Modify: `packages/expert-work-protocol/src/expert_work/protocol/thread_meta.py`
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/thread_meta/{base,sql,memory}.py`
- Test: `packages/expert-work-persistence/tests/test_thread_meta_message_count.py`(新建)

**Interfaces:**
- Produces: `ThreadMetaStore.update_message_count(thread_id: UUID, count: int, *, tenant_id: UUID) -> bool`;`ThreadMeta.message_count: int | None`

> ⚠️ 列**不给 `server_default`**。存量行必须留 `NULL`（= 尚未算过）,被填成 `0` 就与
> 「真的空会话」分不开了。新建会话由 `create` 显式写 `0`。
> ⚠️ SQL 与 in-memory 两个 store 的谓词必须**字节级同义**（既有命门教训）。

- [ ] **Step 1: 写失败测试**

```python
# packages/expert-work-persistence/tests/test_thread_meta_message_count.py
"""thread_meta.message_count —— 新建写 0、存量留 None、更新可回读。"""

import pytest
from uuid import uuid4


@pytest.mark.asyncio
async def test_create_defaults_to_zero(thread_meta_store) -> None:
    tid, tenant = uuid4(), uuid4()
    meta = await thread_meta_store.create(thread_id=tid, tenant_id=tenant, created_by="u")
    assert meta.message_count == 0


@pytest.mark.asyncio
async def test_update_message_count_round_trip(thread_meta_store) -> None:
    tid, tenant = uuid4(), uuid4()
    await thread_meta_store.create(thread_id=tid, tenant_id=tenant, created_by="u")
    assert await thread_meta_store.update_message_count(tid, 7, tenant_id=tenant) is True
    got = await thread_meta_store.get(tid, tenant_id=tenant)
    assert got is not None and got.message_count == 7


@pytest.mark.asyncio
async def test_update_message_count_cross_tenant_is_noop(thread_meta_store) -> None:
    tid, tenant = uuid4(), uuid4()
    await thread_meta_store.create(thread_id=tid, tenant_id=tenant, created_by="u")
    assert await thread_meta_store.update_message_count(tid, 7, tenant_id=uuid4()) is False
    got = await thread_meta_store.get(tid, tenant_id=tenant)
    assert got is not None and got.message_count == 0
```

> `thread_meta_store` fixture 必须**同时**参数化 SQL 与 in-memory 两个实现 —— 照
> `packages/expert-work-persistence/tests/` 里既有 store 测试的参数化写法。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd packages/expert-work-persistence && export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock && uv run pytest tests/test_thread_meta_message_count.py -v`
Expected: FAIL — `AttributeError: 'ThreadMeta' object has no attribute 'message_count'`

- [ ] **Step 3: 写迁移**

```python
# packages/expert-work-persistence/migrations/versions/0144_thread_meta_msg_count.py
"""thread_meta.message_count —— 对外会话列表的消息条数（P2 块 2）。

口径是**第三方可见**的条数（``include_hidden=False``),由 run 终局重算后写入。
刻意不给 server_default:存量行留 NULL 表示"尚未算过",与"真的是空会话"(0)
区分开 —— 填成 0 会让前端把没算过的会话显示成空会话。

Revision ID: 0144_thread_meta_msg_count
Revises: 0143_egress_audit_scan_idx
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0144_thread_meta_msg_count"
down_revision: str | Sequence[str] | None = "0143_egress_audit_scan_idx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_TABLE = "thread_meta"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("message_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, "message_count")
```

- [ ] **Step 4: 改模型 / 协议 / 三个 store**

- `models/thread_meta.py`：`message_count: Mapped[int | None] = mapped_column(Integer, nullable=True)`（**不加 server_default**）
- `protocol/thread_meta.py`：`message_count: int | None = Field(default=None, description="第三方可见口径的消息条数;None=尚未算过")`
- `thread_meta/base.py`：加抽象方法 `update_message_count`,docstring 写明口径与 `None` 语义
- `thread_meta/sql.py`：`create` 里写 `message_count=0`;`update_message_count` 用 `update(...).where(thread_id==, tenant_id==)`,返回 `rowcount > 0`
- `thread_meta/memory.py`：同谓词（`thread_id` 且 `tenant_id` 都匹配才改),返回 `bool`

- [ ] **Step 5: 跑测试**

Run: `cd packages/expert-work-persistence && export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock && uv run pytest tests/test_thread_meta_message_count.py -v`
Expected: 全 PASS（SQL 与 in-memory 两套参数化都过）

- [ ] **Step 6: 提交**

```bash
git add packages/expert-work-persistence packages/expert-work-protocol
git commit -m "feat(persistence): thread_meta 加 message_count 列 + update_message_count"
```

---

## Task 7: run 终局重算计数

**Files:**
- Create: `services/control-plane/src/control_plane/thread_stats.py`
- Modify: `services/orchestrator/src/orchestrator/sse.py`（Protocol + `finally` 里 dispatch）
- Modify: 6 个 `run_agent(...)` 调用点传 recorder：`api/runs.py:730`、`api/runs.py:884`、`run_queue_worker.py:300`、`trigger_firing.py:280`、`orphan_sweep.py:354`
- Modify: `services/control-plane/src/control_plane/trigger_delivery.py`（投递后同步更新）
- Test: `services/orchestrator/tests/test_thread_stats_dispatch.py`、`services/control-plane/tests/test_thread_stats_recorder.py`

**Interfaces:**
- Produces:
  - orchestrator 侧 Protocol：`ThreadStatsRecorder.record(thread_id: UUID, tenant_id: UUID, messages: list[BaseMessage]) -> None`
  - control-plane 侧实现：`ThreadStatsRecorderImpl(threads: ThreadMetaStore)`
- Consumes: Task 1 的 `extract_turns`、Task 6 的 `update_message_count`

> ⚠️ **依赖方向**：orchestrator 只定义 Protocol 并调用,**实现在 control-plane**
> （它才能 import `extract_turns`）。照 `TrajectoryRecorder` 的现成范式。
> ⚠️ dispatch 放 `finally`（`sse.py:762`）—— 一处覆盖全部 6 个 `run_agent` 调用方
> 与全部终局分支。用 `asyncio.create_task` fire-and-forget,不 await,
> 这样 `asyncio.CancelledError` 拆除路径也不会被它拖住。

- [ ] **Step 1: 写失败测试（control-plane 侧实现）**

```python
# services/control-plane/tests/test_thread_stats_recorder.py
"""计数 recorder —— 口径必须与对外消息端点一致（include_hidden=False）。"""

import pytest
from uuid import uuid4
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from control_plane.thread_stats import ThreadStatsRecorderImpl


@pytest.mark.asyncio
async def test_records_visible_turn_count(thread_meta_store) -> None:
    tid, tenant = uuid4(), uuid4()
    await thread_meta_store.create(thread_id=tid, tenant_id=tenant, created_by="u")
    recorder = ThreadStatsRecorderImpl(threads=thread_meta_store)
    await recorder.record(
        thread_id=tid,
        tenant_id=tenant,
        messages=[
            HumanMessage(content="你好"),
            ToolMessage(content="工具结果", tool_call_id="1"),   # 不计
            HumanMessage(content="脚手架", additional_kwargs={"expert_work_hide_from_ui": True}),  # 不计
            AIMessage(content="答案"),
        ],
    )
    got = await thread_meta_store.get(tid, tenant_id=tenant)
    assert got is not None and got.message_count == 2


@pytest.mark.asyncio
async def test_record_swallows_store_failure(thread_meta_store, caplog) -> None:
    """best-effort：store 炸了也不能把 run 的终局路径带崩。"""
    class _Boom:
        async def update_message_count(self, *a, **k):
            raise RuntimeError("boom")

    recorder = ThreadStatsRecorderImpl(threads=_Boom())
    await recorder.record(thread_id=uuid4(), tenant_id=uuid4(), messages=[])  # 不抛
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_thread_stats_recorder.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'control_plane.thread_stats'`

- [ ] **Step 3: 实现 recorder**

```python
# services/control-plane/src/control_plane/thread_stats.py
"""run 终局重算会话的对外可见消息条数（P2 块 2）。

**重算而非累加**：run 会重试、会被打断、会在审批处暂停后续跑,累加型计数器在
这些路径上必然漂。重算天然自愈 —— 任何一次成功的 run 终局都会把计数纠正回来。

口径与对外消息端点严格一致（``include_hidden=False`` + 同一个 ``extract_turns``）。
两处若各写各的就会漂 —— 那正是 thread_message 镜像表已经犯过的错。
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from langchain_core.messages import BaseMessage

from control_plane.transcript import extract_turns
from expert_work.persistence import ThreadMetaStore

logger = logging.getLogger("expert_work.control_plane.thread_stats")

__all__ = ["ThreadStatsRecorderImpl"]


class ThreadStatsRecorderImpl:
    """把重算出的条数写回 ``thread_meta.message_count``。Best-effort。"""

    def __init__(self, *, threads: ThreadMetaStore | Any) -> None:
        self._threads = threads

    async def record(
        self, *, thread_id: UUID, tenant_id: UUID, messages: list[BaseMessage]
    ) -> None:
        try:
            count = len(extract_turns(list(messages), include_hidden=False))
            await self._threads.update_message_count(thread_id, count, tenant_id=tenant_id)
        except Exception:
            # 计数是展示增强,绝不能影响 run 的终局。漏了的会话在下次 run 自愈。
            logger.warning("thread_stats.record_failed thread=%s", thread_id, exc_info=True)
```

- [ ] **Step 4: orchestrator 侧 Protocol + `finally` dispatch**

`sse.py` 顶部加 Protocol（`@runtime_checkable` **不要**加 —— 既有陷阱）:

```python
class ThreadStatsRecorder(Protocol):
    """run 终局重算会话消息条数。实现在 control-plane（它才能 import
    ``transcript.extract_turns``）；orchestrator 只调用,不反向依赖。"""

    async def record(
        self, *, thread_id: UUID, tenant_id: UUID, messages: list[BaseMessage]
    ) -> None: ...
```

`run_agent` 加参数 `thread_stats_recorder: ThreadStatsRecorder | None = None`,`finally` 块内（`bridge.publish_end` 之后、`reset_current_run_id` 之前）:

```python
        _dispatch_thread_stats(thread_stats_recorder, graph, effective_config, record)
```

`_dispatch_thread_stats` 照 `_dispatch_trajectory` 写：`recorder is None` 直接 return；否则
`asyncio.create_task` 一个带 `asyncio.timeout` 的后台体,内部复用现成的
`_fetch_final_messages(graph, config)` 与 `_tenant_id_from_config(config) or record.tenant_id`,
异常只记日志。任务引用存进模块级 `set` 防 GC（照 `_BACKGROUND_TRAJECTORY_TASKS`）。

- [ ] **Step 5: 接 6 个调用点 + 投递路径**

5 个 `run_agent(...)` 调用处各加 `thread_stats_recorder=runtime.thread_stats_recorder`
（`AgentRuntime` 上加这个属性,在 `app.py` 组装时用 `ThreadStatsRecorderImpl(threads=...)` 注入）。

`trigger_delivery.inject_delivery` 在 `aupdate_state` 之后同步更新一次（它在 run 之外追加消息,
不经 `run_agent` 的 `finally`）。

- [ ] **Step 6: 变异自验 + 跑测试**

先把 `finally` 里那行 dispatch 注释掉,跑 orchestrator 的 dispatch 测试确认 **FAIL**；恢复后再跑全绿。

```bash
cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_thread_stats_dispatch.py -v
cd ../control-plane && DOCKER_HOST= uv run pytest tests/test_thread_stats_recorder.py -v
```

- [ ] **Step 7: 提交**

```bash
git add services/control-plane/src/control_plane/thread_stats.py \
        services/orchestrator/src/orchestrator/sse.py services/control-plane/src \
        services/orchestrator/tests services/control-plane/tests
git commit -m "feat: run 终局重算并落 thread_meta.message_count"
```

---

## Task 8: 对外会话列表暴露 `message_count`

**Files:**
- Modify: `services/control-plane/src/control_plane/api/external_sessions.py:148-158`
- Test: `services/control-plane/tests/test_external_sessions.py`（追加）

- [ ] **Step 1: 写失败测试**

```python
# 追加到 services/control-plane/tests/test_external_sessions.py
@pytest.mark.asyncio
async def test_sessions_list_exposes_message_count(external_client, seeded_session) -> None:
    resp = await external_client.get(
        f"/v1/agents/{seeded_session.agent_code}/sessions",
        params={"user_id": seeded_session.user_id},
    )
    assert resp.status_code == 200
    item = resp.json()["data"]["sessions"][0]
    assert "message_count" in item
    assert item["message_count"] == seeded_session.expected_count
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_external_sessions.py -k message_count -v`
Expected: FAIL — `KeyError: 'message_count'`

- [ ] **Step 3: 实现**

```python
                "running": row.thread_id in inflight,
                "message_count": row.message_count,   # None = 尚未算过（存量会话）
```

- [ ] **Step 4: 跑测试**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_external_sessions.py tests/test_external_api_contract.py -v`
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add services/control-plane/src/control_plane/api/external_sessions.py services/control-plane/tests/test_external_sessions.py
git commit -m "feat(control-plane): 对外会话列表暴露 message_count"
```

---

## Task 9: `ExternalRunRequest.inputs` 透传

**Files:**
- Modify: `services/control-plane/src/control_plane/api/agents.py:418-430`、`:945` 附近（拼 `RunRequest` 处）
- Test: `services/control-plane/tests/test_external_run_inputs.py`(新建)

**Interfaces:**
- Produces: `ExternalRunRequest.inputs: dict[str, Any]`

- [ ] **Step 1: 写失败测试**

```python
# services/control-plane/tests/test_external_run_inputs.py
"""对外 run 端点透传 inputs（模板变量）。校验逻辑复用内部现成的。"""

import pytest


@pytest.mark.asyncio
async def test_inputs_reaches_prompt_render(external_client, jinja_agent) -> None:
    resp = await external_client.post(
        f"/v1/agents/{jinja_agent.code}/runs",
        json={"user_id": "u1", "input": "hi", "mode": "queue", "inputs": {"lang": "zh"}},
    )
    assert resp.status_code == 202


@pytest.mark.asyncio
async def test_undeclared_input_key_is_422(external_client, jinja_agent) -> None:
    resp = await external_client.post(
        f"/v1/agents/{jinja_agent.code}/runs",
        json={"user_id": "u1", "input": "hi", "mode": "queue", "inputs": {"没声明的键": "x"}},
    )
    assert resp.status_code == 422
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_external_run_inputs.py -v`
Expected: FAIL — 422（`extra="forbid"` 拒了未知字段 `inputs`）

- [ ] **Step 3: 实现**

`ExternalRunRequest` 加：

```python
    #: P2 —— 提示词模板变量,与内部 ``RunRequest.inputs`` 同语义（未声明键 422、
    #: 必填缺失 422、64 键 / 单值 8192 字符上限）。校验在 ``spawn_run`` 内部由
    #: ``validate_prompt_inputs`` 统一执行,此处不重复。
    inputs: dict[str, Any] = Field(default_factory=dict)
```

`agents.py:945` 拼 `RunRequest` 处加 `inputs=payload.inputs`。

- [ ] **Step 4: 跑测试**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_external_run_inputs.py tests/test_external_api_contract.py -v`
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add services/control-plane/src/control_plane/api/agents.py services/control-plane/tests/test_external_run_inputs.py
git commit -m "feat(control-plane): 对外 run 端点透传 inputs 模板变量"
```

---

## Task 10: `files[]` —— 模型与图片分发

**Files:**
- Modify: `services/control-plane/src/control_plane/api/agents.py`（`ExternalFileRef` 新模型 + `ExternalRunRequest.files`）
- Test: `services/control-plane/tests/test_external_run_files.py`(新建)

**Interfaces:**
- Produces:
  - `ExternalFileRef`：`type: Literal["image","document"]`、`transfer_method: Literal["local_file"]`、`upload_id: str`
  - `ExternalRunRequest.files: list[ExternalFileRef]`（`max_length=64`）

> `transfer_method` 当前唯一合法值是 `local_file`。字段**现在就带上**：日后开
> `remote_url` 只是加一个枚举值（向后兼容）；现在省掉则日后要改形状（破坏性）。

- [ ] **Step 1: 写失败测试**

```python
# services/control-plane/tests/test_external_run_files.py
"""files[] —— 统一图片 / 文档引用（P2 块 1）。"""

import pytest


@pytest.mark.asyncio
async def test_image_file_ref_merges_into_image_refs(external_client, vision_agent, uploaded_image) -> None:
    resp = await external_client.post(
        f"/v1/agents/{vision_agent.code}/runs",
        json={
            "user_id": "u1", "input": "看图", "mode": "queue",
            "files": [{"type": "image", "transfer_method": "local_file", "upload_id": uploaded_image.uri}],
        },
    )
    assert resp.status_code == 202


@pytest.mark.asyncio
async def test_unknown_transfer_method_is_422(external_client, vision_agent) -> None:
    resp = await external_client.post(
        f"/v1/agents/{vision_agent.code}/runs",
        json={
            "user_id": "u1", "input": "x", "mode": "queue",
            "files": [{"type": "image", "transfer_method": "remote_url", "upload_id": "http://x"}],
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_foreign_thread_image_ref_rejected(external_client, vision_agent, other_thread_image) -> None:
    """图片 ref 内嵌 thread_id —— 引用别的会话的图必须被现成的 _validate_image_refs 拦下。"""
    resp = await external_client.post(
        f"/v1/agents/{vision_agent.code}/runs",
        json={
            "user_id": "u1", "input": "x", "mode": "queue",
            "files": [{"type": "image", "transfer_method": "local_file", "upload_id": other_thread_image.uri}],
        },
    )
    assert resp.status_code in (400, 422)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_external_run_files.py -v`
Expected: FAIL — 422（未知字段 `files`）

- [ ] **Step 3: 实现模型 + 图片分发**

```python
class ExternalFileRef(BaseModel):
    """一条附件引用。``upload_id`` 是 ``POST /v1/agents/{code}/uploads`` 的返回值。

    ``transfer_method`` 目前只有 ``local_file``。字段现在就存在是为了日后加
    ``remote_url`` 时只是扩枚举（向后兼容),而不是改形状（破坏性）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["image", "document"]
    transfer_method: Literal["local_file"] = "local_file"
    upload_id: str = Field(min_length=1, max_length=1024)
```

`ExternalRunRequest` 加 `files: list[ExternalFileRef] = Field(default_factory=list, max_length=64)`。

`agents.py:945` 拼 `RunRequest` 之前，把 `type == "image"` 的 `upload_id` 并进 `image_refs`：

```python
    image_refs = [*payload.image_refs, *(f.upload_id for f in payload.files if f.type == "image")]
```

图片侧不写新校验 —— `spawn_run` 里现成的 `_validate_image_refs` 会做 thread 绑定、条数、
`supports_vision` 三重校验,合并后自动生效。

- [ ] **Step 4: 跑测试**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_external_run_files.py -v`
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add services/control-plane/src/control_plane/api/agents.py services/control-plane/tests/test_external_run_files.py
git commit -m "feat(control-plane): 对外 run 请求体加 files[]（图片路径）"
```

---

## Task 11: `files[]` —— 文档分发与路径闸

**Files:**
- Modify: `services/control-plane/src/control_plane/api/runs.py`（`_build_human_message` 加 `document_names`）
- Modify: `services/control-plane/src/control_plane/api/agents.py`（文档名净化 + 透传）
- Test: `services/control-plane/tests/test_external_run_files.py`（追加）

**Interfaces:**
- Consumes: Task 10 的 `ExternalFileRef`
- Produces: `_build_human_message(..., document_names: list[str] | None = None)`

> ⚠️ **安全闸**：客户端给的是字符串。上传时走过 `_safe_workspace_name` 净化,
> run 这侧必须**再净化一遍** —— 只接受纯文件名,含路径分隔符或 `..` 一律 422。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 services/control-plane/tests/test_external_run_files.py
import pytest


@pytest.mark.parametrize("bad", ["../etc/passwd", "a/b.txt", "..", "", "  ", "x\\y.txt"])
@pytest.mark.asyncio
async def test_document_path_traversal_rejected(external_client, plain_agent, bad) -> None:
    resp = await external_client.post(
        f"/v1/agents/{plain_agent.code}/runs",
        json={
            "user_id": "u1", "input": "x", "mode": "queue",
            "files": [{"type": "document", "transfer_method": "local_file", "upload_id": bad}],
        },
    )
    assert resp.status_code == 422, f"{bad!r} 应被拒"


@pytest.mark.asyncio
async def test_document_name_lands_in_prompt(plain_agent) -> None:
    from control_plane.api.runs import _build_human_message

    msg = _build_human_message(
        input_text="总结这份文件",
        image_refs=[],
        supports_vision=False,
        document_names=["合同.pdf"],
    )
    assert "[file attached: 合同.pdf]" in msg.content
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_external_run_files.py -k document -v`
Expected: FAIL — `TypeError: unexpected keyword argument 'document_names'`

- [ ] **Step 3: 实现净化闸**

```python
def _safe_document_name_or_422(name: str) -> str:
    """校验第三方回填的文档 ``upload_id``（= 工作区里的纯文件名）。

    上传时已经过 ``_safe_workspace_name`` 净化,但那是**上传路径**的保证;
    run 请求体里的这个字符串是客户端自己给的,必须独立校验 —— 否则
    ``../`` 就能读到工作区外。
    """
    cleaned = name.strip()
    if not cleaned or cleaned != PurePosixPath(cleaned).name or cleaned in {".", ".."}:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_FILE_REF", "message": "document upload_id must be a bare filename"},
        )
    if "\\" in cleaned or "/" in cleaned:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_FILE_REF", "message": "document upload_id must not contain path separators"},
        )
    return cleaned
```

- [ ] **Step 4: 接进 `_build_human_message` 与对外端点**

`_build_human_message` 加 `document_names: list[str] | None = None`；在拼 `mentions` 的同一
处追加 `[file attached: <name>]` 行（无图片时也要生效 —— 文档与图片各自独立）。
`build_run_graph_input` 同步加 `document_names` 参数并透传。
`agents.py` 里把 `type == "document"` 的 `upload_id` 逐个过 `_safe_document_name_or_422` 后传下去。

- [ ] **Step 5: 变异自验 + 跑测试**

把 `_safe_document_name_or_422` 里的 `cleaned != PurePosixPath(cleaned).name` 判断临时改成
`False`,重跑参数化测试确认 **FAIL**（至少 `../etc/passwd` 与 `a/b.txt` 两例)；恢复后全绿。

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_external_run_files.py -v`
Expected: 全 PASS

- [ ] **Step 6: 提交**

```bash
git add services/control-plane/src/control_plane/api/runs.py services/control-plane/src/control_plane/api/agents.py services/control-plane/tests/test_external_run_files.py
git commit -m "feat(control-plane): files[] 文档分发 + 路径净化闸"
```

---

## Task 12: 幂等键 —— 迁移与 store

**Files:**
- Create: `packages/expert-work-persistence/migrations/versions/0145_agent_run_idempotency.py`
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/models/agent_run.py`
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/runs/schemas.py`（`RunInfo`）
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/runs/store.py`（SQL + in-memory）
- Test: `packages/expert-work-runtime/tests/test_run_store_idempotency.py`(新建)

**Interfaces:**
- Produces:
  - `RunInfo.idempotency_key: str | None = None`、`RunInfo.request_digest: str | None = None`
  - `RunStore.find_by_idempotency_key(tenant_id: UUID, key: str) -> RunInfo | None`
  - `RunStore.create` 在唯一键冲突时抛 `RunIdempotencyConflict`

> ⚠️ 唯一索引建在 `agent_run` 上 —— **占键与建 run 行是同一次插入,天然原子**。
> 不要实现成「先查键表、再建 run」：并发下会留下抢键失败但 run 行已建的孤儿。

- [ ] **Step 1: 写失败测试**

```python
# packages/expert-work-runtime/tests/test_run_store_idempotency.py
"""幂等键：同租户同键只能有一行；并发第二插必须抛冲突。"""

import pytest
from uuid import uuid4

from expert_work.runtime.runs import RunIdempotencyConflict


@pytest.mark.asyncio
async def test_same_key_second_insert_conflicts(run_store, make_run_info) -> None:
    tenant = uuid4()
    await run_store.create(make_run_info(tenant_id=tenant, idempotency_key="k1", request_digest="d1"))
    with pytest.raises(RunIdempotencyConflict):
        await run_store.create(make_run_info(tenant_id=tenant, idempotency_key="k1", request_digest="d1"))


@pytest.mark.asyncio
async def test_same_key_different_tenant_is_allowed(run_store, make_run_info) -> None:
    await run_store.create(make_run_info(tenant_id=uuid4(), idempotency_key="k1", request_digest="d1"))
    await run_store.create(make_run_info(tenant_id=uuid4(), idempotency_key="k1", request_digest="d1"))


@pytest.mark.asyncio
async def test_null_key_rows_do_not_collide(run_store, make_run_info) -> None:
    """部分唯一索引只覆盖非 NULL —— 不带 key 的普通 run 必须能建任意多个。"""
    tenant = uuid4()
    for _ in range(3):
        await run_store.create(make_run_info(tenant_id=tenant, idempotency_key=None))


@pytest.mark.asyncio
async def test_find_by_key(run_store, make_run_info) -> None:
    tenant = uuid4()
    info = make_run_info(tenant_id=tenant, idempotency_key="k1", request_digest="d1")
    await run_store.create(info)
    got = await run_store.find_by_idempotency_key(tenant_id=tenant, key="k1")
    assert got is not None and got.run_id == info.run_id and got.request_digest == "d1"
    assert await run_store.find_by_idempotency_key(tenant_id=tenant, key="nope") is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd packages/expert-work-runtime && export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock && uv run pytest tests/test_run_store_idempotency.py -v`
Expected: FAIL — `ImportError: cannot import name 'RunIdempotencyConflict'`

- [ ] **Step 3: 写迁移**

```python
# packages/expert-work-persistence/migrations/versions/0145_agent_run_idempotency.py
"""agent_run 幂等键（P2 块 1-C）。

部分唯一索引只覆盖非 NULL 的键 —— 不带 Idempotency-Key 的普通 run 不受影响,
可以建任意多个。唯一索引落在 agent_run 上,所以"占键"与"建 run 行"是同一次
插入,天然原子;不需要单独的键表,也就没有"抢键失败但 run 已建"的孤儿。

不设 TTL：agent_run 行本就为计费/分析永久保留（user_purge 对它是 ANONYMIZE
不是 DELETE),索引只覆盖真带 key 的行,不额外撑存储。

Revision ID: 0145_agent_run_idempotency
Revises: 0144_thread_meta_msg_count
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0145_agent_run_idempotency"
down_revision: str | Sequence[str] | None = "0144_thread_meta_msg_count"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

_TABLE = "agent_run"
_INDEX = "uq_agent_run_tenant_idempotency_key"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("idempotency_key", sa.Text(), nullable=True))
    op.add_column(_TABLE, sa.Column("request_digest", sa.Text(), nullable=True))
    op.create_index(
        _INDEX,
        _TABLE,
        ["tenant_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, "request_digest")
    op.drop_column(_TABLE, "idempotency_key")
```

- [ ] **Step 4: 改模型 / RunInfo / 两个 store**

- `models/agent_run.py`：两列 + `Index(..., unique=True, postgresql_where=text("idempotency_key IS NOT NULL"))`
- `runs/schemas.py`：`RunInfo` 加两个 `= None` 字段
- `runs/store.py` SQL 侧：`create` 捕 `IntegrityError` 且约束名匹配 `_INDEX` → `raise RunIdempotencyConflict`；新增 `find_by_idempotency_key`
- `runs/store.py` in-memory 侧：**同谓词** —— 遍历时按 `(tenant_id, idempotency_key)` 且 `key is not None` 判重,行为与 SQL 字节级同义
- 新异常 `RunIdempotencyConflict` 定义在 `runs/` 并从包 `__init__` 导出

- [ ] **Step 5: 跑测试（两套 store 参数化都要过）**

Run: `cd packages/expert-work-runtime && export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock && uv run pytest tests/test_run_store_idempotency.py -v`
Expected: 全 PASS

- [ ] **Step 6: 提交**

```bash
git add packages/expert-work-persistence packages/expert-work-runtime
git commit -m "feat(runtime): agent_run 幂等键两列 + 部分唯一索引 + find_by_idempotency_key"
```

---

## Task 13: 幂等键 —— 端点判定（queue 模式）

**Files:**
- Modify: `services/control-plane/src/control_plane/api/agents.py`（对外 run 端点读 header + 判定）
- Create: `services/control-plane/src/control_plane/api/_idempotency.py`
- Test: `services/control-plane/tests/test_external_idempotency.py`(新建)

**Interfaces:**
- Produces: `request_digest(payload: BaseModel) -> str`、`IDEMPOTENCY_HEADER = "Idempotency-Key"`
- Consumes: Task 12 的 `find_by_idempotency_key` / `RunIdempotencyConflict`

- [ ] **Step 1: 写失败测试**

```python
# services/control-plane/tests/test_external_idempotency.py
"""Idempotency-Key —— 同键同体返回原 run；同键异体 422；并发单赢家。"""

import asyncio
import pytest

BODY = {"user_id": "u1", "input": "你好", "mode": "queue"}


@pytest.mark.asyncio
async def test_same_key_same_body_returns_same_run(external_client, plain_agent) -> None:
    url = f"/v1/agents/{plain_agent.code}/runs"
    h = {"Idempotency-Key": "order-8899"}
    first = await external_client.post(url, json=BODY, headers=h)
    second = await external_client.post(url, json=BODY, headers=h)
    assert first.status_code == 202 and second.status_code == 202
    assert first.json()["data"]["run_id"] == second.json()["data"]["run_id"]


@pytest.mark.asyncio
async def test_same_key_different_body_is_422(external_client, plain_agent) -> None:
    url = f"/v1/agents/{plain_agent.code}/runs"
    h = {"Idempotency-Key": "order-8899"}
    await external_client.post(url, json=BODY, headers=h)
    resp = await external_client.post(url, json={**BODY, "input": "换了内容"}, headers=h)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.asyncio
async def test_no_key_creates_distinct_runs(external_client, plain_agent) -> None:
    url = f"/v1/agents/{plain_agent.code}/runs"
    a = await external_client.post(url, json=BODY)
    b = await external_client.post(url, json=BODY)
    assert a.json()["data"]["run_id"] != b.json()["data"]["run_id"]


@pytest.mark.asyncio
async def test_concurrent_same_key_single_winner(external_client, plain_agent) -> None:
    url = f"/v1/agents/{plain_agent.code}/runs"
    h = {"Idempotency-Key": "race-1"}
    a, b = await asyncio.gather(
        external_client.post(url, json=BODY, headers=h),
        external_client.post(url, json=BODY, headers=h),
    )
    assert {a.status_code, b.status_code} == {202}
    assert a.json()["data"]["run_id"] == b.json()["data"]["run_id"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_external_idempotency.py -v`
Expected: FAIL — 两次调用返回不同 run_id

- [ ] **Step 3: 实现指纹**

```python
# services/control-plane/src/control_plane/api/_idempotency.py
"""Idempotency-Key 支持（P2 块 1-C）。

指纹存的是**规范化请求体**的 sha256,不含 header 里的 key 本身。存指纹而非只
认 key 的理由：第三方改了 ``input`` 却忘换 key 时,只认 key 的实现会静默返回旧
run 的结果 —— 调用方以为发了新活,拿回来的是旧答案,而且没有任何信号。
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel

IDEMPOTENCY_HEADER = "Idempotency-Key"
MAX_IDEMPOTENCY_KEY_LEN = 255

__all__ = ["IDEMPOTENCY_HEADER", "MAX_IDEMPOTENCY_KEY_LEN", "request_digest"]


def request_digest(payload: BaseModel) -> str:
    """规范化请求体的 sha256（键排序,不转义非 ASCII）。"""
    body = payload.model_dump(mode="json")
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: 接进对外 run 端点**

端点签名加 `idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None`。
在**任何副作用之前**：

1. `key is None` → 走原路径,不碰任何幂等逻辑
2. 空白 key 或超 `MAX_IDEMPOTENCY_KEY_LEN` → 422 `INVALID_IDEMPOTENCY_KEY`
3. `find_by_idempotency_key` 命中：
   - `request_digest` 相同 → 直接返回原 run 的响应（不新建）
   - 不同 → 422 `IDEMPOTENCY_KEY_REUSED`
4. 未命中 → 带上 `idempotency_key` + `request_digest` 走正常创建；
   捕 `RunIdempotencyConflict` → 重查一次并返回赢家（并发单赢家）

- [ ] **Step 5: 跑测试**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_external_idempotency.py -v`
Expected: 全 PASS

- [ ] **Step 6: 提交**

```bash
git add services/control-plane/src/control_plane/api/_idempotency.py services/control-plane/src/control_plane/api/agents.py services/control-plane/tests/test_external_idempotency.py
git commit -m "feat(control-plane): 对外 run 端点支持 Idempotency-Key（queue 模式）"
```

---

## Task 14: 幂等键 —— stream 模式重放

**Files:**
- Modify: `services/control-plane/src/control_plane/api/external_events.py`（抽出可复用的响应构造）
- Modify: `services/control-plane/src/control_plane/api/agents.py`
- Test: `services/control-plane/tests/test_external_idempotency.py`（追加）

**Interfaces:**
- Consumes: Task 13 的判定流程
- Produces: `build_events_response(...) -> StreamingResponse | JSONResponse` —— 从 `external_events.py` 的端点体抽出,供重放复用

> stream 模式重试打过来时原 run 可能早已终态,没法「重新流一遍」。
> 复用 `GET .../runs/{run_id}/events` 已有的「终态 replay / 活跃 live-attach」能力,
> 客户端那一次 POST 重试透明拿到同一个流。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 services/control-plane/tests/test_external_idempotency.py
@pytest.mark.asyncio
async def test_stream_replay_attaches_to_original_run(external_client, plain_agent) -> None:
    url = f"/v1/agents/{plain_agent.code}/runs"
    h = {"Idempotency-Key": "stream-1"}
    body = {"user_id": "u1", "input": "你好", "mode": "stream"}
    first = await external_client.post(url, json=body, headers=h)
    original = first.headers["X-Expert-Work-Run-Id"]
    second = await external_client.post(url, json=body, headers=h)
    assert second.headers["X-Expert-Work-Run-Id"] == original
    assert second.headers["X-Expert-Work-Stream-Mode"] in {"replay", "live"}


@pytest.mark.asyncio
async def test_stream_replay_degrades_without_event_store(external_client_no_event_store, plain_agent) -> None:
    """部署未配 run_event_store 时退回 JSON + run_id,不是 500。"""
    url = f"/v1/agents/{plain_agent.code}/runs"
    h = {"Idempotency-Key": "stream-2"}
    body = {"user_id": "u1", "input": "你好", "mode": "stream"}
    first = await external_client_no_event_store.post(url, json=body, headers=h)
    second = await external_client_no_event_store.post(url, json=body, headers=h)
    assert second.status_code == 200
    assert second.json()["data"]["run_id"] == first.headers["X-Expert-Work-Run-Id"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_external_idempotency.py -k stream -v`
Expected: FAIL

- [ ] **Step 3: 从 `external_events.py` 抽出响应构造**

把 `get_events` 端点体里「查 run → 判 `is_terminal` → 构造 `StreamingResponse` + 那两个响应头」
的部分抽成模块级 `build_events_response(...)`,端点自身改为薄封装。**不改行为** ——
`tests/test_external_events.py` 必须原样绿。

- [ ] **Step 4: 重放分支接上**

Task 13 判定流程第 3 步「指纹相同 → 返回原 run」处按模式分叉：
- `mode == "queue"` → 202 信封（Task 15 之后是信封形态）
- `mode == "stream"` 且 `run_event_store` 已配 → `return build_events_response(...)`
- `mode == "stream"` 且未配 → 200 JSON 信封带 `run_id`，`data` 里加
  `"stream_unavailable": true` 说明降级

- [ ] **Step 5: 跑测试**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_external_idempotency.py tests/test_external_events.py -v`
Expected: 全 PASS（含既有 events 测试原样绿,证明抽取没改行为）

- [ ] **Step 6: 提交**

```bash
git add services/control-plane/src/control_plane/api/external_events.py services/control-plane/src/control_plane/api/agents.py services/control-plane/tests/test_external_idempotency.py
git commit -m "feat(control-plane): stream 模式幂等重试接到原 run 的事件流"
```

---

## Task 15: 对外 202 响应信封化

**Files:**
- Modify: `services/control-plane/src/control_plane/api/runs.py:757-850`（`spawn_run` 加 `envelope` 参数）
- Modify: `services/control-plane/src/control_plane/api/agents.py:945`
- Test: `services/control-plane/tests/test_external_api_contract.py`（追加）

**Interfaces:**
- Produces: `spawn_run(..., envelope: bool = False)`

> **破坏性改动**（仅对外）。控制台调用方 `api/runs.py:1079` **不传** `envelope`,
> 保持裸 JSON —— 它的消费者是 admin-ui。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 services/control-plane/tests/test_external_api_contract.py
@pytest.mark.asyncio
async def test_external_queue_run_returns_envelope(external_client, plain_agent) -> None:
    resp = await external_client.post(
        f"/v1/agents/{plain_agent.code}/runs",
        json={"user_id": "u1", "input": "你好", "mode": "queue"},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["success"] is True and body["error"] is None
    assert set(body["data"]) >= {"run_id", "thread_id", "status"}


@pytest.mark.asyncio
async def test_console_queue_run_stays_bare(console_client, seeded_thread) -> None:
    """控制台形状不动 —— admin-ui 在消费它。"""
    resp = await console_client.post(
        f"/v1/sessions/{seeded_thread.thread_id}/runs",
        json={"input": "你好", "mode": "queue"},
    )
    assert resp.status_code == 202
    assert set(resp.json()) == {"run_id", "thread_id", "status"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_external_api_contract.py -k envelope -v`
Expected: FAIL — `KeyError: 'success'`

- [ ] **Step 3: 实现**

`spawn_run` 加 `envelope: bool = False`，queue 分支：

```python
        content: dict[str, Any] = {
            "run_id": str(run_id), "thread_id": str(thread_id), "status": "queued"
        }
        if envelope:
            content = {"success": True, "data": content, "error": None}
        return JSONResponse(status_code=202, content=content)
```

`agents.py:945` 的 `spawn_run(...)` 加 `envelope=True`。

- [ ] **Step 4: 跑测试**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/test_external_api_contract.py tests/test_runs_api.py -v`
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add services/control-plane/src/control_plane/api/runs.py services/control-plane/src/control_plane/api/agents.py services/control-plane/tests/test_external_api_contract.py
git commit -m "feat(control-plane)!: 对外 POST runs 的 202 响应信封化"
```

---

## Task 16: 对外文档更新

**Files:**
- Modify: 文档站对外 API 章节（`POST .../runs` 请求体、会话列表、消息列表三处）
- Test: 无自动化测试；本任务的验收是人工核对渲染结果

- [ ] **Step 1: 定位文档源文件**

Run: `rg -l "image_refs|untrusted_content" docs/ --glob '!**/specs/**' --glob '!**/plans/**'`
把 `POST /v1/agents/{code}/runs` 的请求体表所在文件记下来。

- [ ] **Step 2: 更新请求体文档**

补三项，每项都要有真实可复制的 curl 示例：
- `inputs`：模板变量,未声明键 422
- `files[]`：字段表 + 图片/文档两个例子；写明 `transfer_method` 当前只有 `local_file`
- `Idempotency-Key`：header、同键同体返回原 run、同键异体 422、**永久**记忆、stream 模式重试会接到原 run 的事件流

- [ ] **Step 3: 更新响应文档**

- 会话列表加 `message_count`，写明 `null` = 该会话尚未算过（不是 0）
- 消息列表加 `created_at`（ISO8601）与 `run_id`，写明历史消息为 `null`
- `POST .../runs` 的 202 改成信封形态，**加一条醒目的破坏性变更说明**

- [ ] **Step 4: 本地起文档站核对**

Run: `pnpm -C docs dev`（若路径不同,照 `docs/` 下的 README 起）
逐页核对三处渲染正常、示例可复制。

- [ ] **Step 5: 提交**

```bash
git add docs/
git commit -m "docs: 对外 API 文档补 files[] / inputs / 幂等键与三个新响应字段"
```

---

## 收尾：全量校验

- [ ] **CI 同款全量跑**

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .            # 范围照 CI（含 tests,不含 control-plane 的例外见既有约定）
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
uv run pytest
```

- [ ] **真栈验收**（spec §八 的 1–9 条）

发测试集群后逐条跑通；`message_count` 与消息端点条数必须一致（同一 `include_hidden=False` 口径）。

---

## 自查记录

**Spec 覆盖**：块 2 → Task 1–8；块 1 → Task 9–14；块 3 → Task 15；文档 → Task 16。spec §六 块 4 由 [P2-b 计划](./2026-08-12-external-api-v1-p2b.md) 覆盖,不在本计划内。

**类型一致性**：`extract_turns`（Task 1 定义 → Task 5、7 使用)、`stamp_message`/`stamp_messages`（Task 3 定义 → Task 4 使用)、`update_message_count`（Task 6 定义 → Task 7 使用)、`ExternalFileRef`（Task 10 定义 → Task 11 使用)、`request_digest`/`IDEMPOTENCY_HEADER`（Task 13 定义 → Task 14 使用)、`find_by_idempotency_key`/`RunIdempotencyConflict`（Task 12 定义 → Task 13 使用）—— 名称与签名已逐一对齐。

**已知需实现者补齐的**：几处测试 fixture（`thread_meta_store`、`run_store`/`make_run_info`、`external_client`、`plain_agent`/`vision_agent`/`jinja_agent`、`build_minimal_agent`）计划中标注了「照既有参数化/构造写法复用」，未复制既有 conftest 内容 —— 实现时先在对应 `tests/conftest.py` 里确认是否已存在同名或等价 fixture，**优先复用,不新造一套桩**。
